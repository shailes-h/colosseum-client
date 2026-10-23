"""One-window YAM eval station: setup checks, live A/B recording, then review and submit.

  .venv/bin/python scripts/eval_gui.py --config configs/robot.yam-joint-llm.yaml
  open http://127.0.0.1:8770

Needs the gui extra (uv pip install --python .venv/bin/python -e '.[gui]').
It starts and stops the local Policy Server itself, keeps the LLM API keys in local/llm-keys.env
(set and verified from the Setup page), and records with scripts/record_evals.py. Scoring and
Submit/Discard reuse scripts/review_evals.py. Bound to 127.0.0.1 only.
Offline mode (--offline, or the switch in the header) needs no internet: scripts/offline_router.py
picks two local policies (at random, or the ones you choose) instead of the Router, and nothing is uploaded.
"""
import argparse
import asyncio
import collections
import contextlib
import io
import json
import os
import re
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import replace
from pathlib import Path
from urllib.parse import urlsplit

import httpx
import numpy as np
import uvicorn
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, Response
from PIL import Image

HERE = Path(__file__).resolve().parent
CLIENT = HERE.parent
sys.path.insert(0, str(HERE))
import offline_router as offline  # noqa: E402
import record_evals  # noqa: E402
import review_evals as rv  # noqa: E402

import colosseum_client.evaluation as ev  # noqa: E402
from colosseum_client.robot_config import RobotClientConfig  # noqa: E402

CAMS = rv.CAMS
TOP = 'head_image'
CAN_RESET = '/home/rc1/lerobot/i2rt/scripts/reset_all_can.sh'
OFFLINE = [False]  # no internet: no Router, no uploads, two local policies per eval (random or chosen)


# ---------- shared state ----------

class State:
    def __init__(self):
        self.lock = threading.Lock()
        self.version = 0
        self.checks = {k: {'status': 'idle', 'detail': ''} for k in
                       ('gpu', 'can', 'arms', 'cameras', 'router', 'keys', 'server')}
        self.keys = []             # per-provider key status for the Setup page; never the keys themselves
        self.run = {'phase': 'idle', 'side': None, 'step': 0, 'max_steps': 0, 'eval_id': None, 'message': '',
                    'instruction': ''}
        self.prompt = None
        self.log = collections.deque(maxlen=400)
        self.frame = None          # latest top-camera RGB array
        self.frame_seq = 0
        self.thumbs = {}           # camera -> jpeg bytes from the camera check
        self.last_obs = None       # latest robot observation, for holding the pose after Stop

    def touch(self):
        self.version += 1

    def set_check(self, name, status, detail=''):
        with self.lock:
            self.checks[name] = {'status': status, 'detail': detail}
            self.touch()

    def set_run(self, **kw):
        with self.lock:
            self.run.update(kw)
            self.touch()

    def push_frame(self, rgb):
        self.frame = rgb
        self.frame_seq += 1


S = State()
HW = threading.Lock()  # cameras and CAN buses: one user at a time


class Tee(io.TextIOBase):
    """Mirror stdout/stderr into the GUI log; also drives the run phase from known client messages."""
    def __init__(self, stream):
        self.stream, self.buf = stream, ''

    def write(self, text):
        self.stream.write(text)
        self.buf += text
        while '\n' in self.buf:
            line, self.buf = self.buf.split('\n', 1)
            if line.strip():
                self.line(line.rstrip())
        return len(text)

    def flush(self):
        self.stream.flush()

    def line(self, line):
        with S.lock:
            S.log.append(line)
            S.touch()
        if line.startswith('Trial started'):
            S.set_run(phase='running', message='')
        elif line.startswith('YAM return:'):
            S.set_run(phase='homing', message='Returning the arms to zero…')
        elif line.startswith('Finishing recording'):
            S.set_run(phase='saving', message='Saving the recording…')


def jpeg(rgb, width=None, quality=80):
    img = Image.fromarray(np.asarray(rgb, dtype=np.uint8))
    if width and img.width > width:
        img = img.resize((width, round(img.height * width / img.width)))
    out = io.BytesIO()
    img.save(out, 'JPEG', quality=quality)
    return out.getvalue()


def source_env(path):
    """Variables an env file exports, read with bash so quoting matches `source`."""
    if not path.exists():
        return {}
    out = subprocess.run(['bash', '-c', 'set -a; source "$1" >/dev/null; env -0', '_', str(path)],
                         capture_output=True, check=True, env={'PATH': os.environ.get('PATH', '')}).stdout
    env = dict(item.split('=', 1) for item in out.decode().split('\0') if '=' in item)
    return {k: v for k, v in env.items() if k not in ('PATH', 'PWD', 'SHLVL', '_', 'OLDPWD')}


def port_open(url, timeout=1.0):
    parts = urlsplit(url)
    try:
        with socket.create_connection((parts.hostname, parts.port or 80), timeout=timeout):
            return True
    except OSError:
        return False


# ---------- setup checks ----------

def tree(pid):
    """pid and all its descendants, from /proc."""
    children = collections.defaultdict(list)
    for stat in Path('/proc').glob('[0-9]*/stat'):
        try:
            fields = stat.read_text().rsplit(')', 1)[1].split()
            children[int(fields[1])].append(int(stat.parent.name))
        except (OSError, IndexError, ValueError):
            pass
    out, todo = set(), [pid]
    while todo:
        p = todo.pop()
        out.add(p)
        todo += children[p]
    return out


def gpu_apps():
    out = subprocess.run(['nvidia-smi', '--query-compute-apps=pid,process_name,used_memory',
                          '--format=csv,noheader,nounits'], capture_output=True, text=True, timeout=15)
    if out.returncode:
        raise RuntimeError(out.stderr.strip() or 'nvidia-smi failed')
    apps = []
    for row in out.stdout.strip().splitlines():
        pid, name, mem = [x.strip() for x in row.split(',')]
        apps.append({'pid': int(pid), 'name': Path(name).name, 'mem': int(mem) if mem.isdigit() else 0})
    return apps


