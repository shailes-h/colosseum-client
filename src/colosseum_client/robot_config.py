from __future__ import annotations

from dataclasses import dataclass, field
import re
from pathlib import Path
from typing import Any, Mapping

import yaml


@dataclass(frozen=True)
class RobotClientConfig:
    url: str
    token: str
    cameras: Mapping[str, str]
    llm_api_keys: Mapping[str, str] = field(default_factory=dict, repr=False)
    llm_account_logins: Mapping[str, str] = field(default_factory=dict)
    institution: str = ""
    scene: str = ""
    evaluator: str = ""
    track: str = ""
    policy_server_url: str = ""
    policy_ping_interval: int = 20
    policy_ping_timeout: int = 60
    prepare_timeout: int = 1800
    test: bool = False
    use_local_action_contract: bool = False
    robot_type: str = "franka"
    adapter: str | None = None
    adapter_config: Mapping[str, Any] = field(default_factory=dict)
    api_url: str = ""
    evaluation_dir: str = "eval_runs"
    recording: bool = True
    skip_upload: bool = False
    dataset_url: str = ""
    dataset_token: str = field(default="", repr=False)
    dataset_token_file: str = ""
    max_trial_steps: int = 800
    instruction: str = ""
    control_hz: int = 15
    deadline_ms: int = 30000
    image_width: int = 512
    image_height: int = 288

    @property
    def llm_providers(self):
        """Providers offered to the Router: only those its /next accepts (see ROUTER_PROVIDERS)."""
        from .llm_credentials import ROUTER_PROVIDERS
        return [p for p in dict.fromkeys([*self.llm_api_keys, *self.llm_account_logins])
                if p in ROUTER_PROVIDERS]

    @property
    def llm_api_urls(self):
        from .llm_credentials import API_URLS
        return [API_URLS[name] for name in self.llm_providers]

    def __post_init__(self) -> None:
        from .llm_credentials import resolve_keys, require_secure_policy_url
        keys = resolve_keys(self.llm_api_keys)
        if (not isinstance(self.llm_account_logins, Mapping)
                or dict(self.llm_account_logins) != {'anthropic': 'claude-code'}
                and self.llm_account_logins):
            raise ValueError('llm_account_logins supports only anthropic: claude-code')
        if set(keys) & set(self.llm_account_logins):
            raise ValueError('Choose an API key or account login for each LLM provider')
        if keys or self.llm_account_logins:
            require_secure_policy_url(self.policy_server_url)
        object.__setattr__(self, 'llm_api_keys', keys)
        for name in ('policy_ping_interval', 'policy_ping_timeout'):
            if type(getattr(self, name)) is not int or getattr(self, name) <= 0:
                raise ValueError(f'{name} must be a positive integer in seconds')
        from .hub_upload import dataset_target
        if not isinstance(self.dataset_url, str):
            raise ValueError('dataset_url must be a string')
        if self.dataset_url:
            dataset_target(self.dataset_url)
        if not isinstance(self.dataset_token, str) or not isinstance(self.dataset_token_file, str):
            raise ValueError('Dataset token and token file must be strings')
        if self.dataset_token and self.dataset_token_file:
            raise ValueError('Use either dataset_token or dataset_token_file')
        if (self.dataset_token or self.dataset_token_file) and not self.dataset_url:
            raise ValueError('Dataset credentials require dataset_url')
        if self.robot_type in {"DROID", "droid"}:
            raise ValueError("Use robot_type: franka; DROID is the driver, not a robot type")
        if self.adapter is not None and (self.adapter != "droid" or self.robot_type != "franka"):
            raise ValueError("legacy adapter is supported only as adapter: droid with robot_type: franka")

    @classmethod
    def from_yaml(cls, path: str | Path) -> "RobotClientConfig":
        with Path(path).open("r", encoding="utf-8") as config_file:
            value = yaml.safe_load(config_file)
        if not isinstance(value, dict):
            raise ValueError("robot config must be a YAML mapping")
        if "router_url" in value:
            if "url" in value and value["url"] != value["router_url"]:
                raise ValueError("url and router_url disagree")
            value["url"] = value.pop("router_url")
        raw_track = value.get("track", "")
        if type(raw_track) not in (str, int) or raw_track not in ("", 1, 2, "open", "fine-tuning"):
            raise ValueError("track must be 1 (Open) or 2 (Fine-tuning)")
        value["track"] = {1: "open", 2: "fine-tuning"}.get(raw_track, raw_track)
        if value.get("policy_server_url") is None:
            value["policy_server_url"] = ""
        if type(value.get("test", False)) is not bool:
            raise ValueError("test must be true or false")
        if type(value.get("use_local_action_contract", False)) is not bool:
            raise ValueError("use_local_action_contract must be true or false")
        if value.get("robot_type") == "test":
            value["test"] = True  # Legacy configuration remains marked synthetic.
        if value.get("test", False):
            value.setdefault("cameras", {})
        allowed = {
            "policy_ping_interval", "policy_ping_timeout",
            "institution", "test", "use_local_action_contract", "scene", "evaluator", "track", "policy_server_url", "prepare_timeout",
            "robot_type", "adapter", "adapter_config", "api_url", "evaluation_dir", "recording", "skip_upload", "max_trial_steps",
            "url", "dataset_url", "dataset_token", "dataset_token_file",
            "token", "llm_api_keys", "llm_account_logins",
            "cameras",
            "instruction",
            "control_hz",
            "deadline_ms",
            "image_width",
            "image_height",
        }
        unknown = value.keys() - allowed
        missing = {"url", "token", "cameras"} - value.keys()
        if unknown:
            raise ValueError(f"robot config contains unsupported keys: {', '.join(sorted(unknown))}")
        if missing:
            raise ValueError(f"robot config is missing: {', '.join(sorted(missing))}")
        if not isinstance(value["url"], str) or not value["url"].startswith(("ws://", "wss://")):
            raise ValueError("url must use ws:// or wss://")
        if not isinstance(value["token"], str) or not value["token"]:
            raise ValueError("token must be a non-empty string")
        cameras = value["cameras"]
        if not isinstance(cameras, dict) or (not cameras and not value.get("test", False)):
            raise ValueError("cameras must map image names to camera IDs")
        if any(not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_-]*", name) for name in cameras):
            raise ValueError("camera names must contain only letters, digits, underscores or hyphens")
        if any(not isinstance(camera_id, (str, int)) or not str(camera_id) for camera_id in cameras.values()):
            raise ValueError("camera IDs must be non-empty strings or integers")

        robot_type = value.get("robot_type", "franka")
        if not isinstance(robot_type, str) or not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_-]*", robot_type):
            raise ValueError("robot_type must be a nonempty identifier using letters, digits, underscores or hyphens")
        adapter_config = value.get("adapter_config", {})
        if not isinstance(adapter_config, dict) or any(not isinstance(key, str) for key in adapter_config):
            raise ValueError("adapter_config must be a mapping with string keys")
        adapter = value.get("adapter")
        if adapter is not None and (adapter != "droid" or robot_type != "franka"):
            raise ValueError("legacy adapter is supported only as adapter: droid with robot_type: franka")

        config = cls(
            url=value["url"],
            token=value["token"],
            llm_api_keys=value.get("llm_api_keys", {}),
            llm_account_logins=value.get("llm_account_logins", {}),
            cameras={name: str(camera_id) for name, camera_id in cameras.items()} or ({'head_image':'dummy'} if value.get('test', False) else {}),
            scene=value.get("scene", ""),
            institution=value.get("institution", ""),
            evaluator=value.get("evaluator", ""),
            track=value["track"],
            policy_server_url=value["policy_server_url"],
            policy_ping_interval=value.get("policy_ping_interval", cls.policy_ping_interval),
            policy_ping_timeout=value.get("policy_ping_timeout", cls.policy_ping_timeout),
            prepare_timeout=value.get("prepare_timeout", 1800),
            robot_type=robot_type,
            test=value.get("test", False),
            use_local_action_contract=value.get("use_local_action_contract", False),
            adapter=adapter,
            adapter_config=dict(adapter_config),
            api_url=value.get("api_url", ""),
            evaluation_dir=value.get("evaluation_dir", "eval_runs"),
            recording=value.get("recording", True),
            skip_upload=value.get("skip_upload", False),
            dataset_url=value.get("dataset_url", ""),
            dataset_token=value.get("dataset_token", ""),
            dataset_token_file=(str((Path(path).resolve().parent / Path(value["dataset_token_file"]).expanduser()).resolve())
                if isinstance(value.get("dataset_token_file"), str) and value["dataset_token_file"]
                else value.get("dataset_token_file", "")),
            max_trial_steps=value.get("max_trial_steps", cls.max_trial_steps),
            instruction=value.get("instruction", ""),
            control_hz=value.get("control_hz", 15),
            deadline_ms=value.get("deadline_ms", cls.deadline_ms),
            image_width=value.get("image_width", 512),
            image_height=value.get("image_height", 288),
        )
        if type(config.recording) is not bool:
            raise ValueError("recording must be true or false")
        if type(config.skip_upload) is not bool:
            raise ValueError("skip_upload must be true or false")
        numbers = (
            config.prepare_timeout,
            config.max_trial_steps,
            config.control_hz,
            config.deadline_ms,
            config.image_width,
            config.image_height,
        )
        if any(not isinstance(number, int) or isinstance(number, bool) or number <= 0 for number in numbers):
            raise ValueError("control_hz, deadline_ms, image_width, and image_height must be positive")
        if not isinstance(config.instruction, str):
            raise ValueError("instruction must be a string")
        if config.use_local_action_contract and not config.policy_server_url:
            raise ValueError("use_local_action_contract requires policy_server_url")
        if not all(isinstance(v, str) and v for v in (config.robot_type, config.evaluation_dir)):
            raise ValueError("robot_type and evaluation_dir must be nonempty strings")
        if not isinstance(config.api_url, str) or (config.api_url and not config.api_url.startswith(("https://", "http://"))):
            raise ValueError("api_url must use http:// or https://")
        from urllib.parse import urlsplit
        for name in ("url", "policy_server_url"):
            url = getattr(config, name)
            if name == "policy_server_url" and url == "":
                continue
            if not isinstance(url, str):
                raise ValueError(f"{name} must be a URL string")
            parts = urlsplit(url)
            if parts.scheme not in {"ws", "wss"} or not parts.hostname or parts.username or parts.password or parts.fragment:
                raise ValueError(f"{name} must be a ws:// or wss:// URL without credentials or fragment")
        if not isinstance(config.scene, str) or len(config.scene) > 4000:
            raise ValueError("scene must be a string of at most 4000 characters")
        if not isinstance(config.evaluator, str) or len(config.evaluator) > 200:
            raise ValueError("evaluator must be a string of at most 200 characters")
        if config.prepare_timeout > 1800:
            raise ValueError("prepare_timeout must not exceed 1800 seconds")
        if not isinstance(config.institution, str) or len(config.institution) > 200 or any(ord(c) < 32 for c in config.institution):
            raise ValueError('institution must be a string of at most 200 printable characters')
        return config
