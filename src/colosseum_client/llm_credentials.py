"""BYOK credentials stay on the Client -> Policy Server connection."""
import ipaddress
import json
import os
from collections.abc import Mapping
from urllib.parse import urlsplit

API_URLS = {
    'openai': 'https://api.openai.com/v1',
    'xai': 'https://api.x.ai/v1',
    'anthropic': 'https://api.anthropic.com/v1',
    'google': 'https://generativelanguage.googleapis.com/v1beta/openai',
}
# Assigned model name prefix -> the provider whose key that model uses.
MODEL_PROVIDERS = {'gpt-': 'openai', 'grok-': 'xai', 'claude-': 'anthropic', 'gemini-': 'google'}
# Providers the Router's POST /api/eval/next accepts in llm_providers/llm_api_urls. Its schema
# (checked 2026-11-22) allows only these, so offering google there fails every request with 422.
# A google key still reaches the Policy Server for an assigned gemini-* model; add 'google' here
# once the Router accepts it.
ROUTER_PROVIDERS = ('openai', 'xai', 'anthropic')


def model_provider(name):
    """The llm_api_keys provider for an LLM model name, or None."""
    return next((provider for prefix, provider in MODEL_PROVIDERS.items()
                 if name.startswith(prefix)), None)


def resolve_keys(value):
    if not isinstance(value, Mapping) or set(value) - API_URLS.keys():
        raise ValueError('llm_api_keys must map openai, xai, anthropic, or google to a key or env:VARIABLE')
    result = {}
    for provider, key in value.items():
        if not isinstance(key, str):
            raise ValueError('llm_api_keys values must be strings')
        if key.startswith('env:'):
            key = os.environ.get(key[4:], '')
        if key.strip():
            result[provider] = key.strip()
    return result


def require_secure_policy_url(url):
    parsed = urlsplit(url)
    try:
        loopback = ipaddress.ip_address(parsed.hostname or '').is_loopback
    except ValueError:
        loopback = parsed.hostname == 'localhost'
    if parsed.scheme != 'wss' and not (parsed.scheme == 'ws' and loopback):
        raise ValueError('LLM keys require wss:// or a loopback ws:// SSH tunnel to Policy Server')


def local_preparation(preparation, keys, policy_url, *, account_logins=None,
                      use_local_action_contract=False):
    """Carry only the assigned model's local authentication; preserve Router identity."""
    model = preparation['model']
    if model.get('model_type', 'vla') != 'llm':
        return preparation
    require_secure_policy_url(policy_url)
    provider = model_provider(model.get('name', ''))
    logins = account_logins or {}
    options = {'use_local_action_contract': True} if use_local_action_contract else {}
    if provider == 'anthropic' and logins.get('anthropic') == 'claude-code':
        options['auth_method'] = 'claude-code'
        return {**preparation, 'message': json.dumps({'client_options': options})}
    key = keys.get(provider)
    if not key:
        raise ValueError('Assigned LLM has no matching Client API key')
    result = {**preparation, 'api_key': key}
    if options:
        result['message'] = json.dumps({'client_options': options})
    return result