def check_gpu():
    S.set_check('gpu', 'running', 'Looking at the GPU…')
    try:
        apps = gpu_apps()
    except Exception as e:
        return S.set_check('gpu', 'fail', str(e))
    ours = SERVER.pids()
    leftover = [a for a in apps if a['pid'] not in ours]
    mine = [a for a in apps if a['pid'] in ours]
    if leftover:
        names = ', '.join(f"{a['name']} (pid {a['pid']}, {a['mem']} MiB)" for a in leftover)
        return S.set_check('gpu', 'fail', f'Leftover GPU programs: {names}. Click “Free GPU”.')
    S.set_check('gpu', 'ok', f"Policy loaded: {sum(a['mem'] for a in mine)} MiB in use" if mine else 'GPU is free')


def free_gpu():
    ours = SERVER.pids()
    leftover = [a['pid'] for a in gpu_apps() if a['pid'] not in ours]
    for sig, wait in ((signal.SIGINT, 8), (signal.SIGTERM, 5)):
        for pid in leftover:
            try:
                os.kill(pid, sig)
            except OSError:
                pass
        deadline = time.time() + wait
        while time.time() < deadline and any(Path(f'/proc/{p}').exists() for p in leftover):
            time.sleep(.5)
    check_gpu()


def can_links():
    out = subprocess.run(['ip', '-br', 'link'], capture_output=True, text=True).stdout
    return {line.split()[0]: line.split()[1] for line in out.splitlines() if line.split() and line.startswith('can')}


def check_can(reset=False):
    S.set_check('can', 'running', 'Bringing CAN up (enter your password in the pop-up)…' if reset else 'Checking CAN…')
    if reset:
        out = subprocess.run(['pkexec', 'bash', CAN_RESET], capture_output=True, text=True)
        if out.returncode:
            return S.set_check('can', 'fail', f'CAN reset failed or was cancelled. In a terminal run: bash {CAN_RESET}')
    links = can_links()
    need = [CONFIG.adapter_config.get('left_channel'), CONFIG.adapter_config.get('right_channel')]
    down = [c for c in need if links.get(c) not in ('UP', 'UNKNOWN')]
    if down:
        return S.set_check('can', 'fail', f"Not up: {', '.join(down)}. Click “Bring CAN up”.")
    S.set_check('can', 'ok', ' · '.join(f'{c} up' for c in need))


def check_arms():
    """Read every motor's registers without enabling it: proves both arms answer, nothing moves."""
    if S.checks['can']['status'] != 'ok':
        check_can()
        if S.checks['can']['status'] != 'ok':
            return S.set_check('arms', 'fail', 'Bring CAN up first')
    S.set_check('arms', 'running', 'Asking every motor to answer (no power to the motors)…')
    try:
        from i2rt.motor_drivers.motor_check import run_startup_checks
        from i2rt.robots.utils import ArmType, GripperType, _load_arm_config
    except ImportError:
        return S.set_check('arms', 'fail', 'I2RT is not installed in the client environment')
    hw = _load_arm_config(ArmType.YAM)
    motors = [list(r) for r in hw.motor_list] + [[7, GripperType.LINEAR_4310.get_motor_type(ArmType.YAM)]]
    if not HW.acquire(blocking=False):
        return S.set_check('arms', 'fail', 'The robot is busy (camera preview or an evaluation)')
    results = []
    try:
        for side in ('left', 'right'):
            channel = CONFIG.adapter_config.get(f'{side}_channel')
            try:
                run_startup_checks(channel, motors, check_motor_types=True, check_motor_config=False,
                                   control_mode='MIT', repair=False)
                results.append((side, True, f'{len(motors)}/{len(motors)} motors'))
            except Exception as e:
                msg = str(e).split(' Check that', 1)[0]
                results.append((side, False, msg))
    finally:
        HW.release()
    ok = all(r[1] for r in results)
    detail = ' · '.join(f"{side.capitalize()} arm: {'✓ ' + msg if good else msg}" for side, good, msg in results)
    if not ok:
        detail += ' — check the arm power and that the emergency stop is released.'
    S.set_check('arms', 'ok' if ok else 'fail', detail)


def check_cameras():
    S.set_check('cameras', 'running', 'Capturing a frame from each camera…')
    from colosseum_client.robots.yam import _RealSenseCamera
    if not HW.acquire(blocking=False):
        return S.set_check('cameras', 'fail', 'Cameras are busy (preview or an evaluation)')
    bad, dark, thumbs = [], [], {}
    try:
        for name, serial in CONFIG.cameras.items():
            cam = None
            try:
                cam = _RealSenseCamera(serial, 3000)
                for _ in range(30):  # the first frames are black until auto-exposure settles
                    frame = cam.read()
                thumbs[name] = jpeg(frame, 640)
                if frame.mean() < 15:
                    dark.append(name)
            except Exception as e:
                bad.append(f'{name} ({serial}): {e}')
            finally:
                if cam is not None:
                    try:
                        cam.close()
                    except Exception:
                        pass
    finally:
        HW.release()
    with S.lock:
        S.thumbs = thumbs
        S.touch()
    if bad:
        return S.set_check('cameras', 'fail', '; '.join(bad) + '. Check the USB cable.')
    S.set_check('cameras', 'ok', f'{len(thumbs)}/{len(CONFIG.cameras)} cameras'
                + (f" · very dark: {', '.join(dark)} (lights off or lens covered?)" if dark else ''))


def offline_policies():
    return offline.policies(ARGS.server_dir / ARGS.server_config, CONFIG)


