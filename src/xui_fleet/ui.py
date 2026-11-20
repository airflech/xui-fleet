"""Local web interface: the same engine as the CLI, driven from a browser.

Listens on 127.0.0.1 only. Every API call must carry the random token that is
handed to the browser in the start URL, so other pages cannot drive it.
"""
from __future__ import annotations

import hmac
import json
import time
import os
import secrets
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, urlparse

import yaml

from . import api
from . import geo
from . import xrayconf as xc
from .config import FleetConfig, append_node, set_node_label, update_server_access
from .engine import Fleet
from .reconcile import Report
from .state import State


# Shown once on the page right after it reloads itself for a new version.
WHATS_NEW = "кнопка «Обновить» вдвое быстрее и не стирает карточки; задержка с этого компьютера меряется в обход включённого VPN (раньше VPN подменял цифры)"


def _last_backup(fleet: Any) -> int:
    """When the newest master backup was made (0: none). Only file times — the overview is asked often."""
    from . import backup as bk
    try:
        files = list(fleet.backup_dir.glob(f"{bk.PREFIX}*.tar.gz"))
        return int(max(p.stat().st_mtime for p in files)) if files else 0
    except OSError:
        return 0


def _connect(host: str, port: int, timeout: float, iface: Optional[str] = None) -> None:
    """Open and close a TCP connection; with `iface` (macOS), through that interface, around a running VPN."""
    import socket
    if not iface:
        socket.create_connection((host, port), timeout=timeout).close()
        return
    from .ssh import _IP_BOUND_IF
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.setsockopt(socket.IPPROTO_IP, _IP_BOUND_IF, socket.if_nametoindex(iface))
        sock.settimeout(timeout)
        sock.connect((host, port))
    finally:
        sock.close()


def tcp_rtt(host: str, ports: List[int], tries: int = 3, timeout: float = 2.0, iface: Optional[str] = None) -> Optional[int]:
    """Round trip from this computer to a server, ms: the median time to open a TCP connection (one round trip),
    on the first of `ports` that answers. None if none does. The ports are tried at once, so a server that
    answers on none costs one `timeout`, not one per port."""
    import statistics
    import time as _t
    if len(ports) > 1:
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(len(ports)) as pool:
            got = list(pool.map(lambda p: tcp_rtt(host, [p], tries, timeout, iface), ports))
        return next((g for g in got if g is not None), None)
    for port in ports:
        times = []
        for _ in range(tries):
            start = _t.perf_counter()
            try:
                _connect(host, port, timeout, iface)
            except OSError:
                break
            times.append((_t.perf_counter() - start) * 1000)
        if times:
            return round(statistics.median(times))
    return None


# Windows prints "время=17мс TTL=56" / "time<1ms TTL=56" (TTL in capitals, any language), Linux/macOS
# "icmp_seq=1 ttl=56 time=15.2 ms" — case matters, or Linux's "seq=1 ttl=" would read as a time.
PING_TIMES = __import__("re").compile(rb"[=<](\d+(?:[.,]\d+)?)\s?\S{0,4}?\s+TTL=|time[=<]\s?(\d+(?:\.\d+)?)\s?ms")


def parse_ping(out: bytes) -> List[float]:
    """Reply times from `ping` output, ms: Windows in any language ("время=17мс TTL=56", "time<1ms TTL=56")
    and Linux/macOS ("time=15.2 ms")."""
    return [float((a or b).replace(b",", b".")) for a, b in PING_TIMES.findall(out)]


