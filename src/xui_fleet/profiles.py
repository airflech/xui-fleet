"""Profiles: separate sets of servers on one computer, like kubectl contexts or AWS CLI profiles.

A profile is one fleet.yaml with its state, plus its own master backups and sync settings.

The first profile ("default") is exactly the layout every earlier version used: fleet.yaml next to the
program, its state in .xui-fleet/, backups and sync settings in the data folder (service.APP_DIR). Nothing
is moved, so an older version still finds its data. Further profiles live in profiles/<id>/ next to it;
their backups and sync settings go to <data folder>/profiles/<id>/.

Leaving a profile deletes everything it stored on this computer. The servers and the copy kept by sync on
a server are never touched: other computers keep working.
"""
from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

DEFAULT = "default"
DEFAULT_NAME = "Основной"
_ID = re.compile(r"^[a-f0-9]{8}$")
SYNC_FILES = ("sync.json", "sync-base.json", "sync-hostkeys.json")
_LOCK = threading.Lock()


class ProfileError(ValueError):
    pass


def _app_dir() -> Path:
    from . import service
    return service.APP_DIR


def profile_of(config_path: Path) -> str:
    """The profile a config file belongs to: <id> for profiles/<id>/fleet.yaml, otherwise the default one."""
    parent = Path(config_path).parent
    if parent.parent.name == "profiles" and _ID.match(parent.name):
        return parent.name
    return DEFAULT


def data_dir(config_path: Path) -> Path:
    """Where this profile keeps backups and sync settings on this computer."""
    pid = profile_of(config_path)
    return _app_dir() if pid == DEFAULT else _app_dir() / "profiles" / pid


def _clean_name(name: Any) -> str:
    text = " ".join(str(name or "").split())
    if not text:
        raise ProfileError("введите название профиля")
    if len(text) > 40:
        raise ProfileError("название профиля — не длиннее 40 символов")
    return text


def _rm(path: Path) -> None:
    try:
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path)
        elif path.exists() or path.is_symlink():
            path.unlink()
    except FileNotFoundError:
        pass


