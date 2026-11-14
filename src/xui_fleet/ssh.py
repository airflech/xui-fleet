"""Thin paramiko wrapper: run root scripts and tunnel to a remote local port."""
from __future__ import annotations

import base64
import os
import select
import socket
import socketserver
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Dict, Iterator, Optional

import logging

import paramiko

# Failures are raised as SSHError with a readable reason; paramiko's own tracebacks only scare people.
logging.getLogger("paramiko").setLevel(logging.CRITICAL)

from .config import SSHConfig
from .state import State


class SSHError(RuntimeError):
    pass


@dataclass
class Result:
    code: int
    out: str
    err: str


class _PinHostKey(paramiko.MissingHostKeyPolicy):
    """Trust on first use, then refuse a changed key (stored in the state file)."""

    def __init__(self, state: State, name: str, host: str = "", port: int = 22):
        self.state, self.name, self.host, self.port = state, name, host, port

    def missing_host_key(self, client, hostname, key):  # noqa: D401
        seen = f"{key.get_name()} {base64.b64encode(key.asbytes()).decode()}"
        known = self.state.get(self.name, "ssh_host_key")
        also = list(self.state.get(self.name, "ssh_host_keys_extra") or [])
        if known is None:
            self.state.update(self.name, ssh_host_key=seen)
        elif known != seen and seen not in also:
            # A server has several host keys (ed25519, rsa…) and may offer another type than the one
            # remembered. That is not a new server as long as it still proves the remembered key.
            if known.split(" ", 1)[0] != key.get_name() and self.host and _offers_key(self.host, self.port, known):
                self.state.update(self.name, ssh_host_keys_extra=also + [seen])
                return
            raise SSHError(
                f"{self.name}: SSH-ключ хоста изменился. Если сервер переустановлен — "
                f"выполните `xui-fleet forget-host {self.name}` и повторите."
            )