def icmp_rtt(host: str, count: int = 3, iface: Optional[str] = None) -> Optional[int]:
    """Network round trip to `host`, ms, by the system ping (what the person would see themselves); None when
    the host does not answer pings."""
    import statistics
    import subprocess
    import sys as _sys
    args = (["ping", "-n", str(count), "-w", "1000", host] if _sys.platform == "win32"
            else ["ping", "-c", str(count), "-i", "0.2", "-W", "1000", *(["-b", iface] if iface else []), host]
            if _sys.platform == "darwin" else ["ping", "-c", str(count), "-i", "0.2", "-W", "1", host])
    try:
        out = subprocess.run(args, capture_output=True, timeout=count * 1.5 + 3,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    times = parse_ping(out)
    return round(statistics.median(times)) if times else None


CASCADE_KINDS = ("cascade", "rxh", "rhy2", "rwarp")  # on the master, leading to a node: via RU
DIRECT_KINDS = ("direct", "xhttp", "hy2", "warp")    # on a server itself (a node, or the master as a VPN)


def real_routes(master: str, nodes: List[Dict[str, Any]], inbounds: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Which ways in users really have, from the entries on the panel — what the map draws, whatever the settings
    say: an entry counts when it is switched on and goes into subscriptions. Per node: cascade (via RU), direct,
    warp (any WARP entry, direct or via RU); for the master: vpn (its own entries, traffic leaves from it) and
    warp. A node that has entries of none of these kinds is not offered to users at all."""
    from . import xrayconf as xc
    names = {n.get("id"): n.get("name") for n in nodes}
    out: Dict[str, Any] = {"master": {"vpn": False, "warp": False}, "nodes": {}}

    def node(name: str) -> Dict[str, bool]:
        return out["nodes"].setdefault(name, {"cascade": False, "direct": False, "warp": False})
    for i in inbounds:
        kind = xc.inbound_kind(str(i.get("tag", "")))
        if not kind or i.get("enable") is False or i.get("excludeFromSub"):
            continue
        k, owner = kind
        on_node = names.get(i.get("nodeId")) if i.get("nodeId") else None
        if k in CASCADE_KINDS and not i.get("nodeId"):
            node(owner)["cascade"] = True
            if k == "rwarp":
                node(owner)["warp"] = True
        elif k in DIRECT_KINDS and on_node:
            node(on_node)["direct"] = True
            if k == "warp":
                node(on_node)["warp"] = True
        elif k in DIRECT_KINDS and not i.get("nodeId") and owner == master:
            if k == "warp":
                out["master"]["warp"] = True  # its WARP entry alone (master.warp) is not the VPN role
            else:
                out["master"]["vpn"] = True
    for n in nodes:  # every registered node gets a row, even with nothing offered
        node(n.get("name"))
    return out


def route_rtts(cfg: Any) -> Dict[str, Any]:
    """From this computer (where the person is): to the RU master and straight to every foreign server, by ping —
    the time to open a TCP connection runs several ms higher (equipment on the way inspects new connections) and
    is only the fallback for a server that ignores pings. Measured in parallel, a few seconds.

    A VPN on this computer (Happ, v2RayTun… in TUN mode) answers every TCP connection itself, in 0–1 ms, and
    swallows pings: the figures would be fake. On macOS the measurement goes around it, through the Wi-Fi or
    cable interface; elsewhere a connection to an address that does not exist tells it, and then the figures are
    left out (`vpn`: true) instead of being shown."""
    from concurrent.futures import ThreadPoolExecutor
    from .ssh import _physical_interface
    iface = _physical_interface()
    targets = {"__master": (cfg.master.host, [443, cfg.ssh_for(cfg.master).port or 22])}
    for n in cfg.nodes:
        d = cfg.direct_for(n)
        targets[n.name] = (n.host, [p for p in (d.port if d.enabled else None, 443, cfg.ssh_for(n).port or 22) if p])

    targets["__fake"] = (FAKE_PROBE, [443])  # TEST-NET: nothing answers there, unless a VPN fakes it

    def one(t):
        host, ports = t
        if host == FAKE_PROBE:
            return tcp_rtt(host, ports, tries=1, timeout=1.0, iface=iface)
        # ping and the TCP fallback at once: a server that ignores pings does not wait for ping to give up
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(2) as pool:
            ping = pool.submit(icmp_rtt, host, iface=iface)
            tcp = pool.submit(tcp_rtt, host, ports, timeout=1.5, iface=iface)
            got = ping.result()
            return got if got is not None else tcp.result()
    with ThreadPoolExecutor(max(1, len(targets))) as pool:
        got = dict(zip(targets, pool.map(one, targets.values())))
    if got.pop("__fake") is not None:
        return {"master": None, "nodes": {k: None for k in got if k != "__master"}, "vpn": True}
    return {"master": got.pop("__master"), "nodes": got}


FAKE_PROBE = "192.0.2.1"


def domain_reachability(tls: Any, users: Dict[str, str]) -> Dict[str, bool]:
    """Does the subscription answer on each domain from this computer (DNS, TCP and a valid certificate)?
    This is the user's own network, so a domain blocked by the provider shows up here first."""
    import socket
    import ssl
    from concurrent.futures import ThreadPoolExecutor
    from urllib.parse import urlparse
    if getattr(tls, "mode", "") != "domain" or not tls.domain or not users:
        return {}
    port = urlparse(next(iter(users.values()))).port or 443
    domains = [tls.domain] + [d for d in tls.reserve_domains if d != tls.domain]

    def check(domain: str) -> bool:
        try:
            with socket.create_connection((domain, port), timeout=4) as raw:
                with ssl.create_default_context().wrap_socket(raw, server_hostname=domain):
                    return True
        except (OSError, ssl.SSLError):
            return False
    with ThreadPoolExecutor(len(domains)) as pool:
        return dict(zip(domains, pool.map(check, domains)))


class Job:
    """One long operation at a time; the page polls its log."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.id = 0
        self.title = ""
        self.lines: List[str] = []
        self.running = False
        self.ok: Optional[bool] = None
        self.result: Dict[str, Any] = {}
        self.finished = 0.0  # when the last operation ended (the header shows it quietly after a minute)
        self.started = 0.0
        self.scope = ""  # the profile (its fleet.yaml) the operation belongs to: history is per profile
        self.preview: Optional[List[str]] = None  # what a dry run found: the page offers to apply exactly that
        self._load()

    # The app restarts itself after an update: the finished operation must survive that, or the page
    # waits forever for a job the new process has never heard of.
    @staticmethod
    def _path():
        from .service import APP_DIR
        return APP_DIR / "last-job.json"

    def _load(self) -> None:
        try:
            d = json.loads(self._path().read_text(encoding="utf-8"))
            self.id, self.title, self.lines = int(d["id"]), d.get("title", ""), list(d.get("lines") or [])
            self.ok, self.result = d.get("ok"), d.get("result") or {}
            # written by an older version without the time: the file's own time is when it ended
            self.finished = float(d.get("finished") or self._path().stat().st_mtime)
        except Exception:
            pass

    def _save(self) -> None:
        try:
            self._path().parent.mkdir(parents=True, exist_ok=True)
            self._path().write_text(json.dumps({"id": self.id, "title": self.title, "lines": self.lines[-300:],
                                                "ok": self.ok, "result": self.result, "finished": self.finished},
                                               ensure_ascii=False), encoding="utf-8")
        except Exception:
            pass

    def start(self, title: str, fn: Callable[[Callable[[str], None]], Any], scope: str = "") -> bool:
        with self.lock:
            if self.running:
                return False
            self.id = max(self.id, self._last_archived()) + 1
            self.title, self.lines, self.running, self.ok, self.result = title, [], True, None, {}
            self.started, self.scope, self.preview = time.time(), scope, None

        def work() -> None:
            try:
                out = fn(self.lines.append)
                result: Dict[str, Any] = {}
                if isinstance(out, Report):
                    result = {"changes": len(out.changes), "warnings": out.warnings}
                    for warning in out.warnings:
                        self.lines.append("⚠ " + warning)
                    quiet = not out.changes and not [l for l in self.lines if not l.startswith("⚠")]
                    if out.failed:
                        self.lines.append(f"Не настроены: {', '.join(out.failed)}. Причина — в строках [ошибка] выше. "
                                          f"Остальное применено.")
                    else:
                        self.lines.append("Готово: изменений нет, всё уже настроено." if quiet else "Готово.")
                elif isinstance(out, dict):
                    for name, value in out.items():
                        self.lines.append(f"{name}: {value}")
                    self.lines.append("Готово.")
                elif isinstance(out, str):
                    self.lines.append(out)
                self.result = result
                self.ok = not (isinstance(out, Report) and out.failed)
            except Exception as exc:  # shown to the user, never crashes the server
                self.lines.append(f"Ошибка: {exc}")
                self.ok = False
            finally:
                self.finished = time.time()
                self._save()
                self._archive()
                self.running = False

        threading.Thread(target=work, daemon=True).start()
        return True

    # ---- history: the last operations of each profile, with their logs ----
    KEEP = 40

    @staticmethod
    def _dir() -> Path:
        from .service import APP_DIR
        return APP_DIR / "jobs"

    def _archive(self) -> None:
        try:
            d = self._dir()
            d.mkdir(parents=True, exist_ok=True)
            (d / f"{self.id:06d}.json").write_text(json.dumps(
                {"id": self.id, "title": self.title, "ok": self.ok, "started": self.started, "finished": self.finished,
                 "scope": self.scope, "lines": self.lines[-500:]}, ensure_ascii=False), encoding="utf-8")
            for old in sorted(d.glob("*.json"))[:-self.KEEP]:
                old.unlink(missing_ok=True)
        except Exception:
            pass  # history is a convenience: never fail the operation over it

    def _last_archived(self) -> int:
        try:
            names = sorted(p.stem for p in self._dir().glob("*.json"))
            return int(names[-1]) if names else 0
        except (OSError, ValueError):
            return 0

    def _entries(self, scope: str):
        try:
            files = sorted(self._dir().glob("*.json"), reverse=True)
        except OSError:
            return
        for p in files:
            try:
                d = json.loads(p.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if d.get("scope", "") == scope:
                yield d

    def history(self, scope: str) -> List[Dict[str, Any]]:
        return [{k: d.get(k) for k in ("id", "title", "ok", "started", "finished")} | {"lines": len(d.get("lines") or [])}
                for d in self._entries(scope)]

    def past(self, job_id: int, scope: str) -> Optional[Dict[str, Any]]:
        return next((d for d in self._entries(scope) if d.get("id") == job_id), None)

    def forget(self, scope: Optional[str] = None) -> None:
        """Drop the history of one profile (it was deleted), or all of it (everything was wiped)."""
        for p in list(self._dir().glob("*.json")) if self._dir().exists() else []:
            try:
                if scope is None or json.loads(p.read_text(encoding="utf-8")).get("scope", "") == scope:
                    p.unlink(missing_ok=True)
            except (OSError, ValueError):
                continue

    def view(self) -> Dict[str, Any]:
        return {"id": self.id, "title": self.title, "lines": list(self.lines), "running": self.running,
                "ok": self.ok, "result": self.result,
                "age": int(time.time() - self.finished) if self.finished and not self.running else None,
                "preview": self.preview if not self.running and self.ok else None}

    def clear(self, forget: bool = False) -> None:
        """Forget the last operation (it belongs to another profile, or to one that was just deleted).
        forget: leave no file behind either (everything was wiped off this computer)."""
        with self.lock:
            if self.running:
                return
            self.title, self.lines, self.ok, self.result = "", [], None, {}
        if forget:
            self._path().unlink(missing_ok=True)
        else:
            self._save()


class App:
    def __init__(self, config_path: Path):
        from .profiles import Profiles
        self.base = config_path  # fleet.yaml of the default profile; the others live in profiles/<id>/
        self.profiles = Profiles(config_path)
        # Held while the background sync works with a profile's files, and while a profile is switched or deleted.
        self.data_lock = threading.Lock()
        self.job = Job()
        from . import service
        self.token = service.token()  # stable, so a bookmark keeps working across restarts

    @property
    def path(self) -> Path:
        """fleet.yaml of the active profile."""
        return self.profiles.active_config()

    # ---- profiles -------------------------------------------------------
    def profile_view(self) -> Dict[str, Any]:
        items = self.profiles.list()
        return {"profiles": items, "profile": next((p for p in items if p["active"]), items[0])}

    def profile_change(self, action: str, body: Dict[str, Any]) -> Dict[str, Any]:
        """Switch, create, rename or delete a profile. Never while an operation runs or sync is busy."""
        from .profiles import ProfileError
        if self.job.running:
            raise RuntimeError("дождитесь окончания текущей операции")
        if not self.data_lock.acquire(timeout=30):
            raise RuntimeError("идёт синхронизация данных — повторите через минуту")
        try:
            if action == "switch":
                self.profiles.switch(api.req(body, "id"))
            elif action == "create":
                self.profiles.create(body.get("name"))
            elif action == "rename":
                self.profiles.rename(api.req(body, "id"), body.get("name"))
            elif action == "remove":
                pid = api.req(body, "id")
                item = next((p for p in self.profiles.list() if p["id"] == pid), None)
                if not item:
                    raise ProfileError("такого профиля нет")
                # The name typed by hand, as in GitHub's "delete repository": a click alone never wipes data.
                if " ".join(str(body.get("confirm") or "").split()).lower() != item["name"].lower():
                    raise ProfileError("для подтверждения введите название профиля точно")
                removed = str(self.profiles.config_path(pid))
                from .history import History
                History(Path(removed)).forget()
                self.profiles.remove(pid)
                self.job.forget(removed)
            elif action == "wipe":
                if str(body.get("confirm") or "").strip().lower() != "стереть":
                    raise ProfileError("для подтверждения введите слово «стереть»")
                self.profiles.wipe_all()
                self.job.forget()
                import shutil
                from .service import APP_DIR
                shutil.rmtree(APP_DIR / "history", ignore_errors=True)
            else:
                raise ValueError("неизвестная команда")
        finally:
            self.data_lock.release()
        if action != "rename":
            self.job.clear(forget=action == "wipe")
        return dict(self.profile_view(), ok=True)

    # ---- helpers --------------------------------------------------------
    def update_note(self) -> Dict[str, Any]:
        """A newer version to offer, and whether sync waits for it."""
        from . import __version__
        from . import update as upd
        from .sync import Sync
        st = upd.settings()
        avail = st["available"] if st["available"] and upd.newer(st["available"], __version__) else ""
        err = Sync(self.path).meta.get("error", "") if self.path.exists() else ""
        return {"available": avail, "autoUpdate": st["auto"], "syncWaits": "обновлени" in err and "версией" in err}

    def data_mark(self) -> str:
        """Changes when a newer version of the data arrived from another computer."""
        from .sync import Sync
        return str(Sync(self.path).meta.get("pulled_at") or "")

    def fleet(self, log: Callable[[str], None] = lambda _m: None) -> Fleet:
        fleet = Fleet(self.path, log=log)
        if self.job.running:
            fleet.purpose = self.job.title  # e.g. "Название сервера de-fra-2" becomes the backup's reason
        return fleet

    def _state(self) -> State:
        return State(self.path.parent / ".xui-fleet" / "state.json")

    # ---- read -----------------------------------------------------------
    def overview(self) -> Dict[str, Any]:
        from . import __version__
        if not self.path.exists():
            return {"configured": False, "job": self.job.view(), "app": __version__, **self.profile_view()}
        fleet = self.fleet()
        cfg, state = fleet.cfg, fleet.state
        def access(server) -> Dict[str, Any]:
            ssh = cfg.ssh_for(server)
            return {"sshUser": ssh.user or "root", "sshPort": ssh.port or 22, "sshKey": ssh.key or "",
                    "auth": "key" if ssh.key else "password",
                    "hasPassword": bool(state.get(server.name, "ssh_password") or ssh.password or ssh.password_env)}

        nodes = []
        labels = geo.display_labels(cfg.nodes, {n.name: state.get(n.name, "country") for n in cfg.nodes if state.get(n.name, "country")})
        for n in cfg.nodes:
            d, c = cfg.direct_for(n), cfg.cascade_for(n)
            nodes.append({"name": n.name, "host": n.host, "label": n.label or "",
                          "display": labels[n.name],
                          "flag": n.flag or "auto", "country": state.get(n.name, "country") or "", "hide": list(n.hide),
                          "direct": d.port if d.enabled else None, "cascade": bool(c.enabled),
                          "warp": bool(cfg.set_for(n).warp) and "warp" not in n.hide,
                          "realitySwitched": state.get(n.name, "reality_switched"),
                          # what WARP adds to each round trip from this server (measured daily), ms or None
                          "warpMs": (state.get(n.name, "warp_endpoint") or {}).get("added_ms"),
                          "prepared": bool(state.get(n.name, "api_token")), "access": access(n)})
        tls = cfg.tls_for(cfg.master)
        st = cfg.defaults.set
        template = [name for name, on in (
            ("через RU (XHTTP)", st.cascade_xhttp), ("через RU (Hysteria2)", st.cascade_hysteria2),
            ("через RU (TCP)", st.cascade_tcp), ("через RU → WARP", st.cascade_warp and st.warp),
            ("XHTTP", st.xhttp), ("Hysteria2", st.hysteria2), ("напрямую (TCP)", cfg.defaults.direct.enabled),
            ("WARP", st.warp)) if on]
        extras = ["маскировка под соседний сайт", "свой WARP"] + (["сайт-витрина"] if cfg.defaults.hardening.cover_site else [])
        return {"configured": True, "job": self.job.view(), "app": __version__, **self.profile_view(),
                "nodes": nodes, "template": template, "templateExtras": extras,
                "countries": [[iso, geo.flag(iso), nm] for iso, nm in sorted(geo.COUNTRIES.items(), key=lambda x: x[1])], "version": cfg.panel.version,
                "master": {"name": cfg.master.name, "host": cfg.master.host, "label": cfg.master.label,
                           "tls": tls.mode, "domain": tls.domain or "", "reserveDomains": list(tls.reserve_domains),
                           "addressMode": tls.address_mode, "allowedApps": list(cfg.clients.allowed_apps),
                           "vpnName": cfg.clients.vpn_name, "supportUrl": cfg.clients.support_url,
                           "access": access(cfg.master), "vpn": cfg.master.vpn, "cascades": cfg.master.cascades, "warp": cfg.master.warp,
                           "labelText": cfg.master.label or "", "flag": cfg.master.flag or "auto", "hide": list(cfg.master.hide),
                           "country": state.get(cfg.master.name, "country") or "",
                           "display": geo.display_label(cfg.master.name, cfg.master.label, cfg.master.flag,
                                                        state.get(cfg.master.name, "country")),
                           "prepared": bool(state.get(cfg.master.name, "api_token")),
                           "lastBackup": _last_backup(fleet)}}

    def live(self) -> Dict[str, Any]:
        """Live node health and subscriptions straight from the master panel."""
        if self.job.running:
            return {"busy": True}
        fleet = self.fleet()
        if not fleet.state.get(fleet.cfg.master.name, "api_token") and not fleet.cfg.master.api_url:
            return {"ready": False}
        # This computer → servers is measured here meanwhile (it does not need the master)
        from concurrent.futures import ThreadPoolExecutor
        pool = ThreadPoolExecutor(1)
        rtt_future = pool.submit(route_rtts, fleet.cfg)
        pool.shutdown(wait=False)
        nodes, inbounds = fleet.status(with_subscriptions=True)
        by_tag = {i.get("tag"): i for i in inbounds}
        rows = {}
        for n in nodes:
            cascade = by_tag.get(xc.cascade_tag(n["name"]))
            rows[n["name"]] = {
                "status": n.get("status"), "latency": n.get("latencyMs", 0),
                "panel": n.get("panelVersion", ""), "xray": n.get("xrayVersion", ""),
                "cpu": round(n.get("cpuPct") or 0), "mem": round(n.get("memPct") or 0),
                "clients": n.get("clientCount", 0), "online": n.get("onlineCount", 0),
                "cascadePort": cascade["port"] if cascade else None,
                "error": (n.get("lastError") or n.get("xrayError") or "")[:200],
            }
        activity = getattr(fleet, "last_activity", {"online": [], "lastOnline": {}})
        ms = getattr(fleet, "master_status", {}) or {}
        pct = lambda d: round(100 * d["current"] / d["total"]) if isinstance(d, dict) and d.get("total") else None
        master = {"xray": (ms.get("xray") or {}).get("state", ""), "xrayError": (ms.get("xray") or {}).get("errorMsg", "")[:200],
                  "cpu": round(ms["cpu"]) if isinstance(ms.get("cpu"), (int, float)) else None,
                  "mem": pct(ms.get("mem")), "disk": pct(ms.get("disk")), "uptime": ms.get("uptime"),
                  "panel": ms.get("panelVersion", ""), "ip": (ms.get("publicIP") or {}).get("ipv4", "")}
        users = getattr(fleet, "live_subs", None)
        if users is None:
            users = fleet.subscriptions()
        return {"ready": True, "nodes": rows, "users": users, "master": master,
                "routes": real_routes(fleet.cfg.master.name, nodes, inbounds),
                "online": activity["online"], "lastOnline": activity["lastOnline"],
                "domains": domain_reachability(fleet.cfg.tls_for(fleet.cfg.master), users),
                "rtt": dict(rtt_future.result(), fromMaster=getattr(fleet, "master_rtts", {}) or {}),
                # WARP on each server, from the background check every 5 minutes (real traffic from the master)
                "warp": __import__("xui_fleet.history", fromlist=["History"]).History(self.path).last_warp()}

    # ---- write ----------------------------------------------------------
    def setup(self, body: Dict[str, Any]) -> None:
        if self.path.exists():
            raise ValueError("сеть уже настроена")
        role = body.get("role") or "gateway"  # gateway: entry to the nodes | vpn: the server itself | both
        master: Dict[str, Any] = {"name": api.req(body, "name"), "host": api.req(body, "host"),
                                  "vpn": role in ("vpn", "both"), "cascades": role != "vpn"}
        if body.get("label") or role == "gateway":
            master["label"] = body.get("label") or "RU"
        mode = body.get("tls") or "ip"
        master["tls"] = {"mode": mode}
        if mode == "domain":
            master["tls"]["domain"] = api.req(body, "domain")
        ssh, password = api.ssh_fields(body)
        if not ssh.get("key") and not password:
            raise ValueError("введите пароль SSH")
        if ssh:
            master["ssh"] = ssh
        doc = {"version": 1, "panel": {"version": "v3.11.0"}, "master": master, "nodes": [], "users": []}
        if body.get("user"):
            doc["users"] = [{"name": body["user"]}]
        FleetConfig.model_validate(doc)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(yaml.safe_dump(doc, allow_unicode=True, sort_keys=False), encoding="utf-8")
        if password:
            self._state().update(master["name"], ssh_password=password)

    def add_node(self, body: Dict[str, Any]) -> str:
        name = api.req(body, "name")
        entry: Dict[str, Any] = {"name": name, "host": api.req(body, "host")}
        if body.get("label"):
            entry["label"] = body["label"]
        if body.get("flag") and body["flag"] != "auto":
            entry["flag"] = body["flag"]
        ssh, password = api.ssh_fields(body)
        if not ssh.get("key") and not password:
            raise ValueError("введите пароль SSH")
        if ssh:
            entry["ssh"] = ssh
        if body.get("port"):
            entry["direct"] = {"port": int(body["port"])}
        if body.get("noDirect"):
            entry.setdefault("direct", {})["enabled"] = False
        if body.get("noCascade"):
            entry["cascade"] = {"enabled": False}
        if body.get("panelAddress"):
            entry["panel_address"] = body["panelAddress"]
        if body.get("private"):
            entry["allow_private_address"] = True
        if any(n.name == name for n in self.fleet().cfg.nodes):
            raise ValueError(f"сервер с именем {name} уже есть")
        append_node(self.path, entry)
        if password:
            self._state().update(name, ssh_password=password)
        return name

    def set_access(self, body: Dict[str, Any]) -> Optional[str]:
        """Change host/SSH login of a server. Returns the node name, or None for the master."""
        name, host = api.req(body, "name"), api.req(body, "host")
        fleet = self.fleet()
        server = fleet.cfg.master if fleet.cfg.master.name == name else fleet.cfg.node(name)
        ssh, password = api.ssh_fields(body)
        if not ssh.get("key") and not password and not fleet.state.get(name, "ssh_password"):
            raise ValueError("введите пароль")
        update_server_access(self.path, name, host, ssh)
        state = self._state()
        if ssh.get("key"):
            state.drop(name, "ssh_password")
        elif password:
            state.update(name, ssh_password=password)
        if host != server.host:
            state.drop(name, "ssh_host_key")  # a different machine has a different key
        return None if server is fleet.cfg.master else name

    def remove_node(self, name: str, log: Callable[[str], None], purge: bool = False) -> Report:
        from .cli import _drop_node_from_yaml
        report = self.fleet(log).remove_node(name, purge=purge)
        _drop_node_from_yaml(self.path, name)
        return report

    def remove_master(self, body: Dict[str, Any], log: Callable[[str], None]) -> Report:
        """Delete the master: the whole network of this profile. The profile then shows the first-run screen."""
        report = self.fleet(log).remove_master(purge=bool(body.get("purge")), purge_nodes=bool(body.get("purgeNodes")),
                                               sync_data=bool(body.get("syncData")),
                                               without_backup=bool(body.get("withoutBackup")))
        with self.data_lock:  # the background sync must not bring the settings back
            self.profiles.forget_network(self.profiles.active_id())
        log("настройки сети и доступы стёрты с этого компьютера, бэкапы главного сервера сохранены")
        return report


def _page() -> bytes:
    """The page; its styles and scripts carry the code version, so a new version is never served from cache."""
    from .service import code_version
    html = resources.files("xui_fleet").joinpath("ui.html").read_text(encoding="utf-8")
    return html.replace("{{V}}", code_version()[:12]).encode()


STATIC_TYPES = {".js": "text/javascript; charset=utf-8", ".css": "text/css; charset=utf-8", ".woff2": "font/woff2",
                ".txt": "text/plain; charset=utf-8"}


def _static(name: str) -> Optional[Tuple[bytes, str]]:
    """The page's own scripts and styles. Code, not data: served without the token (a <script src> cannot send
    one), and only plain file names from the package's static folder."""
    import re
    if not re.fullmatch(r"[a-z0-9-]+\.(js|css)|fonts/[A-Za-z0-9-]+\.(woff2|txt)", name):
        return None
    f = resources.files("xui_fleet").joinpath("static")
    for part in name.split("/"):
        f = f.joinpath(part)
    if not f.is_file():
        return None
    return f.read_bytes(), STATIC_TYPES["." + name.rsplit(".", 1)[1]]


def make_handler(app: App):
    class Handler(BaseHTTPRequestHandler):
        server_version = "xui-fleet"

        def log_message(self, *_args: Any) -> None:  # keep the terminal quiet
            return

        def _send(self, code: int, payload: Any, ctype: str = "application/json; charset=utf-8") -> None:
            data = payload if isinstance(payload, bytes) else json.dumps(payload, ensure_ascii=False).encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(data)

        def _local(self) -> bool:
            host = (self.headers.get("Host") or "").split(":")[0]
            return host in ("127.0.0.1", "localhost")

        def _authed(self) -> bool:
            return self._local() and hmac.compare_digest(self.headers.get("X-Fleet-Token") or "", app.token)

        def do_GET(self) -> None:  # noqa: N802
            url = urlparse(self.path)
            if url.path == "/favicon.ico":
                # Browsers ask for it on their own, without the token. The page carries its icon inline;
                # answer "nothing here" instead of a 403 that shows up as an error in the console.
                self.send_response(204)
                self.send_header("Cache-Control", "max-age=86400")
                self.end_headers()
                return
            if url.path.startswith("/static/"):
                found = _static(url.path[len("/static/"):])
                if found is None:
                    return self._send(404, b"", "text/plain")
                return self._send(200, found[0], found[1])
            if url.path == "/":
                given = (parse_qs(url.query).get("t") or [""])[0]
                if not (self._local() and hmac.compare_digest(given, app.token)):
                    return self._send(403, "Откройте интерфейс по ссылке, которую показал xui-fleet.".encode(),
                                      "text/plain; charset=utf-8")
                return self._send(200, _page(), "text/html; charset=utf-8")
            if not self._authed():
                return self._send(403, {"error": "нет доступа"})
            if url.path == "/api/ping":
                from .service import code_version
                return self._send(200, code_version().encode(), "text/plain")
            query = {k: v[-1] for k, v in parse_qs(url.query).items()}
            self._answer("GET", url.path, query)

        def _answer(self, method: str, path: str, body: Dict[str, Any]) -> None:
            try:
                self._send(200, api.dispatch(app, method, path, body))
            except api.NotFound:
                self._send(404, {"error": "не найдено"})
            except Exception as exc:  # shown to the person; the server keeps running
                self._send(200, {"error": str(exc)})

        def do_POST(self) -> None:  # noqa: N802
            if not self._authed():
                return self._send(403, {"error": "нет доступа"})
            length = int(self.headers.get("Content-Length") or 0)
            if length > 300 * 1024 * 1024:
                return self._send(413, {"error": "файл слишком большой"})
            try:
                body = json.loads(self.rfile.read(length) or b"{}") if length else {}
                path = urlparse(self.path).path
                if path == "/api/app/export":  # a file, not JSON
                    from . import appbackup as ab
                    if app.job.running:
                        raise RuntimeError("дождитесь окончания текущей операции")
                    data = ab.export_bytes(app.path, app.fleet().backup_dir, body.get("password") or "")
                    return self._send(200, data, "application/octet-stream")
            except Exception as exc:
                return self._send(200, {"error": str(exc)})
            self._answer("POST", path, body)

    return Handler


def serve(config_path: Path, port: int = 8787, open_browser: bool = True, service_mode: bool = False) -> None:
    from . import service
    url = service.url(port)
    if not service_mode and service.running(port):
        # The background service is already up: just show it instead of starting another copy.
        print(f"Интерфейс уже работает: {url}", flush=True)
        if open_browser:
            webbrowser.open(url)
        return
    app = App(config_path.resolve())
    server = None
    for attempt in range(30 if service_mode else 1):
        try:
            server = ThreadingHTTPServer(("127.0.0.1", port), make_handler(app))
            break
        except OSError:
            if not service_mode:
                break
            time.sleep(1)  # the previous instance may still be releasing the port
    if server is None:
        raise RuntimeError(f"порт {port} занят другой программой")

    def background_backup() -> None:  # quietly, so the page opens at once
        try:
            if app.path.exists():
                fleet = app.fleet()
                if fleet.state.get(fleet.cfg.master.name, "api_token"):
                    fleet.auto_backup(reason="при запуске интерфейса (прошлому бэкапу больше 12 ч)")
        except Exception:
            pass
    threading.Thread(target=background_backup, daemon=True).start()

    def sync_loop() -> None:
        """Send local changes a few seconds after they happen; take newer data from the server on start and every minute."""
        from .sync import Sync, content_hash
        from . import update as upd
        last_pull, last_github, seen = 0.0, 0.0, None
        synced_profile: Optional[Path] = None
        while True:
            if not app.job.running and time.time() - last_github > 1800:
                last_github = time.time()
                try:  # releases on GitHub: works on every computer, with or without sync
                    note = upd.check_github()
                    if note:
                        print(note, flush=True)
                except Exception as exc:
                    print(f"проверка обновлений на GitHub не удалась: {exc}", flush=True)
            # The active profile, held for the whole round: it cannot be switched or deleted under the sync.
            if not app.job.running and app.data_lock.acquire(blocking=False):
                cp = app.path
                try:
                    if cp != synced_profile:  # another profile: its own sync, checked right away
                        synced_profile, last_pull, seen = cp, 0.0, None
                    s = Sync(cp, lambda m: print(m, flush=True))
                    if s.enabled:
                        msg = ""
                        changed = cp.exists() and s.meta.get("hash") and content_hash(cp) != s.meta["hash"]
                        if changed:
                            h = content_hash(cp)
                            if seen == h:  # unchanged for one round: the edit is finished
                                msg, seen = s.push(), None
                            else:
                                seen = h
                        elif time.time() - last_pull > 60:
                            last_pull = time.time()
                            note = s.update_app()  # a newer program first: it may be needed to read newer data
                            if note:
                                print(note, flush=True)
                            if not note.startswith("приложение обновлено"):
                                msg = s.pull()
                        if msg:
                            print(msg, flush=True)
                except Exception as exc:
                    try:
                        Sync(cp).note_error(exc)
                    except Exception:
                        pass
                    last_pull = time.time()  # do not hammer an unreachable server
                finally:
                    app.data_lock.release()
            time.sleep(5)
    threading.Thread(target=sync_loop, daemon=True).start()

    def history_loop() -> None:
        """Route health every 5 minutes and traffic counters every hour, while the app runs (see history.py)."""
        from . import history as hs
        time.sleep(20)  # let the page open first
        while True:
            try:
                if not app.job.running and app.path.exists():
                    fleet = app.fleet()
                    if fleet.state.get(fleet.cfg.master.name, "api_token") or fleet.cfg.master.api_url:
                        done = hs.record(fleet, hs.History(app.path))
                        # A server whose Reality failed twice in a row: switch its masking site by itself (at most
                        # once an hour per server), as an operation everyone sees in the log and the history.
                        for item in done:
                            if not item.startswith("reality-dead:") or app.job.running:
                                continue
                            name = item.split(":", 1)[1]
                            last = float(fleet.state.get(name, "reality_heal_at") or 0)
                            if time.time() - last < 3600:
                                continue
                            fleet.state.update(name, reality_heal_at=time.time())
                            app.job.start(f"Автозамена сайта маскировки на {name}",
                                          lambda log, n=name: app.fleet(log).heal_reality(n), scope=str(app.path))
            except Exception as exc:  # a server that does not answer is just a gap in the history
                print(f"история: {exc}", flush=True)
            time.sleep(60)
    threading.Thread(target=history_loop, daemon=True).start()

    if service_mode:
        started = service.code_version()

        def watch_code() -> None:
            # A code update restarts the service (launchd brings it back), never in the middle of an operation.
            while True:
                time.sleep(2)
                try:
                    if service.code_version() != started and not app.job.running:
                        print("код обновлён — перезапускаюсь", flush=True)
                        server.server_close()
                        # Same process, same port, fresh code: works with or without launchd.
                        service.restart_self(sys.argv[1:])
                except Exception:
                    pass
        threading.Thread(target=watch_code, daemon=True).start()

    print(f"Интерфейс xui-fleet: {url}" + ("" if service_mode else "\nОстановить: Ctrl+C"), flush=True)
    if open_browser:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
