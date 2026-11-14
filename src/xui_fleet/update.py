"""Updates of the application itself, delivered through the sync server.

Whichever computer has the newest version publishes its code there (encrypted and authenticated with the
sync password — nobody without it can plant an update); the others install it by themselves and restart.
Only the program travels: fleet.yaml, the state and the Python environment stay where they are.
"""
from __future__ import annotations

import hashlib
import io
import os
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path
from typing import List, Optional, Tuple

from . import __version__
from . import appbackup as ab

MAGIC = b"XFAPP01"
# Public GitHub repository with the releases ("owner/name"); empty = updates only through the sync server.
REPO = os.environ.get("XUI_FLEET_UPDATE_REPO", "")
# What makes up the program (relative to the project folder). Never the user's data.
FILES = ["launcher.py", "pyproject.toml", "README.md", "fleet.example.yaml",
         "Запустить xui-fleet.bat", "Запустить xui-fleet.command", "Запустить xui-fleet.sh"]
DIRS = ["scripts", "src/xui_fleet"]
SKIP = re.compile(r"(__pycache__|\.pyc$|\.egg-info)")


class UpdateError(RuntimeError):
    pass


def parse(v: str) -> Tuple[int, ...]:
    return tuple(int(x) for x in re.findall(r"\d+", v or "0"))


def newer(a: str, b: str) -> bool:
    return parse(a) > parse(b)


def project_root() -> Optional[Path]:
    """The folder this copy runs from, if it is a normal xui-fleet folder (not an installed package)."""
    root = Path(__file__).resolve().parents[2]
    return root if (root / "pyproject.toml").exists() and (root / "src" / "xui_fleet" / "__init__.py").exists() else None


def _members(root: Path) -> List[Path]:
    out = [root / f for f in FILES if (root / f).exists()]
    for d in DIRS:
        for p in sorted((root / d).rglob("*")):
            if p.is_file() and not SKIP.search(str(p)):
                out.append(p)
    return out


def package(root: Path, password: str) -> bytes:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("VERSION", __version__)
        for p in _members(root):
            info = zipfile.ZipInfo(p.relative_to(root).as_posix(), date_time=(2020, 1, 1, 0, 0, 0))
            info.external_attr = (0o755 if os.access(p, os.X_OK) else 0o644) << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            z.writestr(info, p.read_bytes())
    salt, nonce = os.urandom(16), os.urandom(12)
    return MAGIC + salt + nonce + AESGCM(ab._key(password, salt)).encrypt(nonce, buf.getvalue(), MAGIC)


def unpack(blob: bytes, password: str) -> zipfile.ZipFile:
    from cryptography.exceptions import InvalidTag
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    if not blob.startswith(MAGIC):
        raise UpdateError("это не обновление xui-fleet")
    try:
        plain = AESGCM(ab._key(password, blob[7:23])).decrypt(blob[23:35], blob[35:], MAGIC)
    except InvalidTag:
        raise UpdateError("обновление не прошло проверку подлинности (другой пароль синхронизации?)") from None
    z = zipfile.ZipFile(io.BytesIO(plain))
    names = z.namelist()
    if "VERSION" not in names or "src/xui_fleet/__init__.py" not in names:
        raise UpdateError("обновление неполное")
    _safe_names(z)
    return z


def install(blob: bytes, password: str, root: Path) -> str:
    """Install an update that came through the sync server (sealed with the sync password)."""
    z = unpack(blob, password)
    return _install(z, root, z.read("VERSION").decode().strip(), "")


def _safe_names(z: zipfile.ZipFile) -> None:
    for n in z.namelist():  # never write outside the program folder
        if n.startswith(("/", "\\")) or ".." in Path(n).parts:
            raise UpdateError(f"подозрительный путь в обновлении: {n}")


