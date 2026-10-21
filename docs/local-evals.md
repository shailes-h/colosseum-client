# YAM evaluations with local VLAs: record A/B, review, then submit

The five VLAs (G05, GR00T, MolmoAct2, LingBot, pi05) run as local checkpoints
on this machine's GPU. The three LLM policies (GPT, Grok, Claude) run through
their APIs and the local Policy Server, using Inspect Robots with `joint_pos`
actions. Both LLMs and VLAs use the Router's 14-value `joint_position`
contract: six joints and one gripper for each arm. The Client drives both
policy types through its calibrated YAM joint driver. The Router assigns the
model and run IDs. Scores, videos and datasets are submitted only when you
click **Save + Submit** on the review page.

The former end-effector (EE) setup, where LLMs send EE targets and the Client
solves IK, is still available. See [EE control](#ee-control-instead-of-joint-control).

> [!IMPORTANT]
> Every new instruction starts a **new evaluation**. The Router allows one
> pending evaluation per evaluator, so submit a recording before starting the
> next one if you want it uploaded.

This guide assumes `~/colosseum-client` and
[`~/colosseum-policy-server`](https://github.com/frodobots-org/colosseum-policy-server)
are checked out side by side.

- Client config: `configs/robot.yam-joint-llm.yaml`, used for recording and review.
- Policy Server config: `local/local-runtime-yam-agent-joint.yaml`. It holds the
  5 VLA and 3 LLM entries. Each VLA has a `launcher` that starts its checkpoint
  on a local port (8202–8206). Each LLM has `control_mode: joint_pos`.
- Both configs share the same calibrated 12-joint bounds and `joint_max_step`.
  The Client's joint clipping applies to LLM and VLA actions alike.
- GPT, Grok and Claude use your official OpenAI, xAI and Anthropic API keys.

Both config files are git-ignored because they contain a Router token and
machine-specific paths. Create them from the tracked templates
`configs/robot.yam-joint-llm.yaml.example` (Client) and
`configs/local-runtime-yam-joint.yaml.example` (Policy Server).

A VLA worker starts **on demand** when the Router assigns it. Only one fits on
a 24 GB GPU at a time. The first step of a VLA run waits while the checkpoint
loads, for up to 10 minutes (`start_timeout_seconds: 600`). Worker logs go to
the Policy Server's `local-policy-logs/`.

> [!WARNING]
> Before a physical LLM trial, verify that the three LLM `name`, `url` and
> `revision` values in the Policy Server config exactly match the current Router
> registrations. Also check the joint limits and gripper calibration against the
> rig. Keep the robot attended, with the emergency stop within reach.

## One-time setup

### Client

1. Install the Client, YAM extra and I2RT as in [YAM setup](yam.md#install-and-configure-the-robot-host).
2. Create the config, then fill in the Router URL, token, institution, camera
   serials, CAN channels, gripper calibration (`scripts/calibrate_yam_gripper.py`),
   joint limits and `joint_max_step`:
   ```bash
   cd ~/colosseum-client
   cp configs/robot.yam-joint-llm.yaml.example configs/robot.yam-joint-llm.yaml
   ```
   Recordings go to the git-ignored `local/eval_runs` folder.
3. Put the LLM API keys in a private file:
   ```bash
   mkdir -p local && install -m 600 /dev/null local/llm-keys.env
   ```
   ```bash
   export OPENAI_API_KEY=...
   export XAI_API_KEY=...
   export ANTHROPIC_API_KEY=...
   ```
4. Install `ffmpeg`, which the review page uses to encode preview videos.

### Policy Server

1. Install the Inspect agent:
   ```bash
   cd ~/colosseum-policy-server
   uv sync --extra agent
   ```
2. Set up each VLA's worker environment and checkpoint snapshot. See the Policy
   Server's [YAM guide](https://github.com/frodobots-org/colosseum-policy-server/blob/main/docs/yam.md)
   and [LingBot guide](https://github.com/frodobots-org/colosseum-policy-server/blob/main/docs/lingbot-yam.md).
3. Create the config, then fill in the launcher paths, the joint limits and
   `joint_max_step` matching the Client config, and `robot_notes`:
   ```bash
   mkdir -p local
   cp configs/local-runtime-yam-joint.yaml.example local/local-runtime-yam-agent-joint.yaml
   ```

## Eval Station (one window instead of steps 1–5)

`scripts/eval_gui.py` runs the whole flow below from a single browser page. The page has three
tabs: **Setup** (GPU, CAN, both arms, 3 cameras, Router, AI API keys and the Policy Server, each
with a green tick), **Record** (task form with a live top-camera view, then A and B with live
frames, a step counter and a red STOP button) and **Review** (scores, a speed / smoothness / safety /
precision comparison, preference, and your observations turned into English feedback by Claude Haiku,
then Submit or Discard as in `review_evals.py`).

```bash
cd ~/colosseum-client
uv pip install --python .venv/bin/python -e '.[gui]'   # once
.venv/bin/python scripts/eval_gui.py
# open http://127.0.0.1:8770
```

- It defaults to the joint configs above. It starts the Policy Server (`--server-dir`,
  `--server-config`) and stops it when you press Ctrl-C, which frees the GPU.
- **API keys…** saves the OpenAI, xAI and Anthropic keys to `local/llm-keys.env` (owner-only).
  **Verify** checks each key with its provider, without spending tokens. The Router is offered
  only the LLMs whose key works. If a key fails, the card turns red and names it. The Anthropic
  key is used both for the Claude policy and to write feedback.
- **Bring CAN up** asks for your password in a system pop-up. **Both arms respond** reads each
  motor's registers without powering it.
- **STOP POLICY** (or Esc) ends the current run at once. Even an action already on its way is held
  back. The run is kept up to that step, the arms return to zero, and the evaluation moves on to the next
  policy. **Task done** ends a run the same way. A software button is not an emergency stop, so keep
  the physical stop within reach.
- If the return to zero fails, the page asks you to retry, or to support the arms and release power.

## 0. Check I2RT in the Client environment

```bash
cd ~/colosseum-client
.venv/bin/python -c 'from i2rt.robots.get_robot import get_yam_robot; print("I2RT ready")'
```

If a `uv sync` removed it, reinstall it as in [YAM setup](yam.md#install-and-configure-the-robot-host).

## 1. Start CAN and check the GPU (Terminal 1)

Bring up the CAN interfaces, for example with I2RT's `scripts/reset_all_can.sh`, then:

```bash
ip -br link | grep can
nvidia-smi   # should show no leftover model workers
```

> [!WARNING]
> Stop any teleoperation servers that use the same CAN channels before recording.

> [!CAUTION]
> If there are leftover workers, stop the Policy Server and recorder first.
> Then stop all GPU compute processes with SIGINT, which lets them free GPU
> memory, and check again.

```bash
nvidia-smi --query-compute-apps=pid,process_name,used_memory --format=csv,noheader
nvidia-smi --query-compute-apps=pid --format=csv,noheader | xargs -r kill -INT
sleep 5; nvidia-smi   # if one is still listed: kill -TERM <pid>, and kill -KILL <pid> only as a last resort
```

This only lists compute processes (model workers, Python), not the desktop/Xorg display.

## 2. Start the Policy Server (Terminal 2)

```bash
cd ~/colosseum-policy-server
uv run --no-sync colosseum-policy-local --config local/local-runtime-yam-agent-joint.yaml
```

## 3. Record a new evaluation (Terminal 3)

Load the LLM API keys in the terminal that launches the recorder. The Client
sends only the assigned model's key to the Policy Server.

```bash
cd ~/colosseum-client
source local/llm-keys.env          # sets OPENAI_API_KEY, XAI_API_KEY and ANTHROPIC_API_KEY
.venv/bin/python scripts/record_evals.py configs/robot.yam-joint-llm.yaml --scene '<scene>'
```

Enter the task at the `Instruction:` prompt and confirm robot initialization.
Record A, restore the scene, then record B.

Recordings go to `<evaluation_dir>/<evaluation ID>/<run ID>/`. LLM and VLA runs
both record joint actions.

## 4. Start the review server (Terminal 4)

```bash
cd ~/colosseum-client
.venv/bin/python scripts/review_evals.py --config configs/robot.yam-joint-llm.yaml
```

> [!TIP]
> Leave it open, then open <http://127.0.0.1:8765>. **Save locally** only
> writes local files. **Save + Submit to Router** submits scores and uploads
> videos and the dataset. Don't run `colosseum-robot` separately.

## 5. Review and submit

Refresh the review page, open the evaluation, watch the A/B videos, and enter
both scores (0–100), a preference, and feedback. Then choose:

- **Save locally:** keep a draft.
- **Save + Submit to Router:** publish the result and upload videos and the dataset.
- **Discard:** close the evaluation on the Router without publishing.

## Interrupted runs and upload retry

- A first VLA step that times out usually means the checkpoint is still
  loading. Check the Policy Server's `local-policy-logs/`. Once the worker is
  up, continue the same evaluation:
  ```bash
  .venv/bin/python scripts/record_evals.py configs/robot.yam-joint-llm.yaml --scene '<scene>' --resume ev_<id>
  ```
- If you want a fresh evaluation instead, rerun step 3 without `--resume`. The
  Router closes the old one.
- If you hit CUDA out of memory, stop the Policy Server with Ctrl-C, check
  `nvidia-smi`, and run `kill -INT <pid>` on any leftover worker.
- If the website is missing videos or dataset files after a submit, retry the upload:
  ```bash
  .venv/bin/colosseum-upload-dataset configs/robot.yam-joint-llm.yaml --evaluation-id ev_<id>
  ```

> [!NOTE]
> When you're done, press Ctrl-C in each terminal and confirm `nvidia-smi`
> shows the GPU is free.

## EE control instead of joint control

In the EE setup, the LLMs send end-effector targets through Inspect Robots, and
the Client converts them to joint commands with IK. VLAs are unchanged.

1. Install Inspect YAM EE control in the Client:
   ```bash
   cd ~/colosseum-client
   uv pip install --python .venv/bin/python \
     'inspect-robots @ git+https://github.com/supernoooo/inspect-robots.git@0865dcac2f38dfdd0a4768a68bf4146ef394b69f' \
     'inspect-robots-yam-eef @ git+https://github.com/supernoooo/inspect-robots.git@0865dcac2f38dfdd0a4768a68bf4146ef394b69f#subdirectory=plugins/inspect-robots-yam-eef'
   ```
2. Create `configs/robot.yam-eef-llm.yaml` from `configs/robot.yam-eef-llm.yaml.example`
   and keep its `inspect_eef` bounds. Create `configs/local-runtime-yam-local.yaml`
   from its template and keep the LLMs' EE `control_mode` and `eef_low`/`eef_high`,
   matching the Client bounds. The bounds are Inspect Robots YAM defaults, so check
   them against the rig.
3. Pass both configs explicitly:
   ```bash
   .venv/bin/python scripts/eval_gui.py --config configs/robot.yam-eef-llm.yaml \
     --server-config configs/local-runtime-yam-local.yaml
   ```
   In the separate-terminal flow, use the same two files in steps 2–4.

EE trials need action execution, so don't pass `--no-execute-action`. For LLM
EE runs, `action` is the post-IK joint command and `action.eef_target` is the EE
target.