def check_router():
    if OFFLINE[0]:
        names = [name for name, _ in offline_policies()]
        if len(names) < 2:
            return S.set_check('router', 'fail', f'Offline mode needs two local policies in {ARGS.server_config}; '
                                                 f"found {', '.join(names) or 'none'}")
        return S.set_check('router', 'ok', f"Offline: not used. Each eval picks 2 of {len(names)} local policies "
                                           f"at random or as you choose ({', '.join(names)}). Nothing is uploaded.")
    S.set_check('router', 'running', 'Contacting the Router…')
    api = offline.RealEvalAPI(CONFIG)
    try:
        api.request('GET', '/tasks?robot_id=' + CONFIG.robot_type)
        S.set_check('router', 'ok', f'Connected as {CONFIG.institution}')
    except Exception as e:
        S.set_check('router', 'fail', f'Cannot reach the Router: {type(e).__name__}: {e}'[:300])
    finally:
        api.close()


# provider -> (label, variable in the keys file)
PROVIDERS = {'openai': ('OpenAI · GPT', 'OPENAI_API_KEY'), 'xai': ('xAI · Grok', 'XAI_API_KEY'),
             'anthropic': ('Anthropic · Claude', 'ANTHROPIC_API_KEY')}
KEYS = {}        # provider -> key, loaded from --llm-keys
KEY_OK = set()   # providers whose key passed the last verification
CLAUDE_LOGIN = [False]  # Claude runs on the Claude Code login (no Anthropic key saved) and it works


def verify_key(provider, key):
    """List models with the key: proves it is accepted without spending tokens. Returns (ok, message)."""
    try:
        if provider == 'anthropic':
            import anthropic
            try:
                anthropic.Anthropic(api_key=key, max_retries=1, timeout=20).models.list(limit=1)
            except (anthropic.AuthenticationError, anthropic.PermissionDeniedError):
                return False, 'rejected'
            except anthropic.APIConnectionError:
                return False, 'could not reach Anthropic'
            except anthropic.APIStatusError as e:
                return False, f'error {e.status_code}'
            return True, 'works'
        url = {'openai': 'https://api.openai.com/v1/models', 'xai': 'https://api.x.ai/v1/models'}[provider]
        r = httpx.get(url, headers={'Authorization': f'Bearer {key}'}, timeout=20)
    except httpx.HTTPError:
        return False, 'could not reach the provider'
    if r.status_code in (401, 403):
        return False, 'rejected'
    return (True, 'works') if r.is_success else (False, f'error {r.status_code}')


def claude_login():
    """Claude Code login (Claude subscription) used for Claude when no Anthropic API key is saved."""
    try:
        out = subprocess.run(['claude', 'auth', 'status', '--text'], capture_output=True, text=True,
                             timeout=30, env={**os.environ, **CLAUDE_ENV})
    except (OSError, subprocess.TimeoutExpired):
        return False, 'Claude Code CLI not found'
    method = next((ln.split(':', 1)[1].strip() for ln in out.stdout.splitlines() if ln.startswith('Login method')), '')
    if out.returncode or not method:
        return False, 'Claude Code login expired: run  claude auth login --claudeai  in a terminal'
    return True, 'works'


def check_keys():
    if OFFLINE[0]:
        with S.lock:
            S.keys = []
            S.touch()
        return S.set_check('keys', 'ok', 'Not needed offline: cloud LLM policies are left out.')
    S.set_check('keys', 'running', 'Checking each API key with its provider…')
    rows, problems = [], []
    CLAUDE_LOGIN[0] = False
    for provider, (label, _) in PROVIDERS.items():
        key = KEYS.get(provider, '')
        ok, msg = verify_key(provider, key) if key else (False, 'missing')
        (KEY_OK.add if ok else KEY_OK.discard)(provider)
        shown = 'API key' if key else ''  # how it authenticates; no part of a key leaves this process
        if provider == 'anthropic' and not key:
            ok, msg = claude_login()
            CLAUDE_LOGIN[0], shown = ok, 'Claude Code login'
        rows.append({'provider': provider, 'label': label, 'ok': ok, 'msg': msg, 'source': shown})
        if not ok:
            problems.append(f'{label}: {msg}' if shown == 'Claude Code login' else f'{label} key {msg}')
    with S.lock:
        S.keys = rows
        S.touch()
    if problems:
        return S.set_check('keys', 'fail', 'Please check your keys: ' + '; '.join(problems) + '.')
    S.set_check('keys', 'ok', 'All keys work' + (' (Claude via the Claude Code login)' if CLAUDE_LOGIN[0] else ''))


def save_keys(new):
    """Write changed keys to the keys file (owner-only), keeping any other lines in it."""
    path = ARGS.llm_keys
    names = {var for _, var in PROVIDERS.values()}
    lines = [ln for ln in (path.read_text().splitlines() if path.exists() else [])
             if not any(re.match(rf'\s*(export\s+)?{v}=', ln) for v in names)]
    for provider, value in new.items():
        if provider in PROVIDERS and isinstance(value, str) and value.strip():
            KEYS[provider] = value.strip()
    for provider, key in KEYS.items():
        lines.append(f'export {PROVIDERS[provider][1]}={shlex.quote(key)}')
        os.environ[PROVIDERS[provider][1]] = key
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w') as f:
        f.write('\n'.join(lines) + '\n')
    os.chmod(path, 0o600)


