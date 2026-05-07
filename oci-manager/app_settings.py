from __future__ import annotations

from typing import Any

import yaml

from settings import DATA_DIR

APP_SETTINGS_PATH = DATA_DIR / "settings.yaml"

DEFAULT_APP_SETTINGS: dict[str, Any] = {
    "offline_mode": "1",
    "webhook_url": "",
    "ntfy_server": "",
    "ntfy_topic": "",
}


def load_app_settings() -> dict[str, Any]:
    if not APP_SETTINGS_PATH.exists():
        return dict(DEFAULT_APP_SETTINGS)
    try:
        data = yaml.safe_load(APP_SETTINGS_PATH.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return dict(DEFAULT_APP_SETTINGS)
    settings = dict(DEFAULT_APP_SETTINGS)
    if isinstance(data, dict):
        settings.update({key: str(value or "") for key, value in data.items() if key in settings})
    return settings


def app_setting_enabled(settings: dict[str, Any], key: str, default: bool = False) -> bool:
    value = settings.get(key)
    if value is None or value == "":
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def offline_mode_enabled() -> bool:
    return app_setting_enabled(load_app_settings(), "offline_mode", True)


def save_app_settings(settings: dict[str, Any]) -> None:
    payload = dict(DEFAULT_APP_SETTINGS)
    payload.update({key: str(settings.get(key, "") or "").strip() for key in payload})
    APP_SETTINGS_PATH.write_text(yaml.safe_dump(payload, allow_unicode=True, sort_keys=False), encoding="utf-8")
