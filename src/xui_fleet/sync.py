"""Keep the application's data (fleet.yaml + state) the same on every computer, through one server.

The data lives on a server the user picks (the master by default) in /root/.xui-fleet-sync, encrypted with a
sync password (the same format as a .xfleet copy) — it holds SSH passwords of every server, so a server
that gets compromised must not leak the others. Every version is numbered:

    meta.json            {"rev": 12, "hash": "...", "updated": 1794..., "os": "Windows"}  (plain, no secrets)
    current.xfleet       the latest version
    history/rev-N.xfleet the last 10 versions, so nothing is lost when two computers change things at once

A change here is pushed a few seconds later; a newer version on the server is pulled on start and every
minute. When both sides changed, the last change wins and the other one stays in the history.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import platform
import time
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from . import __version__
from . import appbackup as ab
from . import update as upd
from .config import SSHConfig
from .ssh import SSH
from .state import State

REMOTE = os.environ.get("XUI_FLEET_SYNC_REMOTE", "/root/.xui-fleet-sync")
KEEP = 10
MASTER_BACKUPS = 3  # sent along when the sync server is not the master itself


class SyncError(RuntimeError):
    pass


def _meta_path() -> Path:
    from .service import APP_DIR
    return APP_DIR / "sync.json"


def content_hash(config_path: Path) -> str:
    """What is synced, as one fingerprint: the config and the portable part of the state."""
    h = hashlib.sha256()
    if config_path.exists():
        h.update(config_path.read_bytes())
    state = config_path.parent / ".xui-fleet" / "state.json"
    if state.exists():
        h.update(json.dumps(ab.portable_state(state), sort_keys=True, ensure_ascii=False).encode())
    return h.hexdigest()[:16]


_MISSING = object()


def _named(items: Any) -> bool:
    return isinstance(items, list) and all(isinstance(i, dict) and "name" in i for i in items)


def merge3(base: Any, local: Any, remote: Any, path: str = "", conflicts: Optional[list] = None) -> Any:
    """Three-way merge of the app data, like git: each side's change against the version both last saw.

    Servers and users are matched by name, settings field by field. What only one side changed wins; something
    removed on one side and left untouched on the other is removed; a field changed on both sides keeps this
    computer's value and is reported in `conflicts`."""
    conflicts = conflicts if conflicts is not None else []
    if local == remote:
        return local
    if local == base:
        return remote
    if remote == base:
        return local
    if _named(local) and _named(remote) and (base is _MISSING or _named(base)):
        to_map = lambda items: {i["name"]: i for i in items}
        merged = merge3(to_map(base) if base is not _MISSING else _MISSING, to_map(local), to_map(remote), path, conflicts)
        return list(merged.values())
    if isinstance(local, dict) and isinstance(remote, dict):
        b = base if isinstance(base, dict) else {}
        out = {}
        for k in list(remote) + [k for k in local if k not in remote]:
            here = f"{path}.{k}" if path else str(k)
            if k in local and k in remote:
                out[k] = merge3(b.get(k, _MISSING), local[k], remote[k], here, conflicts)
            elif k in local:  # gone on the other side
                if k in b and local[k] == b[k]:
                    continue  # removed there, untouched here: remove
                out[k] = local[k]  # added here (or changed here while removed there: keep)
            else:
                if k in b and remote[k] == b[k]:
                    continue  # removed here, untouched there
                out[k] = remote[k]
        return out
    conflicts.append(path or "данные")
    return local