class PolicyServer:
    def __init__(self):
        self.proc = None
        self.log = None
        self.busy = False  # starting or stopping: the poller leaves the status alone

    def pids(self):
        return tree(self.proc.pid) if self.proc and self.proc.poll() is None else set()

    def status(self):
        url = CONFIG.policy_server_url
        if self.proc and self.proc.poll() is None:
            if port_open(url):
                return S.set_check('server', 'ok', f'Running ({url}). Models load on demand.')
            return S.set_check('server', 'running', 'Starting…')
        if port_open(url):
            return S.set_check('server', 'ok', 'Running (started outside this window)')
        code = self.proc.returncode if self.proc else None
        S.set_check('server', 'fail' if code else 'idle',
                    f'Stopped (exit {code}). See {self.log_path.name}.' if code else 'Not running')

    @property
    def log_path(self):
        return CLIENT / 'local' / 'logs' / 'eval-gui-policy-server.log'

    def start(self):
        if (self.proc and self.proc.poll() is None) or port_open(CONFIG.policy_server_url):
            return self.status()
        self.busy = True
        S.set_check('server', 'running', 'Starting…')
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self.log = self.log_path.open('ab')
        self.proc = subprocess.Popen(
            [shutil.which('uv') or 'uv', 'run', '--no-sync', 'colosseum-policy-local', '--config', str(ARGS.server_config)],
            cwd=ARGS.server_dir, env={**os.environ, **CLAUDE_ENV}, stdout=self.log, stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL, start_new_session=True)
        deadline = time.time() + 120
        while time.time() < deadline and self.proc.poll() is None and not port_open(CONFIG.policy_server_url):
            time.sleep(1)
        self.busy = False
        self.status()

    def stop(self):
        if not (self.proc and self.proc.poll() is None):
            return self.status()
        self.busy = True
        S.set_check('server', 'running', 'Stopping (freeing the GPU)…')
        for sig, wait in ((signal.SIGINT, 20), (signal.SIGTERM, 10), (signal.SIGKILL, 5)):
            try:
                os.killpg(self.proc.pid, sig)
            except OSError:
                break
            try:
                self.proc.wait(wait)
                break
            except subprocess.TimeoutExpired:
                continue
        self.busy = False
        self.status()


SERVER = PolicyServer()


def run_all_checks():
    for step in (check_gpu, check_can, check_arms, check_cameras, check_router, check_keys, SERVER.start):
        step()


# ---------- live preview and recording ----------

class Preview:
    """Top camera stream for the setup form; released before the robot takes the cameras."""
    def __init__(self):
        self.stop_event = threading.Event()
        self.thread = None

    def start(self):
        if self.thread and self.thread.is_alive():
            return True
        if not HW.acquire(blocking=False):
            return False
        self.stop_event.clear()
        self.thread = threading.Thread(target=self.loop, daemon=True)
        self.thread.start()
        return True

    def loop(self):
        from colosseum_client.robots.yam import _RealSenseCamera
        cam = None
        try:
            cam = _RealSenseCamera(CONFIG.cameras[TOP], 3000)
            while not self.stop_event.is_set():
                S.push_frame(cam.read())
        except Exception as e:
            print(f'Top camera preview stopped: {e}')
        finally:
            if cam is not None:
                try:
                    cam.close()
                except Exception:
                    pass
            HW.release()

    def stop(self):
        self.stop_event.set()
        if self.thread:
            self.thread.join(5)


PREVIEW = Preview()


class Prompts:
    """Answers the client's terminal questions from the browser."""
    def __init__(self):
        self.answer = None
        self.event = threading.Event()
        self.finish_now = threading.Event()  # set by Stop / Task done; cleared when the next run starts
        self.stop_reason = None
        self.recovery = None
        self.closing = False

    def ask(self, prompt=''):
        if self.closing:
            raise KeyboardInterrupt
        text = prompt.strip()
        m = re.match(r'^(\w+): restore the scene', text)
        if m:
            S.set_run(phase='setup', side=m.group(1), step=0, message='')
            kind, title = 'scene', f'Get ready for Policy {m.group(1)}'
            if m.group(1) in S.run.get('known', {}):
                title += f" ({S.run['known'][m.group(1)]})"
            body = 'Put every object back in its starting position, then click Start.'
        elif text.startswith('Confirm this physical trial'):
            kind, title = 'confirm', 'The robot is about to move'
            body = 'The policy is loaded. Make sure the workspace is clear and the emergency stop is within reach.'
        else:
            kind, title, body = 'text', 'Input needed', text
        S.set_run(phase='prompt')
        with S.lock:
            S.prompt = {'id': time.time_ns(), 'kind': kind, 'title': title, 'body': body, 'side': S.run['side']}
            S.touch()
        self.event.clear()
        self.event.wait()
        with S.lock:
            S.prompt = None
            S.touch()
        if self.answer is None:
            raise KeyboardInterrupt
        if kind == 'confirm':
            if self.answer != 'y':
                raise KeyboardInterrupt
            S.set_run(phase='starting', message='Starting the robot…')
        if kind == 'scene':
            S.set_run(phase='loading', message='Loading the policy. The first run of a model can take a few minutes…')
        return self.answer

    def reply(self, answer):
        self.answer = answer
        self.event.set()

    # Stand-ins for evaluation.py's stdin polling ("press Enter to finish").
    def isatty(self):
        return True

    def readline(self):
        return '\n'

    def select(self, r, w, x, timeout=0):
        return (r if self.finish_now.is_set() else []), [], []

    def recovery_choice(self):
        """YAMRobot._zero_recovery_choice, driven by the Retry/Skip buttons."""
        with S.lock:
            if S.prompt is None or S.prompt['kind'] != 'recovery':
                S.prompt = {'id': time.time_ns(), 'kind': 'recovery', 'title': 'Return to zero failed',
                            'body': 'The arms are holding their position. Retry the return, or support both arms '
                                    'by hand and skip, which releases motor power.', 'side': S.run['side']}
                S.touch()
        choice, self.recovery = self.recovery, None
        if choice:
            with S.lock:
                S.prompt = None
                S.touch()
        return choice


PROMPTS = Prompts()


