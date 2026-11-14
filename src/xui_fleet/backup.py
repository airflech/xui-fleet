"""Backups of the master's 3x-ui and moving the master to another server.

A backup is one tar.gz on this computer:
  x-ui.db                consistent copy exported by the panel itself (WAL included)
  files/<abs path>       server files the database depends on: the domain certificate it
                         points at, the node-token key and /etc/default/x-ui when present
  meta.json              when, from which server, panel port/path, what is inside
Only the newest KEEP backups are kept.
"""
from __future__ import annotations

import base64
import io
import json
import os
import shlex
import sqlite3
import tarfile
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

KEEP = 10
PREFIX = "master-"


@dataclass
class BackupInfo:
    path: Path
    created: float
    host: str
    domain: str
    inbounds: int
    clients: int
    nodes: int
    size: int
    reason: str = ""

    def view(self) -> Dict[str, Any]:
        return {"file": self.path.name, "created": int(self.created), "host": self.host, "domain": self.domain,
                "inbounds": self.inbounds, "clients": self.clients, "nodes": self.nodes, "size": self.size,
                "reason": self.reason}


def db_summary(data: bytes) -> Dict[str, int]:
    """Refuse to keep a backup that is not a healthy 3x-ui database."""
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "x-ui.db"
        path.write_bytes(data)
        con = sqlite3.connect(str(path))
        try:
            if con.execute("pragma integrity_check").fetchone()[0] != "ok":
                raise ValueError("база в бэкапе повреждена")
            count = lambda t: con.execute(f"select count(*) from {t}").fetchone()[0]  # noqa: E731
            return {"inbounds": count("inbounds"), "clients": count("clients"), "nodes": count("nodes")}
        finally:
            con.close()


def write_bundle(directory: Path, db: bytes, files: Dict[str, bytes], meta: Dict[str, Any]) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    os.chmod(directory, 0o700)
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(meta["created"]))
    path = directory / f"{PREFIX}{stamp}.tar.gz"
    tmp = path.with_suffix(".tmp")
    with tarfile.open(tmp, "w:gz") as tar:
        def add(name: str, data: bytes) -> None:
            info = tarfile.TarInfo(name)
            info.size, info.mode, info.mtime = len(data), 0o600, int(meta["created"])
            tar.addfile(info, io.BytesIO(data))
        add("meta.json", json.dumps(meta, ensure_ascii=False, indent=2).encode())
        add("x-ui.db", db)
        for remote, data in files.items():
            add("files" + remote, data)
    os.chmod(tmp, 0o600)
    tmp.replace(path)
    rotate(directory)
    return path


def rotate(directory: Path, keep: int = KEEP) -> List[Path]:
    """Delete all but the newest `keep` backups. Returns what was removed."""
    backups = sorted(directory.glob(f"{PREFIX}*.tar.gz"), key=lambda p: p.name, reverse=True)
    removed = backups[keep:]
    for p in removed:
        p.unlink()
    return removed


def read_bundle(path: Path) -> Dict[str, Any]:
    with tarfile.open(path, "r:gz") as tar:
        members = {m.name: m for m in tar.getmembers() if m.isfile()}
        read = lambda n: tar.extractfile(members[n]).read()  # noqa: E731
        meta = json.loads(read("meta.json"))
        db = read("x-ui.db")
        files = {n[len("files"):]: read(n) for n in members if n.startswith("files/")}
    return {"meta": meta, "db": db, "files": files}


def list_backups(directory: Path) -> List[BackupInfo]:
    out: List[BackupInfo] = []
    for p in sorted(directory.glob(f"{PREFIX}*.tar.gz"), key=lambda p: p.name, reverse=True):
        try:
            with tarfile.open(p, "r:gz") as tar:
                meta = json.loads(tar.extractfile("meta.json").read())
        except Exception:
            continue
        c = meta.get("counts", {})
        out.append(BackupInfo(p, meta.get("created", p.stat().st_mtime), meta.get("host", ""), meta.get("domain", ""),
                              c.get("inbounds", 0), c.get("clients", 0), c.get("nodes", 0), p.stat().st_size,
                              meta.get("reason", "")))
    return out


def upload_script(files: Dict[str, bytes]) -> str:
    """Shell that writes each file at its absolute path (root only, mode 600), data inline as base64."""
    parts = ["set -e"]
    for remote, data in files.items():
        q = shlex.quote(remote)
        parts.append(f"mkdir -p \"$(dirname {q})\"")
        parts.append(f"base64 -d > {q}.fleet-new <<'XUIFLEETB64'\n{base64.b64encode(data).decode()}\nXUIFLEETB64")
        parts.append(f"chmod 600 {q}.fleet-new && mv {q}.fleet-new {q}")
    return "\n".join(parts) + "\n"
