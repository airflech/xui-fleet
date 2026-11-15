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
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import parse_qs, urlparse

import yaml

from . import geo
from . import xrayconf as xc
from .config import FleetConfig, append_node, set_node_label, update_server_access
from .engine import Fleet
from .reconcile import Report
from .state import State


# Shown once on the page right after it reloads itself for a new version.
WHATS_NEW = "профили — несколько наборов серверов и выход из профиля (кнопка в шапке)"


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
        except Exception:
            pass

    def _save(self) -> None:
        try:
            self._path().parent.mkdir(parents=True, exist_ok=True)
            self._path().write_text(json.dumps({"id": self.id, "title": self.title, "lines": self.lines[-300:],
                                                "ok": self.ok, "result": self.result}, ensure_ascii=False), encoding="utf-8")
        except Exception:
            pass

    def start(self, title: str, fn: Callable[[Callable[[str], None]], Any]) -> bool:
        with self.lock:
            if self.running:
                return False
            self.id += 1
            self.title, self.lines, self.running, self.ok, self.result = title, [], True, None, {}

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
                self._save()
                self.running = False

        threading.Thread(target=work, daemon=True).start()
        return True

    def view(self) -> Dict[str, Any]:
        return {"id": self.id, "title": self.title, "lines": list(self.lines), "running": self.running,
                "ok": self.ok, "result": self.result}

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
                self.profiles.switch(_req(body, "id"))
            elif action == "create":
                self.profiles.create(body.get("name"))
            elif action == "rename":
                self.profiles.rename(_req(body, "id"), body.get("name"))
            elif action == "remove":
                pid = _req(body, "id")
                item = next((p for p in self.profiles.list() if p["id"] == pid), None)
                if not item:
                    raise ProfileError("такого профиля нет")
                # The name typed by hand, as in GitHub's "delete repository": a click alone never wipes data.
                if " ".join(str(body.get("confirm") or "").split()).lower() != item["name"].lower():
                    raise ProfileError("для подтверждения введите название профиля точно")
                self.profiles.remove(pid)
            elif action == "wipe":
                if str(body.get("confirm") or "").strip().lower() != "стереть":
                    raise ProfileError("для подтверждения введите слово «стереть»")
                self.profiles.wipe_all()
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
                           "access": access(cfg.master), "vpn": cfg.master.vpn, "cascades": cfg.master.cascades,
                           "labelText": cfg.master.label or "", "flag": cfg.master.flag or "auto", "hide": list(cfg.master.hide),
                           "country": state.get(cfg.master.name, "country") or "",
                           "display": geo.display_label(cfg.master.name, cfg.master.label, cfg.master.flag,
                                                        state.get(cfg.master.name, "country")),
                           "prepared": bool(state.get(cfg.master.name, "api_token"))}}

    def live(self) -> Dict[str, Any]:
        """Live node health and subscriptions straight from the master panel."""
        if self.job.running:
            return {"busy": True}
        fleet = self.fleet()
        if not fleet.state.get(fleet.cfg.master.name, "api_token") and not fleet.cfg.master.api_url:
            return {"ready": False}
        nodes, inbounds = fleet.status()
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
        return {"ready": True, "nodes": rows, "users": fleet.subscriptions(), "master": master,
                "online": activity["online"], "lastOnline": activity["lastOnline"]}

    # ---- write ----------------------------------------------------------
    def setup(self, body: Dict[str, Any]) -> None:
        if self.path.exists():
            raise ValueError("сеть уже настроена")
        role = body.get("role") or "gateway"  # gateway: entry to the nodes | vpn: the server itself | both
        master: Dict[str, Any] = {"name": _req(body, "name"), "host": _req(body, "host"),
                                  "vpn": role in ("vpn", "both"), "cascades": role != "vpn"}
        if body.get("label") or role == "gateway":
            master["label"] = body.get("label") or "RU"
        mode = body.get("tls") or "ip"
        master["tls"] = {"mode": mode}
        if mode == "domain":
            master["tls"]["domain"] = _req(body, "domain")
        ssh, password = _ssh_fields(body)
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
        name = _req(body, "name")
        entry: Dict[str, Any] = {"name": name, "host": _req(body, "host")}
        if body.get("label"):
            entry["label"] = body["label"]
        if body.get("flag") and body["flag"] != "auto":
            entry["flag"] = body["flag"]
        ssh, password = _ssh_fields(body)
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
        name, host = _req(body, "name"), _req(body, "host")
        fleet = self.fleet()
        server = fleet.cfg.master if fleet.cfg.master.name == name else fleet.cfg.node(name)
        ssh, password = _ssh_fields(body)
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

    def remove_node(self, name: str, log: Callable[[str], None]) -> Report:
        from .cli import _drop_node_from_yaml
        report = self.fleet(log).remove_node(name)
        _drop_node_from_yaml(self.path, name)
        return report


