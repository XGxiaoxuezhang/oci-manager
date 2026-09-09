from __future__ import annotations

import os
from datetime import timedelta
from pathlib import Path
from typing import Any

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("OCI_MANAGER_DATA_DIR", BASE_DIR)).resolve()
DATA_DIR.mkdir(parents=True, exist_ok=True)

CONFIG_PATH = DATA_DIR / "tenants.yaml"
AUTH_PATH = DATA_DIR / "auth.yaml"
TENANT_DIR = DATA_DIR / "tenants"
CONSOLE_KEY_DIR = DATA_DIR / "console"
AUDIT_LOG_PATH = DATA_DIR / "audit.log.jsonl"
LAUNCH_TASKS_PATH = DATA_DIR / "launch_tasks.json"
CHECKS_PATH = DATA_DIR / "checks.json"
SQLITE_PATH = DATA_DIR / "app.db"
TENANT_DIR.mkdir(parents=True, exist_ok=True)
CONSOLE_KEY_DIR.mkdir(parents=True, exist_ok=True)

DEFAULT_REGIONS = [
    ("af-casablanca-1", "Morocco West (Casablanca)"),
    ("af-johannesburg-1", "South Africa Central (Johannesburg)"),
    ("ap-batam-1", "Indonesia North (Batam)"),
    ("ap-chuncheon-1", "South Korea North (Chuncheon)"),
    ("ap-hyderabad-1", "India South (Hyderabad)"),
    ("ap-kulai-2", "Malaysia West 2 (Kulai)"),
    ("ap-melbourne-1", "Australia Southeast (Melbourne)"),
    ("ap-mumbai-1", "India West (Mumbai)"),
    ("ap-osaka-1", "Japan Central (Osaka)"),
    ("ap-seoul-1", "South Korea Central (Seoul)"),
    ("ap-singapore-1", "Singapore (Singapore)"),
    ("ap-singapore-2", "Singapore West (Singapore)"),
    ("ap-sydney-1", "Australia East (Sydney)"),
    ("ap-tokyo-1", "Japan East (Tokyo)"),
    ("ca-montreal-1", "Canada Southeast (Montreal)"),
    ("ca-toronto-1", "Canada Southeast (Toronto)"),
    ("eu-amsterdam-1", "Netherlands Northwest (Amsterdam)"),
    ("eu-frankfurt-1", "Germany Central (Frankfurt)"),
    ("eu-jovanovac-1", "Serbia Central (Jovanovac)"),
    ("eu-madrid-1", "Spain Central (Madrid)"),
    ("eu-madrid-3", "Spain Central (Madrid 3)"),
    ("eu-marseille-1", "France South (Marseille)"),
    ("eu-milan-1", "Italy Northwest (Milan)"),
    ("eu-paris-1", "France Central (Paris)"),
    ("eu-stockholm-1", "Sweden Central (Stockholm)"),
    ("eu-turin-1", "Italy North (Turin)"),
    ("eu-zurich-1", "Switzerland North (Zurich)"),
    ("il-jerusalem-1", "Israel Central (Jerusalem)"),
    ("me-abudhabi-1", "UAE Central (Abu Dhabi)"),
    ("me-dubai-1", "UAE East (Dubai)"),
    ("me-jeddah-1", "Saudi Arabia West (Jeddah)"),
    ("me-riyadh-1", "Saudi Arabia Central (Riyadh)"),
    ("mx-monterrey-1", "Mexico Northeast (Monterrey)"),
    ("mx-queretaro-1", "Mexico Central (Queretaro)"),
    ("sa-bogota-1", "Colombia Central (Bogota)"),
    ("sa-santiago-1", "Chile Central (Santiago)"),
    ("sa-saopaulo-1", "Brazil East (Sao Paulo)"),
    ("sa-valparaiso-1", "Chile West (Valparaiso)"),
    ("sa-vinhedo-1", "Brazil Southeast (Vinhedo)"),
    ("uk-cardiff-1", "UK West (Newport)"),
    ("uk-london-1", "UK South (London)"),
    ("us-ashburn-1", "US East (Ashburn)"),
    ("us-chicago-1", "US Midwest (Chicago)"),
    ("us-phoenix-1", "US West (Phoenix)"),
    ("us-sanjose-1", "US West (San Jose)"),
]

REGION_LABELS = dict(DEFAULT_REGIONS)

INSTANCE_ACTIONS = {
    "start": "START",
    "stop": "STOP",
    "softstop": "SOFTSTOP",
    "softreset": "SOFTRESET",
    "reset": "RESET",
}