def install_hooks():
    """Observe and steer the unchanged evaluation loop: live frames, A/B side, finish button, prompts."""
    from colosseum_client.robots import yam
    read = ev.read_observation

    def observe(robot, step, phase):
        obs = read(robot, step, phase)
        S.last_obs = obs
        if TOP in obs.images:
            S.push_frame(obs.images[TOP])
        S.run['step'] = step
        S.version += 1
        return obs
    ev.read_observation = observe

    execute = ev.execute_robot_action_async

    async def execute_unless_stopped(robot, action, step, robot_call, enabled=True):
        """After Stop, hold the measured pose instead of sending the policy's action, even if an
        inference was already in flight; the loop then ends at its next step check."""
        if not PROMPTS.finish_now.is_set():
            return await execute(robot, action, step, robot_call, enabled)
        obs = S.last_obs
        return np.r_[obs.joints[:6], obs.gripper[:1], obs.joints[6:], obs.gripper[1:]].astype(np.float32)
    ev.execute_robot_action_async = execute_unless_stopped

    trial = ev.run_trial

    async def run_trial(config, assignment, run, path, *a, **k):
        PROMPTS.finish_now.clear()
        PROMPTS.stop_reason = None
        S.set_run(side=run['side'], step=0, max_steps=assignment['task'].get('max_steps', config.max_trial_steps),
                  eval_id=assignment['id'], instruction=assignment['task'].get('instruction', ''),
                  known=offline.chosen(path.parent) if offline.is_offline(assignment['id']) else {})
        try:
            return await trial(config, assignment, run, path, *a, **k)
        finally:
            if PROMPTS.stop_reason:
                path.mkdir(parents=True, exist_ok=True)
                (path / 'operator-stop.json').write_text(json.dumps(
                    {'reason': PROMPTS.stop_reason, 'step': S.run['step']}))
            S.set_run(phase='between', message=f"Policy {run['side']} finished.")
    ev.run_trial = run_trial
    ev.sys = PROMPTS_SYS
    ev.select = PROMPTS
    yam.YAMRobot._zero_recovery_choice = staticmethod(PROMPTS.recovery_choice)


class _Sys:
    stdin = PROMPTS


PROMPTS_SYS = _Sys()


def record_thread(form):
    config = replace(CONFIG, track='open', instruction=form.get('instruction', '').strip(),
                     scene=form.get('scene', '').strip() or CONFIG.scene,
                     evaluator=form.get('evaluator', '').strip() or CONFIG.evaluator,
                     max_trial_steps=int(form.get('max_steps') or CONFIG.max_trial_steps),
                     llm_api_keys={p: KEYS[p] for p in KEY_OK if p in KEYS},
                     llm_account_logins={'anthropic': 'claude-code'} if CLAUDE_LOGIN[0] else {})
    resume = form.get('resume') or None
    offline.ACTIVE = offline.is_offline(resume) if resume else OFFLINE[0]
    offline.PICK = {side: form.get('policy_' + side.lower()) for side in 'AB'}
    S.set_run(phase='starting', side=None, step=0, max_steps=config.max_trial_steps, eval_id=resume, known={},
              instruction=config.instruction, message='Picking two local policies…' if offline.ACTIVE
              else 'Asking the Router for two policies…')
    try:
        eval_id = record_evals.record(config, resume=resume, ask=PROMPTS.ask)
        if eval_id:
            S.set_run(phase='done', eval_id=eval_id, message='Both runs recorded. Score them now.')
        else:
            S.set_run(phase='error', message='Nothing was recorded. See the log below.')
    except KeyboardInterrupt:
        S.set_run(phase='error', message='Cancelled. Start again, or resume this evaluation.')
    except BaseException as e:
        S.set_run(phase='error', message=f'Stopped: {type(e).__name__}: {e}'[:400])
        import traceback
        traceback.print_exc()
    finally:
        HW.release()


RECORDER = {'thread': None}


def recording():
    t = RECORDER['thread']
    return t is not None and t.is_alive()


# ---------- review (reuses review_evals.py) ----------

def eval_summary(path):
    d = json.loads(path.read_text())
    a = d['assignment']
    state = ('submitted' if d.get('submitted') else 'discarded' if d.get('discarded')
             else 'kept' if d.get('kept_offline') else 'superseded' if d.get('review') == 'superseded'
             else 'scored' if 'result' in d else 'review' if d.get('review') == 'pending' else 'interrupted')
    return {'id': a['id'], 'instruction': a.get('task', {}).get('instruction', ''), 'scene': a.get('scene', ''),
            'state': state, 'time': path.stat().st_mtime, 'busy': rv.busy(a['id']), 'offline': offline.is_offline(a['id']),
            'known': offline.chosen(path.parent) if offline.is_offline(a['id']) else {}}


def notes_path(eid):
    return rv.ROOT / eid / 'review-notes.json'


def load_notes(eid):
    try:
        return json.loads(notes_path(eid).read_text())
    except (OSError, ValueError):
        return {}


def operator_stop(path):
    try:
        return json.loads((path / 'operator-stop.json').read_text())
    except (OSError, ValueError):
        return None


def eval_detail(eid):
    d = rv.load(eid)
    notes = load_notes(eid)
    res = d.get('result', {})
    outcomes = res.get('outcomes') or d.get('outcomes', {})
    draft = d.get('draft', {})
    log = rv.ROOT / eid / 'review-router.log'
    return {**eval_summary(rv.ROOT / eid / 'manifest.json'),
            'runs': [{'id': r['id'], 'side': r['side'], 'recorded': r['recorded'],
                      'cams': [c for c in CAMS if (r['path'] / c).is_dir()],
                      'stop': operator_stop(r['path']), 'notes': notes.get(r['side'], ''),
                      'score': None if outcomes.get(r['side'], {}).get('partial_success') is None
                      else round(outcomes[r['side']]['partial_success'] * 100)} for r in rv.runs(d)],
            'factors': notes.get('factors', {}),
            'preference': res.get('preference') or draft.get('preference') or '',
            'feedback': res.get('feedback', draft.get('feedback', '')),
            'log': log.read_text()[-4000:] if log.exists() else ''}


