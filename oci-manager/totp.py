"""TOTP 两步验证（RFC 6238，SHA-1 / 6 位 / 30 秒步长）。

纯标准库实现，不引入第三方依赖。验证允许前后各一个时间窗的漂移，
兼容手机验证器时钟偏差。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import struct
import time


def generate_secret() -> str:
    """生成 Base32 编码的 160 位密钥（20 字节，标准强度）。"""
    return base64.b32encode(secrets.token_bytes(20)).decode("ascii").rstrip("=")


def _decode_secret(secret_b32: str) -> bytes:
    cleaned = secret_b32.strip().replace(" ", "").upper()
    padding = (8 - len(cleaned) % 8) % 8
    return base64.b32decode(cleaned + "=" * padding, casefold=True)


def _code_at(secret_b32: str, counter: int) -> str:
    key = _decode_secret(secret_b32)
    msg = struct.pack(">Q", counter)
    digest = hmac.new(key, msg, hashlib.sha1).digest()
    # RFC 4226 动态截断：偏移取自摘要最后一个字节的低 4 位
    offset = digest[-1] & 0x0F
    value = struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF
    return f"{value % 1000000:06d}"


def verify(secret_b32: str, code: str, window: int = 1) -> bool:
    """校验动态码，允许前后 window 个 30 秒步长的时钟漂移。"""
    cleaned = (code or "").strip().replace(" ", "")
    if not cleaned.isdigit() or len(cleaned) != 6:
        return False
    counter = int(time.time()) // 30
    return any(hmac.compare_digest(_code_at(secret_b32, counter + offset), cleaned) for offset in range(-window, window + 1))


def otpauth_uri(secret_b32: str, account: str, issuer: str = "OCI Manager") -> str:
    from urllib.parse import quote

    return (
        f"otpauth://totp/{quote(issuer)}:{quote(account)}"
        f"?secret={secret_b32}&issuer={quote(issuer)}&algorithm=SHA1&digits=6&period=30"
    )
