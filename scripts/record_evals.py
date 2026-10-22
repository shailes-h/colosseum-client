"""Record one Router A/B eval WITHOUT scoring or uploading anything; review it later in scripts/review_evals.py.

  .venv/bin/python scripts/record_evals.py configs/robot.yam-joint-llm.yaml --scene "..." [--instruction "..."]

Same flow as colosseum-robot (Router assignment, A runs, reset, B runs), but it stops before the score prompts:
no scores, preference, feedback, videos or datasets are sent. The Router only has the assignment
(instruction, scene) and the run start/finish records. Then open the review page to score and Submit or Discard.

Each invocation without --resume requests a NEW eval. The Router keeps ONE pending eval per
evaluator, so a new eval closes the previous unsubmitted one. Review and submit first if
you want to retain the previous recording.
"""
import argparse
import builtins
import json
import sys
from dataclasses import replace
from pathlib import Path

import colosseum_client.evaluation as ev
import colosseum_client.lerobot_export as lx
from colosseum_client.robot_config import RobotClientConfig


class Recorded(BaseException):  # not Exception: skips the client's "Saved locally" handler message
    pass


def waiting(root):
    """Recorded, unsubmitted evals the Router still holds (offline ones never reach it)."""
    out = []
    for m in root.glob('*/manifest.json'):
        if m.parent.name.startswith('ev_offline_'):
            continue
        try:
            d = json.loads(m.read_text())
        except Exception:
            continue
        if d.get('review') == 'pending' and not d.get('submitted') and not d.get('discarded'):
            out.append(m.parent.name)
    return out


def mark_superseded(root, old_ids, new_id):
    for old_id in old_ids:
        if old_id == new_id:
            continue
        path = root / old_id / 'manifest.json'
        d = json.loads(path.read_text())
        if d.get('review') != 'pending' or d.get('submitted') or d.get('discarded'):
            continue
        d['review'] = 'superseded'
        d['superseded_by'] = new_id
        tmp = path.with_suffix('.tmp')
        tmp.write_text(json.dumps(d, indent=2))
        tmp.replace(path)


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('config')
    p.add_argument('--instruction')
    p.add_argument('--scene')
    p.add_argument('--evaluator')
    p.add_argument('--resume', help='continue recording an eval interrupted before B finished')
    p.add_argument('--no-execute-action', action='store_true')
    a = p.parse_args()
    if a.resume and a.instruction is not None:
        p.error('--instruction starts a new eval; do not combine it with --resume')
    config = RobotClientConfig.from_yaml(a.config)
    config = replace(config, track='open', **{k: getattr(a, k) for k in ('instruction', 'scene', 'evaluator')
                                               if getattr(a, k) is not None})
    if not a.resume and not config.instruction.strip():
        instruction = builtins.input('Instruction: ').strip()
        if not instruction:
            sys.exit('Instruction cannot be empty.')
        config = replace(config, instruction=instruction)
    try:
        eval_id = record(config, resume=a.resume, execute_action=not a.no_execute_action)
    except KeyboardInterrupt:
        print('\nInterrupted. Run again and enter an instruction to start a new evaluation.')
        return
    if eval_id:
        print(f'\nRecorded {eval_id} locally. Nothing scored or uploaded.\n'
              f'Review: .venv/bin/python scripts/review_evals.py --config {a.config}  ->  http://127.0.0.1:8765')


def record(config, *, resume=None, execute_action=True, ask=builtins.input):
    """Record A then B, stopping before scoring. Returns the eval ID, or None if nothing was recorded."""
    root = Path(config.evaluation_dir).resolve()
    before = {m.parent.name for m in root.glob('*/manifest.json')}
    previous_pending = waiting(root) if not resume else []

    # Defer every scoring step: no outcome prompt, no dataset export (it embeds the outcome), stop at Preference.
    ev.progress = lambda label: None
    lx.export_trial = lambda path, *x, **k: path / 'lerobot'

    def guarded_input(prompt=''):
        if prompt.startswith(('Preference', 'Feedback')):
            raise Recorded
        return ask(prompt)
    ev.input = guarded_input  # module-level name shadows the builtin inside evaluation.py
    ev.print = lambda *x, **k: None if x and str(x[0]).startswith('LeRobot dataset saved') else builtins.print(*x, **k)

    try:
        ev.run_evaluation(config, track='open', resume=resume, execute_action=execute_action)
    except Recorded:
        pass
    finally:
        if not resume:
            created = [m for m in root.glob('*/manifest.json') if m.parent.name not in before]
            if created:
                new_id = max(created, key=lambda m: m.stat().st_mtime).parent.name
                if not new_id.startswith('ev_offline_'):  # an offline eval does not close the Router's
                    mark_superseded(root, previous_pending, new_id)
    mans = ([root / resume / 'manifest.json'] if resume else
            [m for m in root.glob('*/manifest.json') if m.parent.name not in before])
    if not mans:
        return None
    m = max(mans, key=lambda path: path.stat().st_mtime)
    d = json.loads(m.read_text())
    if d.get('submitted') or 'result' in d:
        sys.exit(f'Unexpected: {m.parent.name} has a result; not touching it.')
    d['outcomes'] = {}  # placeholders from the deferred prompts
    d['review'] = 'pending'
    tmp = m.with_suffix('.tmp')
    tmp.write_text(json.dumps(d, indent=2))
    tmp.replace(m)
    return m.parent.name

if __name__ == '__main__':
    main()