def _req(body: Dict[str, Any], key: str) -> str:
    value = str(body.get(key) or "").strip()
    if not value:
        raise ValueError(f"не заполнено поле: {key}")
    return value


def _ssh_fields(body: Dict[str, Any]):
    ssh: Dict[str, Any] = {}
    if body.get("sshUser"):
        ssh["user"] = body["sshUser"]
    if body.get("sshPort"):
        ssh["port"] = int(body["sshPort"])
    if body.get("sshKey"):
        ssh["key"] = body["sshKey"]
    return ssh, (body.get("sshPassword") or None)


def _page() -> bytes:
    return resources.files("xui_fleet").joinpath("ui.html").read_bytes()


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
            if url.path == "/":
                given = (parse_qs(url.query).get("t") or [""])[0]
                if not (self._local() and hmac.compare_digest(given, app.token)):
                    return self._send(403, "Откройте интерфейс по ссылке, которую показал xui-fleet.".encode(),
                                      "text/plain; charset=utf-8")
                return self._send(200, _page(), "text/html; charset=utf-8")
            if not self._authed():
                return self._send(403, {"error": "нет доступа"})
            try:
                if url.path == "/api/overview":
                    return self._send(200, app.overview())
                if url.path == "/api/live":
                    return self._send(200, app.live())
                if url.path == "/api/versions":
                    from . import __version__
                    from . import update as upd
                    st = upd.settings()
                    out = {"current": __version__, "channel": st["channel"], "pinned": st["pinned"], "repo": upd.REPO,
                           "auto": st["auto"],
                           "releases": [], "error": ""}
                    try:
                        rels = upd.releases(fresh=bool((parse_qs(url.query).get("fresh") or [""])[0]))
                        out["releases"] = [{k: r[k] for k in ("version", "prerelease", "date", "notes", "page")} for r in rels]
                        stable, pre = upd.latest(rels, False), upd.latest(rels, True)
                        out["latest"] = stable["version"] if stable else ""
                        out["latestPre"] = pre["version"] if pre and pre["prerelease"] else ""
                    except Exception as exc:
                        out["error"] = f"не удалось получить список версий с GitHub: {exc}"
                    return self._send(200, out)
                if url.path == "/api/sync":
                    from .sync import Sync
                    cfg_servers = []
                    if app.path.exists():
                        cfg = app.fleet().cfg
                        cfg_servers = [{"name": cfg.master.name, "host": cfg.master.host, "title": "Главный сервер"}] + [
                            {"name": n.name, "host": n.host, "title": n.label or n.name} for n in cfg.nodes]
                    return self._send(200, dict(Sync(app.path).status(), servers=cfg_servers))
                if url.path in ("/api/ping", "/api/version"):
                    from .service import code_version
                    v = code_version()
                    return (self._send(200, v.encode(), "text/plain") if url.path == "/api/ping"
                            else self._send(200, {"version": v, "app": __import__("xui_fleet").__version__,
                                                  "whatsNew": WHATS_NEW, "data": app.data_mark(), **app.update_note()}))
                if url.path == "/api/panels":
                    if app.job.running:
                        return self._send(200, {"busy": True})
                    return self._send(200, {"panels": app.fleet().panels()})
                if url.path == "/api/backups":
                    fleet = app.fleet()
                    return self._send(200, {"backups": [b.view() for b in fleet.backups()], "dir": str(fleet.backup_dir)})
                if url.path == "/api/job":
                    return self._send(200, app.job.view())
            except Exception as exc:
                return self._send(200, {"error": str(exc)})
            self._send(404, {"error": "не найдено"})

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
                self._send(200, self._post(path, body))
            except Exception as exc:
                self._send(200, {"error": str(exc)})

        def _sync_post(self, path: str, body: Dict[str, Any], run: Callable) -> Dict[str, Any]:
            from .sync import Sync
            sync = Sync(app.path)
            if path == "/api/sync/disable":
                sync.disable()
                return {"ok": True}
            if path == "/api/sync/enable":
                cfg = app.fleet().cfg
                name = _req(body, "server")
                srv = cfg.master if name == cfg.master.name else cfg.node(name)
                ssh = cfg.ssh_for(srv)
                server = {"name": srv.name, "host": srv.host, "port": ssh.port or 22, "user": ssh.user or "root"}
                password = body.get("password") or ""

                def enable(log: Callable[[str], None]) -> str:
                    s = Sync(app.path, log)
                    s.configure(server, password)
                    try:
                        s.pull()  # an existing sync on that server: join it instead of overwriting
                        msg = s.push(force=True) if not s.meta.get("rev") else "данные с сервера взяты"
                    except Exception:
                        s.disable()
                        raise
                    log(msg)
                    return f"Синхронизация включена: данные хранятся на {srv.host}, версия {s.meta.get('rev')}"
                return run("Включение синхронизации", enable)
            if path == "/api/sync/now":
                def now(log: Callable[[str], None]) -> str:
                    s = Sync(app.path, log)
                    msgs = [m for m in (s.pull(), s.push()) if m]
                    for m in msgs:
                        log(m)
                    return msgs[-1] if msgs else "всё уже совпадает"
                return run("Синхронизация", now)
            if path == "/api/sync/join":
                ssh, password = _ssh_fields(body)
                server = {"name": "sync", "host": _req(body, "host"), "port": ssh.get("port", 22), "user": ssh.get("user", "root"),
                          "ssh_password": password or ""}
                if ssh.get("key"):
                    server["key"] = ssh["key"]
                if app.job.running:
                    raise RuntimeError("дождитесь окончания текущей операции")
                msg = sync.join(server, body.get("password") or "")
                try:  # the server is one of ours: remember its name so its saved access is used later
                    cfg = app.fleet().cfg
                    hit = next((x for x in [cfg.master] + list(cfg.nodes) if x.host == server["host"]), None)
                    if hit:
                        sync.meta["server"]["name"] = hit.name
                        sync._save()
                except Exception:
                    pass
                return {"ok": True, "message": msg}
            raise ValueError("неизвестная команда")

        def _post(self, path: str, body: Dict[str, Any]) -> Dict[str, Any]:
            def run(title: str, fn: Callable[[Callable[[str], None]], Any]) -> Dict[str, Any]:
                if not app.job.start(title, fn):
                    raise RuntimeError("уже выполняется другая операция — дождитесь её окончания")
                return {"ok": True}

            if path.startswith("/api/profile/"):
                return app.profile_change(path[len("/api/profile/"):], body)
            if path == "/api/setup":
                app.setup(body)
                return run("Настройка главного сервера", lambda log: app.fleet(log).apply())
            if path == "/api/access":
                if app.job.running:
                    raise RuntimeError("уже выполняется другая операция — дождитесь её окончания")
                node = app.set_access(body)
                only = {node} if node else None
                return run(f"Подключение к серверу {body.get('name')}", lambda log: app.fleet(log).apply(only))
            if path == "/api/panel/password":
                name = _req(body, "name")
                return run(f"Новый пароль панели {name}", lambda log: app.fleet(log).reset_panel_password(name))
            if path in ("/api/app/inspect", "/api/app/import"):
                import base64
                from . import appbackup as ab
                from .service import backup_dir
                data = base64.b64decode(body.get("data") or "")
                if path == "/api/app/inspect":
                    return {"info": ab.describe(data, body.get("password") or ""), "configured": app.path.exists()}
                if app.job.running:
                    raise RuntimeError("дождитесь окончания текущей операции")
                res = ab.import_bytes(data, body.get("password") or "", app.path, backup_dir(app.path), bool(body.get("replace")))
                return {"ok": True, **res}
            if path == "/api/backups/open":
                from .service import open_folder
                folder = app.fleet().backup_dir
                folder.mkdir(parents=True, exist_ok=True)
                open_folder(folder)
                return {"ok": True}
            if path == "/api/backup":
                return run("Бэкап главного сервера", lambda log: app.fleet(log).backup_master("вручную, кнопкой в интерфейсе"))
            if path == "/api/check":
                return run("Проверка подключений", lambda log: app.fleet(log).check_links(body.get("user") or None))
            if path == "/api/migrate":
                host = _req(body, "host")
                ssh, password = _ssh_fields(body)
                if not ssh.get("key") and not password:
                    raise ValueError("введите пароль SSH нового сервера")
                return run(f"Перенос главного сервера на {host}",
                           lambda log: app.fleet(log).migrate_master(host, ssh, password, body.get("backup") or None))
            if path == "/api/domain":
                domain = _req(body, "domain")
                return run(f"Домен подписки {domain}", lambda log: app.fleet(log).set_sub_domain(domain))
            if path == "/api/domain/reserve":
                domain = (body.get("domain") or "").strip()
                return run(f"Резервный домен {domain}" if domain else "Удаление резервного домена",
                           lambda log: app.fleet(log).set_reserve_domain(domain))
            if path == "/api/master/role":
                from .config import set_master_role
                if app.job.running:
                    raise RuntimeError("уже выполняется другая операция — дождитесь её окончания")
                if not body.get("vpn") and not body.get("cascades"):
                    raise ValueError("главный сервер должен выполнять хотя бы одну роль")
                hide = [k for k in ("direct", "xhttp", "hy2", "warp") if body.get("show_" + k) is False]
                set_master_role(app.path, bool(body.get("vpn")), bool(body.get("cascades")), body.get("label"), body.get("flag"), hide)
                return run("Роль главного сервера", lambda log: app.fleet(log).apply())
            if path == "/api/versions/settings":
                from . import update as upd
                changes = {}
                if body.get("channel") in ("stable", "prerelease"):
                    changes["channel"] = body["channel"]
                if body.get("resume"):
                    changes["pinned"] = ""
                if "auto" in body:
                    changes["auto"] = bool(body["auto"])
                return {"ok": True, **upd.save_settings(**changes)}
            if path == "/api/versions/install":
                from . import update as upd
                version = _req(body, "version")
                def install(log: Callable[[str], None]) -> str:
                    log(f"скачиваю версию {version} с GitHub и проверяю контрольную сумму…")
                    v = upd.install_version(version)
                    pinned = upd.settings()["pinned"]
                    log(f"установлена версия {v}" + (" — автообновление на этом компьютере на паузе" if pinned else ""))
                    log("приложение перезапустится само через несколько секунд")
                    return f"Версия {v} установлена"
                return run(f"Установка версии {version}", install)
            if path.startswith("/api/sync/"):
                return self._sync_post(path, body, run)
            if path == "/api/apply":
                return run("Применение настроек", lambda log: app.fleet(log).apply())
            if path == "/api/plan":
                return run("Проверка (без изменений)", lambda log: app.fleet(log).apply(dry_run=True))
            if path == "/api/upgrade":
                return run("Обновление 3x-ui", lambda log: app.fleet(log).upgrade())
            if path == "/api/node/label":
                name = _req(body, "name")
                hide = [k for k in ("direct", "xhttp", "hy2", "warp", "cascade", "cascade_xhttp", "cascade_hy2", "cascade_warp")
                        if body.get("show_" + k) is False]
                set_node_label(app.path, name, body.get("label"), body.get("flag"), hide)
                return run(f"Название сервера {name}",
                           lambda log: app.fleet(log).apply({name}, skip_provision=True))
            if path == "/api/node":
                if app.job.running:
                    raise RuntimeError("уже выполняется другая операция — дождитесь её окончания")
                name = app.add_node(body)
                return run(f"Добавление сервера {name}", lambda log: app.fleet(log).apply({name}))
            if path == "/api/node/remove":
                name = _req(body, "name")
                return run(f"Удаление сервера {name}", lambda log: app.remove_node(name, log))
            if path == "/api/user":
                name = _req(body, "name")
                return run(f"Создание пользователя {name}",
                           lambda log: "Подписка создана: " + app.fleet(log).add_user(name))
            raise ValueError("неизвестная команда")

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