def close_offline(eid, **mark):
    """Submit/Discard for an offline eval: marked in its manifest only, never sent."""
    path = rv.ROOT / eid / 'manifest.json'
    d = json.loads(path.read_text())
    d.update(mark)
    d.pop('review', None)
    tmp = path.with_suffix('.tmp')
    tmp.write_text(json.dumps(d, indent=2))
    tmp.replace(path)


def plain(msg):
    return re.sub(r'<[^>]+>', '', msg)


# ---------- web app ----------

app = FastAPI(lifespan=lambda a: lifespan(a))  # defined below, with the shutdown handler


def known(eid):
    if not eid or '/' in eid or '..' in eid or not (rv.ROOT / eid / 'manifest.json').exists():
        raise HTTPException(404)
    return eid


@app.get('/')
def index():
    return HTMLResponse((HERE / 'eval_gui.html').read_text())


@app.get('/api/config')
def config_info():
    return {'scene': CONFIG.scene, 'evaluator': CONFIG.evaluator, 'max_steps': CONFIG.max_trial_steps,
            'cameras': list(CONFIG.cameras), 'robot': CONFIG.robot_type, 'institution': CONFIG.institution}


@app.post('/api/check/{name}')
def run_check(name: str):
    actions = {'all': run_all_checks, 'gpu': check_gpu, 'free_gpu': free_gpu, 'can': check_can,
               'can_reset': lambda: check_can(reset=True), 'arms': check_arms, 'cameras': check_cameras,
               'router': check_router, 'keys': check_keys, 'server': SERVER.start,
               'server_stop': SERVER.stop}
    if name not in actions:
        raise HTTPException(404)
    if recording() and name not in ('gpu', 'router', 'keys'):
        raise HTTPException(409, 'An evaluation is running')
    threading.Thread(target=actions[name], daemon=True).start()
    return {'ok': True}


@app.post('/api/keys')
def set_keys(body: dict):
    if recording():
        raise HTTPException(409, 'An evaluation is running')
    save_keys(body)
    threading.Thread(target=check_keys, daemon=True).start()
    return {'ok': True}


@app.post('/api/offline')
def set_offline(body: dict):
    if recording():
        raise HTTPException(409, 'An evaluation is running')
    OFFLINE[0] = bool(body.get('on'))
    with S.lock:
        S.touch()
    threading.Thread(target=lambda: (check_router(), check_keys()), daemon=True).start()
    return {'ok': True}


@app.get('/api/offline/policies')
def offline_policy_names():
    return [name for name, _ in offline_policies()]


@app.get('/api/thumb/{cam}.jpg')
def thumb(cam: str):
    data = S.thumbs.get(cam)
    if data is None:
        raise HTTPException(404)
    return Response(data, media_type='image/jpeg', headers={'Cache-Control': 'no-store'})


@app.post('/api/preview/{action}')
def preview(action: str):
    if action == 'start':
        if recording():
            return {'ok': True}
        return {'ok': PREVIEW.start()}
    PREVIEW.stop()
    return {'ok': True}


@app.post('/api/eval/start')
def start_eval(form: dict):
    if recording():
        raise HTTPException(409, 'An evaluation is already running')
    if not form.get('resume') and not form.get('instruction', '').strip():
        raise HTTPException(400, 'Please type the task instruction')
    if not form.get('resume') and OFFLINE[0] and form.get('policy_a') and form.get('policy_a') == form.get('policy_b'):
        raise HTTPException(400, 'Policy A and Policy B must be different')
    PREVIEW.stop()
    if not HW.acquire(timeout=6):
        raise HTTPException(409, 'The robot is busy with a check; try again in a moment')
    PROMPTS.finish_now.clear()
    with S.lock:
        S.frame = None
        S.log.clear()
        S.touch()
    t = threading.Thread(target=record_thread, args=(form,), daemon=True, name='eval-recorder')
    RECORDER['thread'] = t
    t.start()
    return {'ok': True}


@app.post('/api/eval/reply')
def reply(body: dict):
    if body.get('kind') == 'recovery':
        PROMPTS.recovery = body.get('answer')
    else:
        PROMPTS.reply(body.get('answer'))
    return {'ok': True}


@app.post('/api/eval/finish')
def finish_now(body: dict):
    """End the current run now: 'stopped' (unsafe) or 'completed'. It is recorded up to this step,
    the arms return to zero, and the evaluation continues with the next policy."""
    if S.run['phase'] != 'running':
        raise HTTPException(409, 'No policy is running')
    PROMPTS.stop_reason = 'stopped' if body.get('reason') == 'stopped' else 'completed'
    PROMPTS.finish_now.set()
    S.set_run(phase='stopping', message='Stopping the policy and returning the arms to zero…'
              if PROMPTS.stop_reason == 'stopped' else 'Ending this run…')
    return {'ok': True}


@app.post('/api/eval/reset')
def reset_run():
    if recording():
        raise HTTPException(409, 'An evaluation is running')
    S.set_run(phase='idle', side=None, step=0, eval_id=None, message='')
    return {'ok': True}


@app.get('/api/evals')
def list_evals():
    """Evals of the current mode only: offline and online ones are listed (and resumed) separately."""
    out = []
    for m in rv.ROOT.glob('*/manifest.json'):
        if offline.is_offline(m.parent.name) != OFFLINE[0]:
            continue
        try:
            out.append(eval_summary(m))
        except Exception:
            continue
    return sorted(out, key=lambda e: e['time'], reverse=True)[:60]


