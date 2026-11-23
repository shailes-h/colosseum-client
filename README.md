# Colosseum Client

Run robot evaluations with [RoboColosseum](https://frodobots-org.github.io/robo-colosseum/) and upload the recorded results.

## 1. Install

Requirements: **Python 3.10 or newer**, **uv**, and **FFmpeg** for recording export.

```bash
git clone https://github.com/frodobots-org/colosseum-client.git
cd colosseum-client
```

## 2. Getting Started

```bash
cp configs/robot.yaml.example configs/robot.yaml
```

Edit `configs/robot.yaml` for your setup. See the
[Configuration example](configs/robot.yaml.example) for configuration details.

Install first, then run:

```bash
uv sync --extra <robot_type>
.venv/bin/colosseum-robot configs/robot.yaml
```

Or install and run in one command:

```bash
uv run --extra <robot_type> colosseum-robot configs/robot.yaml
```

Follow the prompts to enter an instruction or select a task, start the trial,
and submit the evaluation. Recordings are saved under `eval_runs/` by default.
After a successful LeRobot export, the trial's source PNGs are deleted, even
when upload is skipped. Videos, Parquet, metadata and `frames.jsonl` remain for
upload retries. Failed exports retain PNGs. The final observation without an
action retains its numeric record only; its image is not included in the dataset.

## Supported robots

| Robot | `robot_type` | Status |
| --- | --- | --- |
| [Franka](src/colosseum_client/robots/franka.py) | `franka` | Implemented |
| [SO-ARM101](src/colosseum_client/robots/so101.py) | `so101` | [LeRobot driver and setup](docs/so101.md) |
| [Bimanual YAM](src/colosseum_client/robots/yam.py) | `yam` | [I2RT driver and setup](docs/yam.md); hardware validation pending |
| [Unitree G1](src/colosseum_client/robots/g1.py) | `g1` | TODO |

[Adding a new robot](docs/robot-adapters.md)

[YAM local evaluations with VLAs and LLMs: record, review and submit](docs/local-evals.md)

### YAM Eval Station (GUI)

Once the [one-time YAM setup](docs/local-evals.md#one-time-setup) is done,
including gripper calibration with `scripts/calibrate_yam_gripper.py`, one
browser page replaces the separate terminals for the Policy Server, recording
and review:

```bash
uv pip install --python .venv/bin/python -e '.[gui]'   # once
.venv/bin/python scripts/eval_gui.py
# open http://127.0.0.1:8770
```

- **Setup** checks the GPU, CAN, both arms, the 3 cameras, the Router, the API
  keys and the Policy Server, and shows a green tick for each one that passes.
  It starts the Policy Server for you, and Ctrl-C stops it and frees the GPU.
- **Record** takes the task, then runs A and B with live camera frames, a step
  counter and a STOP button (or Esc).
- **Review** collects scores, a preference and your observations, which
  Claude Haiku turns into written feedback, then submits to the Router or
  discards the evaluation.

It defaults to joint control (`configs/robot.yam-joint-llm.yaml`). For EE
control, pass `--config` and `--server-config`; see
[EE control](docs/local-evals.md#ee-control-instead-of-joint-control). A
software STOP is not an emergency stop, so keep the physical stop within reach.

## Reference

[Evaluation flow & API reference](docs/client-reference.md)

### LLM relay credentials

The Router supplies each LLM's API URL; no Client base-URL setting is needed.
`llm_api_keys.openai`, `.xai`, `.anthropic`, and `.google` select credentials by the
assigned model name (`gpt-*`, `grok-*`, `claude-*`, `gemini-*`), including when they
all use one relay host. Use that relay's respective keys, not unrelated
official-provider keys. The Router sends Gemini as `gemini-4-hydrogen` (policyId
`gemini-4-hydrogen-yam`), but its `/next` does not accept the `google` provider yet,
so a `google` key is not offered to it; see
[Gemini](docs/local-evals.md#gemini-gemini-4-hydrogen). The Policy Server calls
Gemini through the Antigravity CLI with that key, so install `agy` on the Policy
Server machine: `curl -fsSL https://antigravity.google/cli/install.sh | bash`.
For example:

```yaml
llm_api_keys:
  openai: env:YHLXJ_OPENAI_KEY
  xai: env:YHLXJ_XAI_KEY
  anthropic: env:YHLXJ_ANTHROPIC_KEY
deadline_ms: 120000
```

`env:` resolves process environment variables; it does not load `.env` by
itself. Literal key values in a private YAML config are also supported.
The Client sends only capability names/legacy URLs to Router, never keys.
Upgrade Router before this Client: `/next` now includes `llm_providers`.
