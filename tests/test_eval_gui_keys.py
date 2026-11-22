"""The eval GUI needs, and offers the Router, only the keys of the LLMs its Policy Server serves."""
import importlib
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

ALL_PROVIDERS = ('openai', 'xai', 'anthropic', 'google')


@pytest.fixture
def gui(tmp_path, monkeypatch):
    if sys.version_info < (3, 12):
        pytest.skip('Existing review_evals.py uses Python 3.12 f-string syntax')
    pytest.importorskip('fastapi', reason='The GUI is an optional extra')
    pytest.importorskip('uvicorn', reason='The GUI is an optional extra')
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / 'scripts'))
    module = importlib.import_module('eval_gui')
    monkeypatch.setattr(module, 'ARGS', SimpleNamespace(server_dir=tmp_path, server_config='server.yaml'),
                        raising=False)  # main() sets it
    monkeypatch.setattr(module, 'S', module.State())
    monkeypatch.setattr(module, 'KEYS', {})
    monkeypatch.setattr(module, 'KEY_OK', set())
    monkeypatch.setattr(module, 'verify_key', lambda provider, key: (True, 'works'))
    monkeypatch.setattr(module, 'agy_installed', lambda: True)
    return module


def serve(gui, *models, gemini_endpoint=None):
    lines = ['models:']
    for name, kind in models:
        options = (f', backend_options: {{llm_endpoint: {gemini_endpoint}}}'
                   if gemini_endpoint and name.startswith('gemini-') else '')
        lines.append(f'  - {{name: {name}, model_type: {kind}{options}}}')
    (gui.ARGS.server_dir / gui.ARGS.server_config).write_text('\n'.join(lines) + '\n')


def google_row(gui):
    return next(row for row in gui.S.keys if row['provider'] == 'google')


def test_served_providers_follow_model_names(gui):
    serve(gui, ('g05-yam', 'vla'), ('grok-4.7', 'llm'), ('claude-opus-5-5-capx', 'llm'),
          ('gemini-4-hydrogen', 'llm'))
    assert gui.llm_providers_served() == {'xai', 'anthropic', 'google'}


def test_unserved_providers_stay_optional(gui):
    gui.KEYS.update({'xai': 'grok-key', 'anthropic': 'claude-key'})
    serve(gui, ('grok-4.7', 'llm'), ('claude-opus-5-5', 'llm'))
    gui.check_keys()
    assert gui.S.checks['keys']['status'] == 'ok'
    # Providers no policy uses stay listed as optional (ok None), so their key can still be added.
    assert [(row['provider'], row['ok']) for row in gui.S.keys] == [
        ('openai', None), ('xai', True), ('anthropic', True), ('google', None)]
    assert gui.KEY_OK == {'xai', 'anthropic'}


def test_missing_gemini_key_is_a_note_until_the_router_assigns_gemini(gui, monkeypatch):
    gui.KEYS.update({'xai': 'grok-key', 'anthropic': 'claude-key'})
    serve(gui, ('grok-4.7', 'llm'), ('claude-opus-5-5', 'llm'), ('gemini-4-hydrogen', 'llm'))
    gui.check_keys()  # the Router's /next does not accept google yet
    assert gui.S.checks['keys']['status'] == 'ok'
    assert google_row(gui)['ok'] is None and 'not needed yet' in google_row(gui)['msg']

    monkeypatch.setattr(gui, 'ROUTER_PROVIDERS', ALL_PROVIDERS)  # once the Router assigns Gemini
    gui.check_keys()
    assert gui.S.checks['keys']['status'] == 'fail'
    assert 'Google · Gemini key missing' in gui.S.checks['keys']['detail']


def test_gemini_through_antigravity_needs_agy(gui, monkeypatch):
    monkeypatch.setattr(gui, 'ROUTER_PROVIDERS', ALL_PROVIDERS)
    gui.KEYS.update({'anthropic': 'claude-key', 'google': 'gemini-key'})
    serve(gui, ('gemini-4-hydrogen', 'llm'), gemini_endpoint='antigravity')
    gui.check_keys()
    assert gui.S.checks['keys']['status'] == 'ok'
    assert google_row(gui)['source'] == 'API key via Antigravity CLI'
    assert gui.KEY_OK == {'google', 'anthropic'}

    monkeypatch.setattr(gui, 'agy_installed', lambda: False)
    gui.check_keys()
    assert gui.S.checks['keys']['status'] == 'fail'
    assert 'antigravity.google/cli/install.sh' in gui.S.checks['keys']['detail']
    assert gui.KEY_OK == {'anthropic'}

    serve(gui, ('gemini-4-hydrogen', 'llm'), gemini_endpoint='official')  # agy is not needed
    gui.check_keys()
    assert gui.S.checks['keys']['status'] == 'ok'


def test_gemini_key_is_saved_and_checked(gui, tmp_path, monkeypatch):
    gui.ARGS.llm_keys = tmp_path / 'llm-keys.env'
    monkeypatch.setenv('GEMINI_API_KEY', '')  # save_keys exports it; restored after the test
    gui.save_keys({'google': ' gemini-key '})
    assert gui.ARGS.llm_keys.read_text() == 'export GEMINI_API_KEY=gemini-key\n'
    serve(gui, ('gemini-4-hydrogen', 'llm'))
    gui.KEYS['anthropic'] = 'claude-key'
    gui.check_keys()
    assert gui.S.checks['keys']['status'] == 'ok'
    assert gui.KEY_OK == {'google', 'anthropic'}