@app.get('/api/evals/{eid}')
def get_eval(eid: str):
    return eval_detail(known(eid))


@app.post('/api/evals/{eid}')
def act_eval(eid: str, body: dict):
    known(eid)
    if rv.busy(eid):
        raise HTTPException(409, 'Still talking to the Router; wait a moment')
    action = body.get('action', 'save')
    if action == 'discard':
        reason = (body.get('reason') or '').strip()
        if not reason:
            raise HTTPException(400, 'A reason is required to discard')
        if offline.is_offline(eid):
            close_offline(eid, discarded=True, discard_reason=reason)
            return {'msg': 'Discarded on this computer.'}
        return {'msg': plain(rv.router(eid, 'discard', reason))}
    if isinstance(body.get('notes'), dict):
        notes = {k: str(v) for k, v in body['notes'].items() if k in ('A', 'B')}
        if isinstance(body.get('factors'), dict):
            notes['factors'] = {k: v for k, v in body['factors'].items() if k in FACTORS and v in FACTOR_TEXT}
        notes_path(eid).write_text(json.dumps(notes, ensure_ascii=False))
    form = {k: [str(v)] for k, v in body.items() if k.startswith('score_') or k in ('preference', 'feedback')}
    try:
        rv.save(eid, form)
    except ValueError as e:
        raise HTTPException(400, f'Not saved: {e}')
    if action == 'submit':
        if 'result' not in rv.load(eid):
            raise HTTPException(400, 'Give both runs a score and choose a preference before submitting')
        if offline.is_offline(eid):
            close_offline(eid, kept_offline=True)
            return {'msg': 'Result kept on this computer. Nothing was uploaded.'}
        return {'msg': plain(rv.router(eid, 'submit'))}
    return {'msg': 'Saved on this computer.' if 'result' in rv.load(eid) else
            'Draft saved. Both scores and a preference are needed to submit.'}


FACTORS = ('speed', 'smoothness', 'safety', 'precision')
# A2/B2 = better, A1/B1 = slightly better; 'none' (Safety only) = not evaluated, left out of the feedback
FACTOR_TEXT = {'A2': 'Policy A better', 'A1': 'Policy A slightly better', 'tie': 'tie',
               'B1': 'Policy B slightly better', 'B2': 'Policy B better', 'none': None}

FEEDBACK_PROMPT = """You write the evaluator feedback for a blind A/B comparison of two robot policies that
attempted the same task. Turn the evaluator's own observations into clear, consistent feedback.

Task: {task}
Scene: {scene}
{sides}
Evaluator's factor comparison:
{factors}
Evaluator preference: {preference}

Write in English (the observations may be in English or Chinese), as plain text in exactly this form:
Policy A: <2-3 sentences: what it achieved, where and how it failed, anything unsafe>
Policy B: <2-3 sentences, same>
Overall: <1-2 sentences: which did better and the deciding difference>

Rules: use only facts from the observations, scores, run endings and factor comparison; never invent
details. Reflect each compared factor where it fits (a tie means no advantage either way); do not mention
a factor that is not listed. Keep it consistent with the scores, the factors and the preference. Neutral, specific, no model names, no markdown, under 130 words."""


@app.get('/api/evals/{eid}/reveal')
def reveal(eid: str):
    """Which policy ran as A and B in an offline eval; only after its result is kept, so scoring stays blind."""
    d = rv.load(known(eid))
    if not offline.is_offline(eid) or not d.get('kept_offline'):
        raise HTTPException(409, 'Keep the result first; the policies are revealed only after that')
    names = offline.load_state(rv.ROOT / eid)['policies']
    return {r['side']: names[r['id']]['name'] for r in d['assignment']['runs']}


@app.post('/api/evals/{eid}/generate')
def generate_feedback(eid: str, body: dict):
    d = rv.load(known(eid))
    a = d['assignment']
    sides = []
    for r in rv.runs(d):
        side = r['side']
        stop = operator_stop(r['path'])
        ending = ('stopped by the evaluator as unsafe at step {step}' if stop and stop['reason'] == 'stopped'
                  else 'ended by the evaluator as done at step {step}' if stop else 'ran until it finished or hit the step limit')
        score = str(body.get(f'score_{side}', '')).strip() or 'not given'
        obs = str((body.get('notes') or {}).get(side, '')).strip() or '(no observations written)'
        sides.append(f'Policy {side}: score {score}/100; run {ending.format(**(stop or {}))}.\n'
                     f'Evaluator observations for {side}: {obs}')
    if not any((body.get('notes') or {}).values()):
        raise HTTPException(400, 'Write a few observations for each policy first')
    picked = body.get('factors') or {}
    middle = {f: 'none' if f == 'safety' else 'tie' for f in FACTORS}  # Safety's middle option is No eval
    if any(picked.get(f) not in ('A2', 'A1', middle[f], 'B1', 'B2') for f in FACTORS):
        raise HTTPException(400, 'Select one option for every factor under “Compare the policies” first')
    factors = '\n'.join(f'- {f.capitalize()}: {FACTOR_TEXT[picked[f]]}' for f in FACTORS if FACTOR_TEXT[picked[f]])
    prompt = FEEDBACK_PROMPT.format(task=a.get('task', {}).get('instruction', ''), scene=a.get('scene', '') or '-',
                                    factors=factors,
                                    sides='\n'.join(sides), preference=body.get('preference') or 'not chosen')
    if not KEYS.get('anthropic'):
        return {'feedback': feedback_via_claude_code(prompt)}
    import anthropic
    client = anthropic.Anthropic(api_key=KEYS['anthropic'], timeout=120)
    try:
        response = client.messages.create(
            model='claude-haiku-5-5', max_tokens=16000, output_config={'effort': 'low'},
            messages=[{'role': 'user', 'content': prompt}])
    except (anthropic.AuthenticationError, anthropic.PermissionDeniedError):
        raise HTTPException(502, 'The Anthropic key was rejected; please check your keys on the Setup page')
    except anthropic.APIConnectionError:
        raise HTTPException(502, 'Could not reach Anthropic; check the internet connection')
    except anthropic.APIStatusError as e:
        raise HTTPException(502, f'Feedback generation failed ({e.status_code}); try again')
    text = ''.join(b.text for b in response.content if b.type == 'text').strip()
    if response.stop_reason == 'refusal' or not text:
        raise HTTPException(502, 'No feedback was written; edit the observations and try again')
    return {'feedback': text}