class Profiles:
    """The list of profiles next to the default fleet.yaml (profiles.json) and the files of each one."""

    def __init__(self, base_config: Path):
        self.base = Path(base_config)
        self.root = self.base.parent
        self.file = self.root / "profiles.json"

    # ---- registry -------------------------------------------------------
    def _read(self) -> Dict[str, Any]:
        try:
            raw = json.loads(self.file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raw = {}
        profiles: List[Dict[str, str]] = []
        seen = set()
        for p in raw.get("profiles") or []:
            pid = str((p or {}).get("id") or "")
            if (pid == DEFAULT or _ID.match(pid)) and pid not in seen:
                seen.add(pid)
                profiles.append({"id": pid, "name": str(p.get("name") or (DEFAULT_NAME if pid == DEFAULT else pid))})
        if not profiles:
            profiles = [{"id": DEFAULT, "name": DEFAULT_NAME}]
        active = raw.get("active")
        if not any(p["id"] == active for p in profiles):
            active = profiles[0]["id"]
        return {"active": active, "profiles": profiles}

    def _write(self, data: Dict[str, Any]) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        tmp = self.file.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(str(tmp), str(self.file))

    def _find(self, data: Dict[str, Any], pid: str) -> Dict[str, str]:
        for p in data["profiles"]:
            if p["id"] == pid:
                return p
        raise ProfileError("такого профиля нет")

    # ---- read -----------------------------------------------------------
    def config_path(self, pid: str) -> Path:
        return self.base if pid == DEFAULT else self.root / "profiles" / pid / self.base.name

    def active_id(self) -> str:
        return self._read()["active"]

    def active_config(self) -> Path:
        return self.config_path(self.active_id())

    def list(self) -> List[Dict[str, Any]]:
        data = self._read()
        out = []
        for p in data["profiles"]:
            path = self.config_path(p["id"])
            out.append({"id": p["id"], "name": p["name"], "active": p["id"] == data["active"],
                        "configured": path.exists(), "master": _master_of(path)})
        return out

    # ---- write ----------------------------------------------------------
    def create(self, name: Any) -> str:
        """A new empty profile, made active: the interface shows the first-run screen for it."""
        name = _clean_name(name)
        with _LOCK:
            data = self._read()
            if any(p["name"].lower() == name.lower() for p in data["profiles"]):
                raise ProfileError(f"профиль «{name}» уже есть")
            pid = secrets.token_hex(4)
            while any(p["id"] == pid for p in data["profiles"]):
                pid = secrets.token_hex(4)
            self.config_path(pid).parent.mkdir(parents=True, exist_ok=True)
            data["profiles"].append({"id": pid, "name": name})
            data["active"] = pid
            self._write(data)
            return pid

    def switch(self, pid: str) -> None:
        with _LOCK:
            data = self._read()
            self._find(data, pid)
            data["active"] = pid
            self._write(data)

    def rename(self, pid: str, name: Any) -> None:
        name = _clean_name(name)
        with _LOCK:
            data = self._read()
            if any(p["name"].lower() == name.lower() and p["id"] != pid for p in data["profiles"]):
                raise ProfileError(f"профиль «{name}» уже есть")
            self._find(data, pid)["name"] = name
            self._write(data)

    def remove(self, pid: str) -> str:
        """Leave a profile: delete everything it keeps on this computer. Returns the profile now active;
        when it was the last one, a fresh empty default profile (the first-run screen)."""
        with _LOCK:
            data = self._read()
            self._find(data, pid)
            self.wipe_files(pid)
            data["profiles"] = [p for p in data["profiles"] if p["id"] != pid]
            if not data["profiles"]:
                data = {"active": DEFAULT, "profiles": [{"id": DEFAULT, "name": DEFAULT_NAME}]}
            elif data["active"] == pid:
                data["active"] = data["profiles"][0]["id"]
            self._write(data)
            return data["active"]

    def wipe_all(self) -> None:
        """Every profile and everything the program remembers about servers on this computer.
        Kept: the program itself, its update settings and the address of this page (ui-token)."""
        with _LOCK:
            data = self._read()
            ids = {p["id"] for p in data["profiles"]} | {DEFAULT}
            for pid in ids:
                self.wipe_files(pid)
            _rm(self.root / "profiles")
            _rm(_app_dir() / "profiles")
            _rm(self.file)
            _rm(_app_dir() / "last-job.json")
            _truncate(_app_dir() / "xui-fleet.log")

    def forget_network(self, pid: str) -> List[str]:
        """The servers of a profile are gone: forget its settings, access and sync, keep the master backups
        (they are what brings the network back). The profile stays, showing the first-run screen."""
        with _LOCK:
            self._find(self._read(), pid)
            return self.wipe_files(pid, keep_backups=True)

    def wipe_files(self, pid: str, keep_backups: bool = False) -> List[str]:
        """Delete the files of one profile. Returns what was there (for the log)."""
        gone: List[str] = []

        def rm(path: Path) -> None:
            if path.exists() or path.is_symlink():
                gone.append(str(path))
                _rm(path)

        config = self.config_path(pid)
        if pid != DEFAULT and not keep_backups:
            rm(config.parent)
            rm(_app_dir() / "profiles" / pid)
            return gone
        if pid != DEFAULT:
            rm(config)
            for extra in sorted(config.parent.glob(config.name + ".*")):
                rm(extra)
            rm(config.parent / ".xui-fleet")
            for name in SYNC_FILES:
                rm(_app_dir() / "profiles" / pid / name)
            return gone
        # The default profile shares its folders with the program and with the data folder:
        # delete its own files only, never the folders themselves.
        from .service import backup_dir
        rm(config)
        for extra in sorted(config.parent.glob(config.name + ".*")):  # *.before-import-…, *.before-sync, *.tmp
            rm(extra)
        rm(config.parent / ".xui-fleet")
        backups = backup_dir()
        if backups.is_dir() and not keep_backups:
            for f in sorted(backups.glob("master-*.tar.gz")):
                rm(f)
            try:
                backups.rmdir()  # only when nothing else is in it (a custom XUI_FLEET_BACKUP_DIR may hold more)
            except OSError:
                pass
        for name in SYNC_FILES:
            rm(_app_dir() / name)
            for extra in _app_dir().glob(name + ".*"):
                rm(extra)
        return gone


def _truncate(path: Path) -> None:
    """The log may be held open by the running interface (Windows cannot delete it then): empty it instead."""
    try:
        if path.exists():
            with open(path, "w"):
                pass
    except OSError:
        pass


def _master_of(config: Path) -> Optional[str]:
    """The master's address, to tell profiles apart in the list."""
    if not config.exists():
        return None
    try:
        import yaml
        doc = yaml.safe_load(config.read_text(encoding="utf-8")) or {}
        master = doc.get("master") or {}
        tls = master.get("tls") or {}
        return str(tls.get("domain") or master.get("host") or "") or None
    except Exception:
        return None


def resolve(config: Path) -> Path:
    """The config the command line works with: the active profile when `config` is the default one
    (fleet.yaml next to profiles.json), the given file otherwise."""
    config = Path(config)
    profiles = Profiles(config)
    if profiles.file.exists() and profile_of(config) == DEFAULT:
        return profiles.active_config()
    return config