def _install(z: zipfile.ZipFile, root: Path, version: str, prefix: str) -> str:
    """Put the new program files in place; reinstall dependencies only when their list changed.
    The running service notices the new code and restarts itself. `prefix` — top folder inside the zip."""
    old_pyproject = (root / "pyproject.toml").read_bytes() if (root / "pyproject.toml").exists() else b""
    with tempfile.TemporaryDirectory() as tmp:
        z.extractall(tmp)
        for name in z.namelist():
            if name == "VERSION" or name.endswith("/") or not name.startswith(prefix):
                continue
            rel = name[len(prefix):]
            if not (rel in FILES or any(rel.startswith(d + "/") for d in DIRS)):
                continue  # only program files: an update must never touch the user's data
            src, dst = Path(tmp) / name, root / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            part = dst.with_name(dst.name + ".xf-new")
            shutil.copyfile(src, part)
            mode = (z.getinfo(name).external_attr >> 16) & 0o777
            if mode and os.name != "nt":
                os.chmod(part, mode)
            os.replace(part, dst)
    if _deps(old_pyproject) != _deps((root / "pyproject.toml").read_bytes()):
        if _name((root / "pyproject.toml").read_bytes()) != "xui-fleet":
            raise UpdateError("в обновлении не тот проект — зависимости не ставлю")
        r = subprocess.run([sys.executable, "-m", "pip", "-q", "--disable-pip-version-check", "install", "-e", str(root)],
                           capture_output=True, text=True)
        if r.returncode != 0:
            raise UpdateError("файлы обновлены, но зависимости не установились: " + (r.stderr or r.stdout)[-300:])
    return version


def _deps(pyproject: bytes) -> str:
    """The dependency list only: a new version number alone must not trigger a reinstall."""
    m = re.search(r"^dependencies\s*=\s*\[(.*?)\]", pyproject.decode("utf-8", "replace"), re.S | re.M)
    return re.sub(r"\s+", "", m.group(1)) if m else ""


def _name(pyproject: bytes) -> str:
    m = re.search(r'^name\s*=\s*"([^"]+)"', pyproject.decode("utf-8", "replace"), re.M)
    return m.group(1) if m else ""


def github_latest(repo: str = "") -> Optional[dict]:
    """Newest release of the public repository: {"version", "url", "sha256"} or None."""
    import httpx
    repo = repo or REPO
    if not repo:
        return None
    r = httpx.get(f"https://api.github.com/repos/{repo}/releases/latest", timeout=15,
                  headers={"Accept": "application/vnd.github+json", "User-Agent": "xui-fleet-updater"})
    if r.status_code != 200:
        return None
    rel = r.json()
    version = str(rel.get("tag_name", "")).lstrip("v")
    assets = {a["name"]: a for a in rel.get("assets") or []}
    name = f"xui-fleet-{version}.zip"
    if name not in assets:
        return None
    sha = ""
    if f"{name}.sha256" in assets:
        t = httpx.get(assets[f"{name}.sha256"]["browser_download_url"], timeout=30, follow_redirects=True,
                      headers={"User-Agent": "xui-fleet-updater"})
        sha = t.text.split()[0] if t.status_code == 200 and t.text.strip() else ""
    if not sha:  # GitHub also reports the digest of every asset itself
        d = str(assets[name].get("digest") or "")
        sha = d[7:] if d.startswith("sha256:") else ""
    return {"version": version, "url": assets[name]["browser_download_url"], "sha256": sha}


def install_from_github(root: Path, info: dict) -> str:
    """Download a release archive, check its SHA-256, install it."""
    import httpx
    if not info.get("sha256"):
        raise UpdateError("у релиза нет контрольной суммы — не устанавливаю")
    r = httpx.get(info["url"], timeout=120, follow_redirects=True, headers={"User-Agent": "xui-fleet-updater"})
    r.raise_for_status()
    if hashlib.sha256(r.content).hexdigest() != info["sha256"]:
        raise UpdateError("архив обновления не совпал с контрольной суммой — не устанавливаю")
    z = zipfile.ZipFile(io.BytesIO(r.content))
    _safe_names(z)
    if "xui-fleet/src/xui_fleet/__init__.py" not in z.namelist():
        raise UpdateError("в архиве нет программы")
    return _install(z, root, info["version"], "xui-fleet/")


def check_github(root: Optional[Path] = None) -> str:
    """Install a newer release from GitHub, if there is one. Returns a note (empty when nothing to do)."""
    root = root or project_root()
    if root is None or not REPO:
        return ""
    info = github_latest()
    if not info or not newer(info["version"], __version__):
        return ""
    version = install_from_github(root, info)
    return f"приложение обновлено с GitHub: {__version__} → {version}"


def digest(blob: bytes) -> str:
    return hashlib.sha256(blob).hexdigest()[:16]
