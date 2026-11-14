"""Copy of the whole application to move it to another computer (any OS).

One file, encrypted with a password the user chooses: it holds SSH passwords and API tokens.
Inside: fleet.yaml, the local state (tokens, host keys, measured settings) and the master backups,
so the new computer can manage the servers — and even rebuild the master — right away.

Format: b"XFLEET1" | salt(16) | nonce(12) | AES-256-GCM(zip). Key: scrypt(password, salt, n=2**15, r=8, p=1).
"""
from __future__ import annotations

import io
import json
import os
import platform
import time
import zipfile
from pathlib import Path
from typing import Any, Dict, List, Optional

MAGIC = b"XFLEET1"
SUFFIX = ".xfleet"
# Facts about *this* computer's network, not about the servers: re-learned on the new one.
MACHINE_KEYS = ("ssh_route",)


class AppBackupError(RuntimeError):
    pass


def _key(password: str, salt: bytes) -> bytes:
    from cryptography.hazmat.primitives.kdf.scrypt import Scrypt
    return Scrypt(salt=salt, length=32, n=2 ** 15, r=8, p=1).derive(password.encode())


def _check_password(password: str) -> None:
    if len(password or "") < 8:
        raise AppBackupError("пароль копии — не короче 8 символов")


def export_bytes(config_path: Path, backup_dir: Path, password: str, max_backups: Optional[int] = None) -> bytes:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    _check_password(password)
    if not config_path.exists():
        raise AppBackupError("нечего сохранять: приложение ещё не настроено")
    state_path = config_path.parent / ".xui-fleet" / "state.json"
    backups = sorted(backup_dir.glob("master-*.tar.gz")) if backup_dir.is_dir() else []
    if max_backups is not None:
        backups = backups[-max_backups:] if max_backups > 0 else []
    manifest = {"format": 1, "created": int(time.time()), "from": platform.system(),
                "files": ["fleet.yaml"] + (["state.json"] if state_path.exists() else []),
                "master_backups": [b.name for b in backups]}
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
        z.write(config_path, "fleet.yaml")
        if state_path.exists():
            z.writestr("state.json", json.dumps(portable_state(state_path), ensure_ascii=False, indent=2))
        for b in backups:
            z.write(b, f"backups/{b.name}")
    salt, nonce = os.urandom(16), os.urandom(12)
    return MAGIC + salt + nonce + AESGCM(_key(password, salt)).encrypt(nonce, buf.getvalue(), MAGIC)


def portable_state(state_path: Path) -> Dict[str, Any]:
    """state.json without the facts about this computer's own network."""
    state = json.loads(state_path.read_text(encoding="utf-8"))
    for server in (state.get("servers") or {}).values():
        for k in MACHINE_KEYS:
            server.pop(k, None)
    return state


def read(data: bytes, password: str) -> zipfile.ZipFile:
    from cryptography.exceptions import InvalidTag
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    if not data.startswith(MAGIC) or len(data) < len(MAGIC) + 28:
        raise AppBackupError("это не копия xui-fleet (файл .xfleet)")
    salt, nonce, body = data[7:23], data[23:35], data[35:]
    try:
        plain = AESGCM(_key(password or "", salt)).decrypt(nonce, body, MAGIC)
    except InvalidTag:
        raise AppBackupError("неверный пароль копии (или файл повреждён)") from None
    z = zipfile.ZipFile(io.BytesIO(plain))
    if "fleet.yaml" not in z.namelist():
        raise AppBackupError("в копии нет fleet.yaml")
    return z


def describe(data: bytes, password: str) -> Dict[str, Any]:
    z = read(data, password)
    manifest = json.loads(z.read("manifest.json")) if "manifest.json" in z.namelist() else {}
    import yaml
    cfg = yaml.safe_load(z.read("fleet.yaml")) or {}
    return {"created": manifest.get("created"), "from": manifest.get("from", ""),
            "master": (cfg.get("master") or {}).get("host", ""), "nodes": [n.get("name") for n in cfg.get("nodes") or []],
            "masterBackups": len(manifest.get("master_backups") or [])}


def import_bytes(data: bytes, password: str, config_path: Path, backup_dir: Path, replace: bool = False,
                 keep_suffix: Optional[str] = None) -> Dict[str, Any]:
    """Restore a copy here. The current settings (if any) are kept next to them as *.before-import-<time>
    (or *.<keep_suffix>, overwritten each time — used by sync)."""
    from .config import FleetConfig
    import yaml
    z = read(data, password)
    cfg_text = z.read("fleet.yaml")
    FleetConfig.model_validate(yaml.safe_load(cfg_text) or {})  # never install a broken config
    state_path = config_path.parent / ".xui-fleet" / "state.json"
    if config_path.exists() and not replace:
        raise AppBackupError("здесь уже есть настройки — подтвердите замену")
    stamp = time.strftime("%Y%m%d-%H%M%S")
    suffix = keep_suffix or f"before-import-{stamp}"
    for p in (config_path, state_path):
        if p.exists():
            p.replace(p.with_name(f"{p.name}.{suffix}"))
    config_path.write_bytes(cfg_text)
    restored: List[str] = []
    if "state.json" in z.namelist():
        state = json.loads(z.read("state.json"))
        old = {}
        backup_state = state_path.with_name(f"{state_path.name}.{suffix}")
        if backup_state.exists():  # facts about this computer's network survive a restore
            try:
                old = json.loads(backup_state.read_text(encoding="utf-8")).get("servers") or {}
            except ValueError:
                old = {}
        for name, server in (state.get("servers") or {}).items():
            for k in MACHINE_KEYS:
                server.pop(k, None)
                if k in (old.get(name) or {}):
                    server[k] = old[name][k]
        state_path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(state_path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(state, fh, indent=2, ensure_ascii=False)
    backup_dir.mkdir(parents=True, exist_ok=True)
    for name in z.namelist():
        if name.startswith("backups/") and name.endswith(".tar.gz"):
            target = backup_dir / Path(name).name
            if not target.exists():
                target.write_bytes(z.read(name))
                try:
                    os.chmod(target, 0o600)
                except OSError:
                    pass
                restored.append(target.name)
    return {"backups": len(restored), "previous": f"*.before-import-{stamp}" if replace else ""}
