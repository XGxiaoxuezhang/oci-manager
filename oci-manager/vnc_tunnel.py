from __future__ import annotations

import re
import secrets
import select
import shlex
import socket
import subprocess
import os
import sys
import threading
import time
from dataclasses import dataclass
from typing import Any

from tenant_services import console_private_key_path
from settings import server_host


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
    internal_port: int = 0
    token: str = ""
    gate: Any = None


TUNNELS: dict[str, Tunnel] = {}
WEB_PROXIES: dict[str, WebProxy] = {}
PRIVATE_KEY_PLACEHOLDERS = ("<private_key>", "<private-key>", "<privateKey>", "<privateKeyPath>")

# 这些 ssh 选项能把"在本地执行命令/读任意配置文件"的能力交给连接串的编写者。
# 注意 -o ProxyCommand 是 OCI 官方控制台连接串的正常组成部分，必须放行；
# 这里拦的是与 VNC 隧道无关、纯粹用于扩大本地执行能力的选项。
_FORBIDDEN_FLAGS = {"-F", "-J", "-W", "-E", "-R", "-D", "-S"}
_FORBIDDEN_OPTION_KEYS = (
    "localcommand",
    "permitlocalcommand",
    "proxyjump",
    "proxyusefdpass",
    "include",
    "forwardagent",
    "forwardx11",
    "forwardx11trusted",
    "dynamicforward",
    "remoteforward",
    "remotecommand",
    "permitremoteopen",
    "permitlisten",
    "controlmaster",
    "controlpath",
    "controlpersist",
)

# ssh -o 同时接受 key=value 和 ssh_config 的空格语法（-o "LocalCommand xxx"），
# 所以不能按 "=" 切分，必须按"开头连续字母数字"提取选项名。
_SSH_OPTION_KEY_RE = re.compile(r"^\s*([A-Za-z][A-Za-z0-9]*)")


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
    # 只放行 PATH 里的 ssh，禁止任何带路径分隔符的可执行文件（含 UNC 共享路径）
    if "/" in parts[0] or "\\" in parts[0] or parts[0].lower() not in {"ssh", "ssh.exe"}:
        raise ValueError("VNC 连接串必须以 ssh 命令开头，不能指定可执行文件路径。")
    return parts


def _reject_unsafe_option(option: str) -> None:
    match = _SSH_OPTION_KEY_RE.match(option)
    if not match:
        raise ValueError(f"VNC 连接串含无法识别的 SSH 选项: {option}")
    if match.group(1).lower() in _FORBIDDEN_OPTION_KEYS:
        raise ValueError(f"VNC 连接串含不允许的 SSH 选项: {option}")


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
        if part in _FORBIDDEN_FLAGS:
            raise ValueError(f"VNC 连接串含不允许的 SSH 参数: {part}")
        if part == "-o":
            option = str(parts[index + 1] if index + 1 < len(parts) else "")
            _reject_unsafe_option(option)
            prepared.extend(["-o", _replace_private_key_placeholder(option, private_key_path)])
            skip_next = True
            continue
        if part.startswith("-o") and len(part) > 2:
            _reject_unsafe_option(part[2:])
            prepared.append("-o" + _replace_private_key_placeholder(part[2:], private_key_path))
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


class _TokenGate:
    """websockify 的轻量 TCP 门禁。

    websockify 自身没有鉴权能力；门禁监听对外端口，校验 HTTP 请求行里的
    token 后才把连接转发给 127.0.0.1 上的 websockify（noVNC 的 WS 升级请求
    首行是 GET /?token=...，校验首行即可）。这样把面板暴露到 0.0.0.0 时，
    局域网内的其他进程不能绕过面板直连实例 VNC。
    """

    def __init__(self, bind_host: str, listen_port: int, target_port: int, token: str):
        self._bind_host = bind_host
        self._listen_port = listen_port
        self._target_port = target_port
        self._token = token.encode("utf-8")
        self._sock: socket.socket | None = None
        self._running = False

    def start(self) -> None:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((self._bind_host, self._listen_port))
        sock.listen(16)
        sock.settimeout(1.0)  # accept 循环定期醒来感知 stop
        self._sock = sock
        self._running = True
        threading.Thread(target=self._accept_loop, daemon=True, name=f"vnc-gate-{self._listen_port}").start()

    @property
    def bound_port(self) -> int:
        return self._listen_port

    def stop(self) -> None:
        self._running = False
        if self._sock:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None

    def _accept_loop(self) -> None:
        while self._running:
            try:
                conn, _addr = self._sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    def _handle(self, conn: socket.socket) -> None:
        upstream: socket.socket | None = None
        try:
            conn.settimeout(5)
            buf = b""
            while b"\r\n\r\n" not in buf and len(buf) < 65536:
                chunk = conn.recv(4096)
                if not chunk:
                    return
                buf += chunk
            first_line = buf.split(b"\r\n", 1)[0]
            if b"token=" not in first_line or self._token not in first_line:
                return  # 未带有效 token：静默断开
            upstream = socket.create_connection(("127.0.0.1", self._target_port), timeout=5)
            upstream.sendall(buf)
            self._relay(conn, upstream)
        except OSError:
            pass
        finally:
            for sock in (conn, upstream):
                if sock:
                    try:
                        sock.close()
                    except OSError:
                        pass

    @staticmethod
    def _relay(client: socket.socket, upstream: socket.socket) -> None:
        while True:
            readable, _, _ = select.select([client, upstream], [], [], 30)
            if not readable:
                continue
            for sock in readable:
                data = sock.recv(65536)
                if not data:
                    return
                target = upstream if sock is client else client
                target.sendall(data)


def start_web_vnc(key: str, vnc_connection_string: str, private_key_path: str | None = None) -> dict[str, Any]:
    tunnel = start_tunnel(key, vnc_connection_string, private_key_path)
    existing = WEB_PROXIES.get(key)
    if existing and existing.process.poll() is None:
        return {"web_port": existing.web_port, "vnc_port": tunnel["local_port"], "already_running": True}
    web_port = _free_port(6080)
    internal_port = _free_port(6100)
    vnc_port = int(tunnel["local_port"])
    token = secrets.token_urlsafe(16)
    # websockify 只绑 127.0.0.1，对外由带 token 校验的门禁代理暴露：
    # websockify 无鉴权，直接绑对外地址时局域网内任何进程都能直连实例 VNC。
    bind_host = server_host()
    command = [
        sys.executable,
        "-m",
        "websockify",
        "--idle-timeout",
        "1800",
        f"127.0.0.1:{internal_port}",
        f"127.0.0.1:{vnc_port}",
    ]
    process = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(0.4)
    if process.poll() is not None:
        raise RuntimeError("websockify 启动失败，请先运行 pip install -r requirements.txt。")
    gate = _TokenGate(bind_host, web_port, internal_port, token)
    gate.start()
    WEB_PROXIES[key] = WebProxy(
        key=key,
        process=process,
        web_port=web_port,
        vnc_port=vnc_port,
        command=command,
        internal_port=internal_port,
        token=token,
        gate=gate,
    )
    return {"web_port": web_port, "vnc_port": vnc_port, "token": token, "already_running": False}


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
    if proxy.gate:
        proxy.gate.stop()
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
    return {"running": running, "web_port": proxy.web_port, "vnc_port": proxy.vnc_port, "token": proxy.token}