class Sync:
    def __init__(self, config_path: Path, log: Callable[[str], None] = lambda _m: None):
        self.config_path, self.log = config_path, log
        self.path = _meta_path()
        self.meta: Dict[str, Any] = {}
        if self.path.exists():
            try:
                self.meta = json.loads(self.path.read_text(encoding="utf-8"))
            except ValueError:
                self.meta = {}

    # ---- local settings -------------------------------------------------
    @property
    def enabled(self) -> bool:
        return bool(self.meta.get("server") and self.meta.get("password"))

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(self.path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(self.meta, fh, indent=2, ensure_ascii=False)

    def status(self) -> Dict[str, Any]:
        s = self.meta.get("server") or {}
        return {"enabled": self.enabled, "server": s.get("name", ""), "host": s.get("host", ""),
                "rev": self.meta.get("rev", 0), "syncedAt": self.meta.get("synced_at"),
                "app": __version__, "serverApp": self.meta.get("server_app", ""),
                "error": self.meta.get("error", ""), "pending": self.enabled and content_hash(self.config_path) != self.meta.get("hash")}

    def configure(self, server: Dict[str, Any], password: str) -> None:
        ab._check_password(password)
        self.meta = {"server": server, "password": password, "rev": 0, "hash": "", "device": os.urandom(6).hex()}
        self._save()

    def disable(self) -> None:
        self.meta = {}
        self.path.unlink(missing_ok=True)

    # ---- server ---------------------------------------------------------
    def _ssh(self) -> SSH:
        s = self.meta["server"]
        password = None
        if self.config_path.exists():  # the server is one of ours: its current saved access wins
            st = State(self.config_path.parent / ".xui-fleet" / "state.json")
            password = st.get(s.get("name", ""), "ssh_password")
        password = password or s.get("ssh_password")
        cfg = SSHConfig(user=s.get("user") or "root", port=s.get("port") or 22, key=s.get("key") or None, password=password)
        keys = State(self.path.with_name("sync-hostkeys.json"))
        # The key is remembered per address: a different server typed in by mistake must never be mistaken
        # for this one (and a mistyped attempt must not block the right address afterwards).
        pin = f"{s['host']}:{s.get('port') or 22}"
        try:
            return SSH(pin, s["host"], cfg, keys).connect()
        except Exception as exc:
            msg = str(exc)
            if "ключ хоста изменился" in msg:
                msg = (f"сервер {s['host']} предъявил не тот SSH-ключ, что при первом подключении. Если сервер "
                       f"переустанавливали — это нормально: удалите строку «{pin}» из файла {keys.path}. Иначе не вводите на нём пароль.")
            raise SyncError(f"сервер синхронизации {s['host']} недоступен: {msg}") from exc

    @staticmethod
    def _remote_meta(ssh: SSH) -> Dict[str, Any]:
        out = ssh.run(f"cat {REMOTE}/meta.json 2>/dev/null || echo '{{}}'", check=False).out.strip() or "{}"
        try:
            return json.loads(out)
        except ValueError:
            return {}

    def _with_backups(self) -> Optional[int]:
        """Master backups travel only when the data is kept somewhere else than the master."""
        try:
            from .config import load_config
            master = load_config(self.config_path).master
            return 0 if (self.meta["server"].get("host") == master.host) else MASTER_BACKUPS
        except Exception:
            return 0

    # ---- merging --------------------------------------------------------
    def _state_path(self) -> Path:
        return self.config_path.parent / ".xui-fleet" / "state.json"

    def _snapshot(self) -> Dict[str, Any]:
        import yaml
        cfg = yaml.safe_load(self.config_path.read_text(encoding="utf-8")) if self.config_path.exists() else {}
        st = ab.portable_state(self._state_path()) if self._state_path().exists() else {}
        return {"config": cfg or {}, "state": st or {}}

    def _base_path(self) -> Path:
        return self.path.with_name("sync-base.json")

    def _save_base(self) -> None:
        fd = os.open(str(self._base_path()), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(self._snapshot(), fh, ensure_ascii=False)

    def _load_base(self) -> Any:
        try:
            return json.loads(self._base_path().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return _MISSING

    def _merge_with(self, blob: bytes) -> str:
        """Combine the server's version (newer than the one we last saw) with this computer's changes."""
        import yaml
        from .config import FleetConfig
        z = ab.read(blob, self.meta["password"])
        remote = {"config": yaml.safe_load(z.read("fleet.yaml")) or {},
                  "state": json.loads(z.read("state.json")) if "state.json" in z.namelist() else {}}
        base, local = self._load_base(), self._snapshot()
        conflicts: list = []
        merged = merge3(base, local, remote, "", conflicts)
        FleetConfig.model_validate(merged["config"])  # never write something the app cannot load
        raw_state = json.loads(self._state_path().read_text(encoding="utf-8")) if self._state_path().exists() else {}
        for name, server in (merged["state"].get("servers") or {}).items():  # this computer's own network facts
            for k in ab.MACHINE_KEYS:
                if k in ((raw_state.get("servers") or {}).get(name) or {}):
                    server[k] = raw_state["servers"][name][k]
        self.config_path.write_text(yaml.safe_dump(merged["config"], allow_unicode=True, sort_keys=False), encoding="utf-8")
        self._state_path().parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(self._state_path()), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(merged["state"], fh, indent=2, ensure_ascii=False)
        self.meta["pulled_at"] = time.time()
        names = sorted({c.split(".")[0] if not c.startswith("config.nodes") else c for c in conflicts})
        return (" — объединено с изменениями другого компьютера" +
                (f"; в обеих версиях менялось одно и то же ({', '.join(names[:5])}) — оставлено как здесь" if conflicts else ""))

    # ---- operations -----------------------------------------------------
    def push(self, force: bool = False) -> str:
        """Send this computer's data if it changed. Returns what happened (for the log)."""
        if not self.enabled:
            return ""
        if not self.config_path.exists():
            return ""
        h = content_hash(self.config_path)
        if h == self.meta.get("hash") and not force:
            return ""
        from .service import backup_dir
        ssh = self._ssh()
        try:
            remote = self._remote_meta(ssh)
            if upd.branch(remote.get("app", "0")) > upd.branch(__version__):
                # This program may not understand that data: keep the change here until it is updated.
                raise SyncError(f"данные на сервере записаны версией {remote['app']}, а здесь {__version__} — "
                                f"изменения этого компьютера сохранены и уйдут после обновления приложения")
            note = ""
            if remote.get("rev", 0) > self.meta.get("rev", 0) and remote.get("hash") not in (self.meta.get("hash"), h):
                # Somebody changed things since we last synced: merge, never overwrite.
                data = ssh.run(f"base64 < {REMOTE}/current.xfleet | tr -d '\\n'", check=False, timeout=300).out.strip()
                note = self._merge_with(base64.b64decode(data))
                h = content_hash(self.config_path)
            blob = ab.export_bytes(self.config_path, backup_dir(), self.meta["password"], self._with_backups())
            rev = max(remote.get("rev", 0), self.meta.get("rev", 0)) + 1
            meta = {"rev": rev, "hash": h, "updated": int(time.time()), "os": platform.system(), "app": __version__}
            script = (
                f"set -e\nmkdir -p {REMOTE}/history && chmod 700 {REMOTE}\n"
                f"base64 -d > {REMOTE}/history/rev-{rev}.xfleet <<'XFSYNC'\n{base64.b64encode(blob).decode()}\nXFSYNC\n"
                f"cp {REMOTE}/history/rev-{rev}.xfleet {REMOTE}/current.xfleet.tmp && mv {REMOTE}/current.xfleet.tmp {REMOTE}/current.xfleet\n"
                f"cat > {REMOTE}/meta.json.tmp <<'XFSYNC'\n{json.dumps(meta)}\nXFSYNC\nmv {REMOTE}/meta.json.tmp {REMOTE}/meta.json\n"
                f"chmod 600 {REMOTE}/current.xfleet {REMOTE}/meta.json {REMOTE}/history/*\n"
                f"ls -1t {REMOTE}/history/rev-*.xfleet | tail -n +{KEEP + 1} | xargs -r rm -f\n"
            )
            ssh.run(script, timeout=300)
        finally:
            ssh.close()
        self.meta.update(rev=rev, hash=h, synced_at=int(time.time()), error="")
        self._save()
        self._save_base()
        return f"синхронизация: отправлена версия {rev}{note}"

    def pull(self) -> str:
        """Take a newer version from the server, if there is one. Returns what happened."""
        if not self.enabled:
            return ""
        if self.meta.get("hash") and self.config_path.exists() and content_hash(self.config_path) != self.meta["hash"]:
            return self.push()  # this computer has changes too: push merges them with the server's version
        ssh = self._ssh()
        try:
            remote = self._remote_meta(ssh)
            if not remote.get("rev"):
                return ""
            if upd.branch(remote.get("app", "0")) > upd.branch(__version__):
                # Written by a newer branch: an older one might not understand it. Update first.
                self.meta["error"] = f"данные записаны версией {remote['app']} — ждут обновления приложения (сейчас {__version__})"
                self._save()
                return ""
            local_hash = content_hash(self.config_path)
            if remote["rev"] <= self.meta.get("rev", 0) or remote.get("hash") == local_hash:
                self.meta.update(rev=max(remote["rev"], self.meta.get("rev", 0)), synced_at=int(time.time()), error="")
                if remote.get("hash") == local_hash:
                    self.meta["hash"] = local_hash
                    if not self._base_path().exists():
                        self._save_base()
                self._save()
                return ""
            out = ssh.run(f"base64 < {REMOTE}/current.xfleet | tr -d '\\n'",
                          check=False, timeout=300).out.strip()
        finally:
            ssh.close()
        if not out:
            raise SyncError("на сервере нет данных синхронизации")
        from .service import backup_dir
        try:
            ab.import_bytes(base64.b64decode(out), self.meta["password"], self.config_path, backup_dir(),
                            replace=True, keep_suffix="before-sync")
        except ab.AppBackupError as exc:
            raise SyncError(f"не удалось открыть данные с сервера: {exc}") from exc
        self.meta.update(rev=remote["rev"], hash=content_hash(self.config_path), synced_at=int(time.time()), error="",
                         pulled_at=time.time())
        self._save()
        self._save_base()
        return f"синхронизация: получена версия {remote['rev']} ({remote.get('os') or 'другой компьютер'})"

    def join(self, server: Dict[str, Any], password: str) -> str:
        """Connect this computer to an existing sync: take the data from the server."""
        self.configure(server, password)
        try:
            ssh = self._ssh()
            try:
                remote = self._remote_meta(ssh)
            finally:
                ssh.close()
            if not remote.get("rev"):
                raise SyncError("на этом сервере ещё нет данных синхронизации — включите её на компьютере, где всё настроено")
            if self.config_path.exists():
                self.meta["rev"] = 0  # take the server's version even if this computer had its own
            msg = self.pull()
        except Exception:
            self.disable()
            raise
        return msg or "данные уже совпадают"

    # Stable versions and prereleases live apart, so a beta can never cover a stable version:
    # app/ (stable, where copies before 0.2.4 also look) and app-pre/.
    SLOTS = {"stable": "app", "pre": "app-pre"}

    def update_app(self) -> str:
        """Install a newer program from the server, or publish this one if it is the newest of its kind."""
        root = upd.project_root()
        if not self.enabled or root is None:
            return ""
        ssh = self._ssh()
        try:
            def slot_meta(slot: str) -> dict:
                out = ssh.run(f"cat {REMOTE}/{self.SLOTS[slot]}/meta.json 2>/dev/null || echo '{{}}'", check=False).out.strip() or "{}"
                try:
                    return json.loads(out)
                except ValueError:
                    return {}
            metas = {slot: slot_meta(slot) for slot in self.SLOTS}
            pre_channel = upd.settings()["channel"] == "prerelease"
            candidates = [(slot, m) for slot, m in metas.items() if m.get("version") and (slot == "stable" or pre_channel)
                          and (slot == "pre" or not upd.is_prerelease(m["version"]))]
            best = max(candidates, key=lambda c: upd.parse(c[1]["version"]), default=None)
            self.meta["server_app"] = best[1]["version"] if best else ""
            if best and upd.newer(best[1]["version"], __version__) and not upd.allowed(best[1]["version"]):
                if upd.channel_ok(best[1]["version"]) and not upd.newer(upd.settings()["available"] or "0", best[1]["version"]):
                    upd.offer(best[1]["version"])  # offered, installed only when the user agrees
            if best and upd.newer(best[1]["version"], __version__) and upd.allowed(best[1]["version"]):
                slot, info = best
                data = ssh.run(f"base64 < {REMOTE}/{self.SLOTS[slot]}/current.xfapp | tr -d '\\n'", check=False, timeout=300).out.strip()
                blob = base64.b64decode(data)
                if upd.digest(blob) != info.get("sha"):
                    raise SyncError("обновление на сервере повреждено (контрольная сумма не совпала)")
                ssh.close()
                version = upd.install(blob, self.meta["password"], root)
                self.meta.update(error="", updated_app_at=time.time())
                self._save()
                return f"приложение обновлено: {__version__} → {version}"
            mine = "pre" if upd.is_prerelease(__version__) else "stable"
            there = metas[mine].get("version", "")
            wrong_kind = mine == "stable" and there and upd.is_prerelease(there)
            if (not there or upd.newer(__version__, there) or wrong_kind) and not upd.settings()["pinned"]:
                d = f"{REMOTE}/{self.SLOTS[mine]}"
                blob = upd.package(root, self.meta["password"])
                meta = {"version": __version__, "sha": upd.digest(blob), "size": len(blob), "published": int(time.time()),
                        "os": platform.system()}
                ssh.run(f"set -e\nmkdir -p {d} && chmod 700 {d}\n"
                        f"base64 -d > {d}/current.xfapp.tmp <<'XFSYNC'\n{base64.b64encode(blob).decode()}\nXFSYNC\n"
                        f"mv {d}/current.xfapp.tmp {d}/current.xfapp\n"
                        f"cat > {d}/meta.json <<'XFSYNC'\n{json.dumps(meta)}\nXFSYNC\n"
                        f"chmod 600 {d}/current.xfapp {d}/meta.json\n", timeout=300)
                self._save()
                kind = "пререлиз" if mine == "pre" else "версия"
                return f"{kind} приложения {__version__} выложен(а) для остальных компьютеров"
            self._save()
            return ""
        finally:
            ssh.close()

    def history(self) -> list:
        ssh = self._ssh()
        try:
            out = ssh.run(f"ls -1t {REMOTE}/history 2>/dev/null | head -{KEEP}", check=False).out.split()
        finally:
            ssh.close()
        return out

    def note_error(self, exc: Exception) -> None:
        self.meta["error"] = str(exc)[:300]
        self._save()
