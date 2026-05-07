from __future__ import annotations

import shlex
import socket
import subprocess
import os
import sys
import time
from dataclasses import dataclass
from typing import Any

from tenant_services import console_private_key_path


@dataclass
class Tunnel:
    key: str
    process: subprocess.Popen[Any]
    local_port: int
    command: list[str]


@dataclass
class WebProxy:
    key: str
    process: subprocess.Popen[Any]
    web_port: int
    vnc_port: int
    command: list[str]


TUNNELS: dict[str, Tunnel] = {}
WEB_PROXIES: dict[str, WebProxy] = {}
PRIVATE_KEY_PLACEHOLDERS = ("<private_key>", "<private-key>", "<privateKey>", "<privateKeyPath>")


def _free_port(preferred: int = 5900) -> int:
    for port in range(preferred, preferred + 100):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(0.2)
            if sock.connect_ex(("127.0.0.1", port)) != 0:
                return port
    raise RuntimeError("没有可用的本地 VNC 端口。")


def _split_ssh_command(command: str) -> list[str]:
    normalized = command.replace("\\\n", " ").replace("`n", " ")
    parts = shlex.split(normalized, posix=True)
    if not parts:
        raise ValueError("VNC 连接串为空。")
    if parts[0].lower().endswith("ssh.exe"):
        return parts
    if parts[0].lower() != "ssh":
        raise ValueError("VNC 连接串不是 ssh 命令。")
    return parts


def _prepare_command(vnc_connection_string: str, local_port: int, private_key_path: str) -> list[str]:
    parts = _split_ssh_command(vnc_connection_string)
    private_key_path = str(private_key_path)
    prepared: list[str] = []
    skip_next = False
    inserted_key = False
    for index, part in enumerate(parts):
        if skip_next:
            skip_next = False
            continue
        if part == "-i":
            prepared.extend(["-i", private_key_path])
            inserted_key = True
            skip_next = True
            continue
        if part.startswith("-L"):
            if part == "-L" and index + 1 < len(parts):
                prepared.extend(["-L", _rewrite_forward(parts[index + 1], local_port)])
                skip_next = True
            else:
                prepared.append("-L" + _rewrite_forward(part[2:], local_port))
            continue
        replaced = _replace_private_key_placeholder(part, private_key_path)
        if replaced != part:
            prepared.append(replaced)
            if part in PRIVATE_KEY_PLACEHOLDERS:
                inserted_key = True
            continue
        prepared.append(part.strip('"'))
    if not inserted_key:
        prepared[1:1] = ["-i", private_key_path]
    known_hosts_sink = "NUL" if os.name == "nt" else "/dev/null"
    prepared[1:1] = ["-o", "ExitOnForwardFailure=yes", "-o", "StrictHostKeyChecking=no", "-o", f"UserKnownHostsFile={known_hosts_sink}"]
    return prepared


def _replace_private_key_placeholder(value: str, private_key_path: str) -> str:
    result = value
    for placeholder in PRIVATE_KEY_PLACEHOLDERS:
        if placeholder in result:
            result = result.replace(placeholder, private_key_path)
    return result


def _rewrite_forward(value: str, local_port: int) -> str:
    clean = value.strip('"')
    parts = clean.split(":")
    if len(parts) == 4:
        return f"{local_port}:{parts[2]}:{parts[3]}"
    if len(parts) == 3:
        return f"{local_port}:{parts[1]}:{parts[2]}"
    return f"{local_port}:localhost:5900"


def start_tunnel(key: str, vnc_connection_string: str, private_key_path: str | None = None) -> dict[str, Any]:
    existing = TUNNELS.get(key)
    if existing and existing.process and existing.process.poll() is None:
        return {"local_port": existing.local_port, "already_running": True}
    local_port = _free_port(5900)
    command = _prepare_command(vnc_connection_string, local_port, private_key_path or console_private_key_path())
    creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    process = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True, creationflags=creationflags)
    time.sleep(1.2)
    if process.poll() is not None:
        error = ""
        if process.stderr:
            try:
                error = process.stderr.read().strip()
            except OSError:
                error = ""
        detail = _clean_ssh_error(error)
        raise RuntimeError(f"SSH VNC 隧道启动失败：{detail}" if detail else "SSH VNC 隧道启动失败，请确认控制台连接串和本地私钥可用。")
    TUNNELS[key] = Tunnel(key=key, process=process, local_port=local_port, command=command)
    return {"local_port": local_port, "already_running": False}


def _clean_ssh_error(error: str) -> str:
    if not error:
        return ""
    lines = [line.strip() for line in error.splitlines() if line.strip()]
    if not lines:
        return ""
    return "；".join(lines[-3:])[:420]


def start_web_vnc(key: str, vnc_connection_string: str, private_key_path: str | None = None) -> dict[str, Any]:
    tunnel = start_tunnel(key, vnc_connection_string, private_key_path)
    existing = WEB_PROXIES.get(key)
    if existing and existing.process.poll() is None:
        return {"web_port": existing.web_port, "vnc_port": existing.vnc_port, "already_running": True}
    web_port = _free_port(6080)
    vnc_port = int(tunnel["local_port"])
    command = [
        sys.executable,
        "-m",
        "websockify",
        "--idle-timeout",
        "1800",
        f"0.0.0.0:{web_port}",
        f"127.0.0.1:{vnc_port}",
    ]
    process = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(0.4)
    if process.poll() is not None:
        raise RuntimeError("websockify 启动失败，请先运行 pip install -r requirements.txt。")
    WEB_PROXIES[key] = WebProxy(key=key, process=process, web_port=web_port, vnc_port=vnc_port, command=command)
    return {"web_port": web_port, "vnc_port": vnc_port, "already_running": False}


def stop_tunnel(key: str) -> bool:
    stop_web_vnc(key)
    tunnel = TUNNELS.pop(key, None)
    if not tunnel:
        return False
    if tunnel.process.poll() is None:
        tunnel.process.terminate()
    return True


def stop_web_vnc(key: str) -> bool:
    proxy = WEB_PROXIES.pop(key, None)
    if not proxy:
        return False
    if proxy.process.poll() is None:
        proxy.process.terminate()
    return True


def tunnel_status(key: str) -> dict[str, Any]:
    tunnel = TUNNELS.get(key)
    if not tunnel:
        return {"running": False}
    running = tunnel.process.poll() is None
    return {"running": running, "local_port": tunnel.local_port}


def web_vnc_status(key: str) -> dict[str, Any]:
    proxy = WEB_PROXIES.get(key)
    if not proxy:
        return {"running": False}
    running = proxy.process.poll() is None
    return {"running": running, "web_port": proxy.web_port, "vnc_port": proxy.vnc_port}