LAUNCH_PRESETS: dict[str, dict[str, Any]] = {
    "amd1c1g": {
        "label": "AMD 1C1G",
        "shape": "VM.Standard.E2.1.Micro",
        "ocpus": None,
        "memory_gbs": None,
        "boot_volume_gbs": 50,
        "description": "常见 Always Free AMD 1C1G 规格。",
    },
    "arm2c12g": {
        "label": "ARM 2C12G",
        "shape": "VM.Standard.A1.Flex",
        "ocpus": 2,
        "memory_gbs": 12,
        "boot_volume_gbs": 50,
        "description": "A1 Flex 2C12G。",
    },
    "arm4c24g": {
        "label": "ARM 4C24G",
        "shape": "VM.Standard.A1.Flex",
        "ocpus": 4,
        "memory_gbs": 24,
        "boot_volume_gbs": 50,
        "description": "A1 Flex 4C24G。",
    },
}

RETRYABLE_KEYWORDS = [
    "out of host capacity",
    "outofhostcapacity",
    "capacity",
    "too many requests",
    "rate limit",
    "temporarily unavailable",
    "internalerror",
]

def env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except ValueError:
        return default


OCI_CONNECT_TIMEOUT = env_int("OCI_MANAGER_OCI_CONNECT_TIMEOUT", 8)
OCI_READ_TIMEOUT = env_int("OCI_MANAGER_OCI_READ_TIMEOUT", 30)

# 单请求上传体积上限，防止对象上传无节制地把内存和带宽吃满。
MAX_UPLOAD_MB = max(1, env_int("OCI_MANAGER_MAX_UPLOAD_MB", 100))
MAX_UPLOAD_BYTES = MAX_UPLOAD_MB * 1024 * 1024


def secret_key() -> str:
    """会话签名密钥。

    优先级：环境变量 > 落盘的随机密钥（首次启动自动生成）> 报错。
    不再回退到硬编码默认值，避免他人伪造会话 cookie。
    """
    env_value = os.environ.get("OCI_MANAGER_SECRET_KEY", "").strip()
    if env_value and env_value not in {"change-this-secret-key", "oci-manager-dev-key"}:
        return env_value
    secret_file = DATA_DIR / ".secret_key"
    if secret_file.exists():
        saved = secret_file.read_text(encoding="utf-8").strip()
        if saved:
            return saved
    import secrets as _secrets

    generated = _secrets.token_urlsafe(48)
    secret_file.write_text(generated, encoding="utf-8")
    try:
        os.chmod(secret_file, 0o600)
    except OSError:
        pass
    return generated


def env_bool(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def server_host() -> str:
    return os.environ.get("OCI_MANAGER_HOST", "0.0.0.0")


def server_port() -> int:
    try:
        return int(os.environ.get("OCI_MANAGER_PORT", "5080"))
    except ValueError:
        return 5080


def debug_enabled() -> bool:
    return env_bool("OCI_MANAGER_DEBUG", False)


def clear_proxy_enabled() -> bool:
    return env_bool("OCI_MANAGER_CLEAR_PROXY", False)


def session_lifetime() -> timedelta:
    try:
        hours = int(os.environ.get("OCI_MANAGER_SESSION_HOURS", "12"))
    except ValueError:
        hours = 12
    return timedelta(hours=max(1, hours))


def login_window_seconds() -> int:
    return max(60, env_int("OCI_MANAGER_LOGIN_WINDOW_SECONDS", 600))


def login_max_failures() -> int:
    return max(3, env_int("OCI_MANAGER_LOGIN_MAX_FAILURES", 8))


def login_lock_seconds() -> int:
    return max(60, env_int("OCI_MANAGER_LOGIN_LOCK_SECONDS", 900))


def session_cookie_secure() -> bool:
    return env_bool("OCI_MANAGER_SESSION_COOKIE_SECURE", False)


def trusted_proxy_enabled() -> bool:
    """是否信任反向代理头（X-Forwarded-For）。

    默认关闭：登录限速使用 request.remote_addr（真实来源），避免攻击者伪造
    X-Forwarded-For 绕过限速。只有部署在可信反代（nginx/traefik）之后时才应开启。
    """
    return env_bool("OCI_MANAGER_TRUSTED_PROXY", False)


def format_region(region: str) -> str:
    label = REGION_LABELS.get(region, region or "-")
    if not region:
        return "-"
    if label == region:
        return region
    return f"{label} ({region})"
