from __future__ import annotations

import os
import secrets
from pathlib import Path
from typing import Any

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "tenants.yaml"
AUTH_PATH = BASE_DIR / "auth.yaml"
TENANT_DIR = BASE_DIR / "tenants"
TENANT_DIR.mkdir(exist_ok=True)

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
        "description": "A1 Flex，适合先试容量。",
    },
    "arm4c24g": {
        "label": "ARM 4C24G",
        "shape": "VM.Standard.A1.Flex",
        "ocpus": 4,
        "memory_gbs": 24,
        "boot_volume_gbs": 50,
        "description": "A1 Flex，常见 ARM 4C24G 抢机规格。",
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

OCI_CONNECT_TIMEOUT = 5
OCI_READ_TIMEOUT = 15

# ── Security: allowed file types for key upload ──────────
ALLOWED_KEY_EXTENSIONS = {".pem", ".key"}
MAX_KEY_SIZE_BYTES = 8 * 1024  # 8 KB – RSA private keys are well under this


def secret_key() -> str:
    """
    Read secret key from env var OCI_MANAGER_SECRET_KEY.
    If not set, generate a random one and warn (not suitable for prod multi-process).
    """
    key = os.environ.get("OCI_MANAGER_SECRET_KEY", "")
    if not key:
        # Generate a stable key stored alongside auth.yaml so restarts don't
        # invalidate existing sessions.
        key_file = BASE_DIR / ".secret_key"
        if key_file.exists():
            key = key_file.read_text(encoding="utf-8").strip()
        if not key:
            key = secrets.token_hex(32)
            key_file.write_text(key, encoding="utf-8")
            key_file.chmod(0o600)
    return key


def format_region(region: str) -> str:
    label = REGION_LABELS.get(region, region or "-")
    if not region:
        return "-"
    if label == region:
        return region
    return f"{label} ({region})"