def feedback_via_claude_code(prompt):
    try:
        out = subprocess.run(['claude', '-p', '--model', 'claude-haiku-5-5', '--output-format', 'text',
                              '--tools', '', '--effort', 'low'], input=prompt, capture_output=True, text=True,
                             timeout=180, cwd=tempfile.gettempdir(), env={**os.environ, **CLAUDE_ENV})
    except (OSError, subprocess.TimeoutExpired) as e:
        raise HTTPException(502, f'Feedback generation failed: {e}')
    if out.returncode or not out.stdout.strip():
        raise HTTPException(502, 'Feedback generation failed; check the Claude login on the Setup page')
    return out.stdout.strip()


@app.get('/video/{eid}/{run_id}/{cam}')
def video(eid: str, run_id: str, cam: str):
    known(eid)
    if cam not in CAMS or run_id not in {r['id'] for r in rv.load(eid)['assignment'].get('runs', [])}:
        raise HTTPException(404)
    f = rv.preview(eid, run_id, cam)
    if not f:
        raise HTTPException(404)
    return FileResponse(f, media_type='video/mp4')


def snapshot():
    with S.lock:
        return {'checks': S.checks, 'run': dict(S.run), 'prompt': S.prompt, 'log': list(S.log)[-200:],
                'recording': recording(), 'thumbs': sorted(S.thumbs), 'keys': S.keys, 'offline': OFFLINE[0], 'preview': bool(PREVIEW.thread and PREVIEW.thread.is_alive())}


@app.websocket('/ws')
async def ws(sock: WebSocket):
    await sock.accept()
    sent_version, sent_frame, last_beat = -1, -1, 0
    try:
        while True:
            now = time.monotonic()
            # State at most 5 times a second (steps change it ~30 times); camera frames up to 15.
            if (S.version != sent_version and now - last_beat > .2) or now - last_beat > 2:
                sent_version, last_beat = S.version, now
                await sock.send_text(json.dumps(snapshot()))
            if S.frame_seq != sent_frame and S.frame is not None:
                sent_frame = S.frame_seq
                await sock.send_bytes(await asyncio.to_thread(jpeg, S.frame, 960, 75))
            await asyncio.sleep(1 / 15)
    except (WebSocketDisconnect, RuntimeError):
        pass


async def poll_server():
    while True:
        if not SERVER.busy:
            await asyncio.to_thread(SERVER.status)
        await asyncio.sleep(3)


@contextlib.asynccontextmanager
async def lifespan(app):
    task = asyncio.get_running_loop().create_task(poll_server())
    threading.Thread(target=check_keys, daemon=True).start()
    yield
    task.cancel()
    await asyncio.to_thread(shutdown)


def shutdown():
    if recording():
        print('Window closing during an evaluation: finishing the run so the arms return to zero…')
        PROMPTS.closing = True
        PROMPTS.finish_now.set()
        PROMPTS.reply(None)
        RECORDER['thread'].join(120)
    PREVIEW.stop()
    SERVER.stop()


def main():
    global ARGS, CONFIG, CLAUDE_ENV
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--config', default=str(CLIENT / 'configs/robot.yam-joint-llm.yaml'))
    p.add_argument('--server-dir', type=Path, default=Path.home() / 'colosseum-policy-server')
    p.add_argument('--server-config', default='local/local-runtime-yam-agent-joint.yaml',
                   help='Policy Server config, relative to --server-dir')
    p.add_argument('--llm-keys', type=Path, default=CLIENT / 'local/llm-keys.env')
    p.add_argument('--claude-env', type=Path, default=CLIENT / 'local/claude-account.env',
                   help='env for the Claude Code login, used for Claude when no Anthropic key is saved')
    p.add_argument('--port', type=int, default=8770)
    p.add_argument('--offline', action='store_true', help='start in offline mode (no Router, nothing uploaded)')
    ARGS = p.parse_args()
    OFFLINE[0] = ARGS.offline
    CONFIG = RobotClientConfig.from_yaml(ARGS.config)
    CLAUDE_ENV = source_env(ARGS.claude_env)
    env = source_env(ARGS.llm_keys)
    KEYS.update({p: env[var] for p, (_, var) in PROVIDERS.items() if env.get(var)})
    os.environ.update({PROVIDERS[p][1]: k for p, k in KEYS.items()})
    rv.CONFIG = Path(ARGS.config).resolve()
    rv.ROOT = (CLIENT / CONFIG.evaluation_dir).resolve()
    rv.CACHE = rv.ROOT.parent / 'eval_review_cache'
    offline.install(rv.ROOT, ARGS.server_dir / ARGS.server_config)
    os.chdir(CLIENT)
    sys.stdout, sys.stderr = Tee(sys.stdout), Tee(sys.stderr)
    install_hooks()
    print(f'Eval station: open http://127.0.0.1:{ARGS.port}  (Ctrl-C here stops it and the Policy Server)')
    uvicorn.run(app, host='127.0.0.1', port=ARGS.port, log_level='warning')


if __name__ == '__main__':
    main()
