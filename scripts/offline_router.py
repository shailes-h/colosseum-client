"""Offline mode: a stand-in for the Router when the rig has no internet.

It picks two different local policies at random from the Policy Server config, as the Router would,
and answers the client's Router calls on this computer. Nothing is sent anywhere: the eval stays in
evaluation_dir under an ev_offline_ ID, with the policy behind each side in offline.json. Cloud LLM
policies are left out because they need the internet. Any Router call this stand-in does not know
(results, video or dataset uploads) raises instead of going out.
"""
import asyncio
import json
import random
import re
import secrets
from pathlib import Path

import yaml

import colosseum_client.evaluation as ev
import colosseum_client.local_policy as lp
from colosseum_client.local_protocol import decode_control, encode_control

PREFIX = 'ev_offline_'
STATE = 'offline.json'
ACTION_DIMS = {'yam': 14, 'so101': 6}  # joint_position action size, as in evaluation.run_trial
ACTIVE = False  # set by the caller before run_evaluation: new evals (and their API) go offline
RealEvalAPI = ev.EvalAPI


def is_offline(eval_id):
    return bool(eval_id) and eval_id.startswith(PREFIX)


def policies(server_config, config):
    """(name, model) for each local policy that fits this robot, model as the Router would send it."""
    raw = yaml.safe_load(Path(server_config).read_text())
    out = []
    for m in raw.get('models') or []:
        if m.get('model_type', 'vla') != 'vla':
            continue
        if (m.get('action_space') != 'joint_position' or int(m.get('action_dim', 0)) != ACTION_DIMS.get(config.robot_type)
                or int(m.get('control_hz', 0)) != config.control_hz):
            continue
        out.append((m['name'], {k: m[k] for k in ('url', 'revision', 'action_space', 'action_dim',
                                                   'control_hz', 'max_horizon')} | {'subfolder': ''}))
    return out


def load_state(folder):
    return json.loads((folder / STATE).read_text())


def save_state(folder, state):
    folder.mkdir(parents=True, exist_ok=True)
    tmp = folder / (STATE + '.tmp')
    tmp.write_text(json.dumps(state, indent=2))
    tmp.replace(folder / STATE)


def assignment(folder):
    state = load_state(folder)
    a = state['assignment']
    return {**a, 'runs': [{**r, 'state': 'finished' if r['id'] in state['finished'] else r['state']}
                          for r in a['runs']]}


class OfflineAPI:
    """EvalAPI for offline evals: answers from local files and never opens a connection."""
    def __init__(self, config, server_config):
        self.config = config
        self.root = Path(config.evaluation_dir).resolve()
        self.server_config = server_config

    def request(self, method, path, body=None):
        if (method, path) == ('POST', '/reset'):
            return {}  # offline evals never close the Router's pending one
        if (method, path) == ('POST', '/next'):
            return self.next(body)
        m = re.fullmatch(r'/assignments/(\w+)', path)
        if method == 'GET' and m and is_offline(m.group(1)) and (self.root / m.group(1) / STATE).exists():
            return assignment(self.root / m.group(1))
        raise RuntimeError(f'Offline mode: {method} {path} needs the Router; nothing was sent')

    def upload(self, *a, **k):
        raise RuntimeError('Offline mode: nothing is uploaded')

    upload_dataset = upload

    def close(self):
        pass

    def next(self, body):
        if body['track'] != 'open' or body['inference_mode'] != 'local':
            raise RuntimeError('Offline mode runs Open evals with the local Policy Server only')
        choices = policies(self.server_config, self.config)
        if len(choices) < 2:
            raise ev.NoAssignment('No pair available offline: the Policy Server config needs two local policies')
        picked = random.sample(choices, 2)
        eid = PREFIX + secrets.token_hex(12)
        runs = [{'id': 'run_' + secrets.token_hex(16), 'side': side, 'state': 'assigned', 'plans': 0} for side in 'AB']
        a = {'id': eid, 'robot_id': body['robot_id'], 'track': 'open',
             'task': {'id': '', 'robot_id': body['robot_id'], 'instruction': body['instruction'],
                      'setup': body['scene'], 'success_criteria': 'Operator A/B preference',
                      'partial_success_criteria': '0 = no progress; 100 = complete',
                      'max_steps': body['max_steps'], 'cameras': body['cameras']},
             'scene': body['scene'], 'evaluator': body['evaluator'], 'inference_mode': 'local',
             'institution': body['institution'], 'test': body['test'], 'recording': body['recording'],
             'state': 'pending', 'runs': runs, 'result': None, 'offline': True}
        save_state(self.root / eid, {'assignment': a, 'finished': [],
                                     'policies': {r['id']: {'name': name, 'model': model}
                                                  for r, (name, model) in zip(runs, picked)}})
        print(f'Offline: picked 2 of {len(choices)} local policies at random.', flush=True)
        return a


class Control:
    """The Router's end of a run's control socket: prepare, then acknowledge start, heartbeats and finish."""
    REPLY = {'heartbeat': 'heartbeat', 'finish': 'finished'}

    def __init__(self, folder, run_id):
        self.folder, self.run_id = folder, run_id
        state = load_state(folder)
        self.replies = asyncio.Queue()
        self.replies.put_nowait({'type': 'prepare', 'protocol_version': 1, 'run_id': run_id,
                                 'preparation_id': 'prep_' + secrets.token_hex(8), 'test': state['assignment']['test'],
                                 'model': state['policies'][run_id]['model'], 'task': state['assignment']['task']})

    async def recv(self):
        return encode_control(await self.replies.get())

    async def send(self, raw):
        msg = decode_control(raw)
        kind = msg.get('type')
        if kind == 'ready':
            reply = {'type': 'started', 'run_id': self.run_id}
        elif kind in self.REPLY:
            reply = {**msg, 'type': self.REPLY[kind]}
            if kind == 'finish':
                state = load_state(self.folder)
                state['finished'] = sorted(set(state['finished']) | {self.run_id})
                save_state(self.folder, state)
        else:
            reply = {'type': 'error', 'run_id': self.run_id, 'code': 'OFFLINE', 'message': f'unexpected {kind}'}
        self.replies.put_nowait(reply)

    async def close(self):
        pass


def install(evaluation_dir, server_config):
    """Route offline evals to the stand-in; online evals keep the real Router."""
    root = Path(evaluation_dir).resolve()
    ev.EvalAPI = lambda config: OfflineAPI(config, server_config) if ACTIVE else RealEvalAPI(config)
    real_connect = lp.connect

    def connect(url, *a, **k):
        # Control sockets of offline runs end here; the Policy Server connection is the real one.
        m = re.search(r'/api/eval/runs/(run_\w+)/local$', url)
        folder = m and next((f.parent for f in root.glob(PREFIX + '*/' + STATE)
                             if m.group(1) in load_state(f.parent)['policies']), None)
        if folder:
            return asyncio.sleep(0, Control(folder, m.group(1)))
        return real_connect(url, *a, **k)
    lp.connect = connect
