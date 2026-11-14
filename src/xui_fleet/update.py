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
REPO = os.environ.get("XUI_FLEET_UPDATE_REPO", "airflech/xui-fleet")
# What makes up the program (relative to the project folder). Never the user's data.
FILES = ["launcher.py", "pyproject.toml", "README.md", "fleet.example.yaml",
         "Запустить xui-fleet.bat", "Запустить xui-fleet.command", "Запустить xui-fleet.sh"]
DIRS = ["scripts", "src/xui_fleet"]
SKIP = re.compile(r"(__pycache__|\.pyc$|\.egg-info)")


class UpdateError(RuntimeError):
    pass


_PRE = {"a": 0, "alpha": 0, "b": 1, "beta": 1, "rc": 2}


def parse(v: str) -> Tuple[int, ...]:
    """Comparable key: 0.3.0-beta.1 < 0.3.0-rc.1 < 0.3.0 < 0.3.1 (also PEP 440 forms like 0.3.0b1)."""
    m = re.match(r"v?(\d+)\.(\d+)(?:\.(\d+))?(?:[-.]?(alpha|beta|rc|a|b)\.?(\d*))?", (v or "").strip())
    if not m:
        return (0, 0, 0, 3, 0)
    major, minor, patch, pre, num = m.groups()
    return (int(major), int(minor), int(patch or 0), _PRE.get(pre, 3) if pre else 3, int(num or 0))


def newer(a: str, b: str) -> bool:
    return parse(a) > parse(b)


def is_prerelease(v: str) -> bool:
    return parse(v)[3] < 3


def branch(v: str) -> Tuple[int, int]:
    """Versions of one branch (0.2.x) read each other's data; a new branch may change it."""
    return parse(v)[:2]


# ---- per-computer update settings ---------------------------------------
def _settings_path() -> Path:
    from .service import APP_DIR
    return APP_DIR / "update.json"


def settings() -> dict:
    try:
        import json
        data = json.loads(_settings_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    return {"channel": data.get("channel") if data.get("channel") in ("stable", "prerelease") else "stable",
            "pinned": data.get("pinned") or "",
            # Off: a new version is offered (a button), not installed by itself.
            "auto": bool(data.get("auto", False)),
            "available": data.get("available") or ""}


def save_settings(**changes) -> dict:
    import json
    data = dict(settings(), **changes)
    path = _settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return data


def channel_ok(version: str) -> bool:
    return settings()["channel"] == "prerelease" or not is_prerelease(version)


def allowed(version: str) -> bool:
    """May this computer move to `version` by itself? Only with automatic updates on, not while pinned,
    prereleases only on that channel."""
    st = settings()
    return st["auto"] and not st["pinned"] and channel_ok(version)


def offer(version: str) -> None:
    """Remember a newer version to show the user (an "Update" button)."""
    if settings()["available"] != version:
        save_settings(available=version)


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


_cache: dict = {}


def releases(repo: str = "", fresh: bool = False) -> List[dict]:
    """All published versions, newest first: version, prerelease, date, notes, page, zip/sha asset links."""
    import time
    import httpx
    repo = repo or REPO
    if not repo:
        return []
    hit = _cache.get(repo)
    # A list that does not know the installed version is out of date (e.g. read while that release was
    # still being built): never serve it from the cache.
    if hit and not fresh and time.time() - hit[0] < 60 and any(r["version"] == __version__ for r in hit[1]):
        return hit[1]
    r = httpx.get(f"https://api.github.com/repos/{repo}/releases?per_page=50", timeout=15,
                  headers={"Accept": "application/vnd.github+json", "User-Agent": "xui-fleet-updater"})
    if r.status_code != 200:
        raise UpdateError(f"GitHub ответил {r.status_code}")
    out = []
    for rel in r.json():
        if rel.get("draft"):
            continue
        version = str(rel.get("tag_name", "")).lstrip("v")
        assets = {a["name"]: a for a in rel.get("assets") or []}
        name = f"xui-fleet-{version}.zip"
        if name not in assets:
            continue
        d = str(assets[name].get("digest") or "")
        out.append({"version": version, "prerelease": bool(rel.get("prerelease")) or is_prerelease(version),
                    "date": rel.get("published_at", ""), "notes": (rel.get("body") or "")[:4000], "page": rel.get("html_url", ""),
                    "url": assets[name]["browser_download_url"],
                    "sha_url": assets.get(name + ".sha256", {}).get("browser_download_url", ""),
                    "sha256": d[7:] if d.startswith("sha256:") else ""})
    out.sort(key=lambda r: parse(r["version"]), reverse=True)
    _cache[repo] = (time.time(), out)
    return out


def latest(rels: List[dict], prerelease: bool) -> Optional[dict]:
    return next((r for r in rels if prerelease or not r["prerelease"]), None)


def github_latest(repo: str = "") -> Optional[dict]:
    """Newest release allowed by this computer's channel."""
    return latest(releases(repo), settings()["channel"] == "prerelease")


def _with_sha(info: dict) -> dict:
    """The .sha256 file published next to the archive (GitHub's own digest as a fallback)."""
    import httpx
    if info.get("sha_url"):
        t = httpx.get(info["sha_url"], timeout=30, follow_redirects=True, headers={"User-Agent": "xui-fleet-updater"})
        if t.status_code == 200 and t.text.strip():
            return dict(info, sha256=t.text.split()[0])
    return info


def install_version(version: str, root: Optional[Path] = None) -> str:
    """Install exactly this version (newer or older). Choosing an older one pins it: no automatic updates
    until the user resumes them — otherwise the next check would bring the newest one back."""
    root = root or project_root()
    if root is None:
        raise UpdateError("эта копия установлена не из папки xui-fleet — обновлять нечего")
    info = next((r for r in releases(fresh=True) if r["version"] == version.lstrip("v")), None)
    if info is None:
        raise UpdateError(f"версии {version} нет среди релизов")
    installed = install_from_github(root, info)
    newest = latest(releases(), settings()["channel"] == "prerelease")
    behind = bool(newest and newer(newest["version"], installed))
    save_settings(pinned=installed if behind and newer(__version__, installed) else "",
                  available=newest["version"] if behind else "")
    return installed


def install_from_github(root: Path, info: dict) -> str:
    """Download a release archive, check its SHA-256, install it."""
    import httpx
    info = _with_sha(info)
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
    if not info or not newer(info["version"], __version__) or not channel_ok(info["version"]):
        offer("")
        return ""
    if not allowed(info["version"]):
        offer(info["version"])
        return ""
    version = install_from_github(root, info)
    return f"приложение обновлено с GitHub: {__version__} → {version}"


def digest(blob: bytes) -> str:
    return hashlib.sha256(blob).hexdigest()[:16]