class SSH:
    def __init__(self, name: str, host: str, cfg: SSHConfig, state: State):
        self.name, self.host, self.cfg, self.state = name, host, cfg, state
        self._client: Optional[paramiko.SSHClient] = None
        self._sudo = False

    def connect(self) -> "SSH":
        if self._client:
            return self
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(_PinHostKey(self.state, self.name, self.host, self.cfg.port or 22))
        kwargs: Dict[str, object] = dict(
            hostname=self.host,
            port=self.cfg.port or 22,
            username=self.cfg.user or "root",
            timeout=12,
            banner_timeout=15,
            auth_timeout=20,
        )
        password = self.cfg.resolve_password()
        if self.cfg.key:
            if not os.path.isfile(os.path.expanduser(self.cfg.key)):
                raise SSHError(f"{self.name}: файл SSH-ключа не найден: {self.cfg.key}")
            kwargs["key_filename"] = os.path.expanduser(self.cfg.key)
            if self.cfg.key_passphrase_env:
                kwargs["passphrase"] = os.environ.get(self.cfg.key_passphrase_env)
            kwargs["look_for_keys"] = False
        if password:
            kwargs["password"] = password
            if not self.cfg.key:
                kwargs["look_for_keys"] = False
                kwargs["allow_agent"] = False
        # Remember which path worked: a host blocked on the direct path then costs nothing next time.
        route = self.state.get(self.name, "ssh_route") or {}
        recent_default = route.get("via") == "default" and time.time() - route.get("at", 0) < ROUTE_RECHECK
        direct = None if recent_default else _direct_socket(self.host, int(kwargs["port"]), 4)  # type: ignore[arg-type]
        if direct is not None:
            kwargs["sock"] = direct
        if not recent_default and _physical_interface():
            via = "direct" if direct is not None else "default"
            if route.get("via") != via or via == "default":
                self.state.update(self.name, ssh_route={"via": via, "at": time.time()})
        try:
            client.connect(**kwargs)  # type: ignore[arg-type]
        except SSHError:
            raise
        except paramiko.AuthenticationException as exc:
            how = "пароль" if password else "ключ"
            raise SSHError(
                f"{self.name}: сервер {self.host} отклонил вход ({how}, пользователь {kwargs['username']}). "
                f"Проверьте логин и {how}; для root вход по паролю может быть запрещён в настройках SSH сервера."
            ) from exc
        except FileNotFoundError as exc:
            raise SSHError(f"{self.name}: файл SSH-ключа не найден: {self.cfg.key}") from exc
        except (socket.timeout, TimeoutError, ConnectionError, socket.gaierror, OSError) as exc:
            raise SSHError(
                f"{self.name}: сервер {self.host}:{kwargs['port']} недоступен по SSH ({exc}). "
                f"Проверьте адрес и порт."
            ) from exc
        except Exception as exc:  # paramiko raises a zoo of types
            raise SSHError(f"{self.name}: не удалось подключиться по SSH к {self.host}: {exc}") from exc
        client.get_transport().set_keepalive(20)  # type: ignore[union-attr]
        self._client = client
        self._sudo = (self.cfg.user or "root") != "root"
        if self._sudo and self.run("true", check=False).code != 0:
            raise SSHError(f"{self.name}: пользователю {self.cfg.user} нужен sudo без пароля (или подключайтесь как root)")
        return self

    def close(self) -> None:
        if self._client:
            self._client.close()
            self._client = None

    def run(self, script: str, check: bool = True, env: Optional[Dict[str, str]] = None, timeout: int = 900) -> Result:
        """Run a bash script as root. The script travels over stdin, so no quoting games."""
        assert self._client, "connect() first"
        cmd = "sudo -n bash -s" if self._sudo else "bash -s"
        exports = "".join(f"export {k}={_shq(v)}\n" for k, v in (env or {}).items())
        stdin, stdout, stderr = self._client.exec_command(cmd, timeout=timeout)
        stdin.write("set -o pipefail\n" + exports + script + "\n")
        stdin.channel.shutdown_write()
        out = stdout.read().decode("utf-8", "replace")
        err = stderr.read().decode("utf-8", "replace")
        code = stdout.channel.recv_exit_status()
        if check and code != 0:
            tail = "\n".join((err.strip() or out.strip()).splitlines()[-15:])
            raise SSHError(f"{self.name}: команда завершилась с кодом {code}:\n{tail}")
        return Result(code, out, err)

    @contextmanager
    def forward(self, remote_port: int, remote_host: str = "127.0.0.1") -> Iterator[int]:
        """Expose remote_host:remote_port (as seen from the server) on a local port."""
        assert self._client, "connect() first"
        transport = self._client.get_transport()

        class Handler(socketserver.BaseRequestHandler):
            def handle(self) -> None:  # noqa: D401
                try:
                    chan = transport.open_channel(  # type: ignore[union-attr]
                        "direct-tcpip", (remote_host, remote_port), self.request.getpeername()
                    )
                except Exception:
                    return
                try:
                    while True:
                        r, _, _ = select.select([self.request, chan], [], [], 60)
                        if self.request in r:
                            data = self.request.recv(65536)
                            if not data:
                                break
                            chan.sendall(data)
                        if chan in r:
                            data = chan.recv(65536)
                            if not data:
                                break
                            self.request.sendall(data)
                except (OSError, EOFError, socket.error):
                    pass
                finally:
                    chan.close()

        class Server(socketserver.ThreadingTCPServer):
            daemon_threads = True
            allow_reuse_address = True

        server = Server(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield server.server_address[1]
        finally:
            server.shutdown()
            server.server_close()


ROUTE_RECHECK = 1800  # seconds before the direct path is tried again for a host that needed the VPN
_IP_BOUND_IF = 25  # macOS <netinet/in.h>; Python does not export it


def _physical_interface() -> Optional[str]:
    """macOS interface of the default route that is not a VPN tunnel (utun/ipsec/ppp)."""
    if sys.platform != "darwin":
        return None
    try:
        out = subprocess.run(["netstat", "-rn", "-f", "inet"], capture_output=True, text=True, timeout=5).stdout
    except Exception:
        return None
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 4 and parts[0] == "default" and not parts[-1].startswith(("utun", "ipsec", "ppp", "tun")):
            return parts[-1]
    return None


def _direct_socket(host: str, port: int, timeout: float) -> Optional[socket.socket]:
    """Connect around a running VPN (Happ, v2RayTun…) through the physical interface.

    Returns None when there is no VPN or the direct path fails; the caller then
    connects the normal way, so this can only ever add a working route.
    """
    iface = _physical_interface()
    if not iface:
        return None
    try:
        index = socket.if_nametoindex(iface)
        addr = socket.getaddrinfo(host, port, socket.AF_INET, socket.SOCK_STREAM)[0][4]
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.IPPROTO_IP, _IP_BOUND_IF, index)
        sock.settimeout(timeout)
        sock.connect(addr)
        return sock
    except Exception:
        try:
            sock.close()  # type: ignore[possibly-undefined]
        except Exception:
            pass
        return None


def _offers_key(host: str, port: int, known: str) -> bool:
    """Does the server at host:port still hold the remembered host key (of whatever type it is)?"""
    kind = known.split(" ", 1)[0]
    types = ["rsa-sha2-512", "rsa-sha2-256"] if kind == "ssh-rsa" else [kind]
    sock = _direct_socket(host, port, 4) or socket.create_connection((host, port), 10)
    transport = paramiko.Transport(sock)
    try:
        transport.get_security_options().key_types = types
        transport.start_client(timeout=15)
        key = transport.get_remote_server_key()
        return f"{key.get_name()} {base64.b64encode(key.asbytes()).decode()}" == known
    except Exception:
        return False
    finally:
        transport.close()


def _shq(value: str) -> str:
    return "'" + str(value).replace("'", "'\\''") + "'"
