"""Run the web interface as a macOS background service (launchd LaunchAgent).

One fixed address with a token that never changes, so a browser bookmark keeps working;
started at login, restarted if it dies, and restarted by itself when the code is updated.
"""
from __future__ import annotations

import hashlib
import os
import plistlib
import secrets
import subprocess
import sys
from pathlib import Path
from typing import Optional

LABEL = "com.xui-fleet.ui"
PORT = 8787
HOME = Path.home()


def data_dir() -> Path:
    """Per-user folder for the UI token and master backups, in the place each OS expects:
    macOS ~/Library/Application Support, Windows %APPDATA%, Linux $XDG_DATA_HOME or ~/.local/share."""
    if os.environ.get("XUI_FLEET_DATA_DIR"):
        return Path(os.environ["XUI_FLEET_DATA_DIR"]).expanduser()
    if sys.platform == "darwin":
        return HOME / "Library" / "Application Support" / "xui-fleet"
    if os.name == "nt":
        return Path(os.environ.get("APPDATA") or HOME / "AppData" / "Roaming") / "xui-fleet"
    return Path(os.environ.get("XDG_DATA_HOME") or HOME / ".local" / "share") / "xui-fleet"


APP_DIR = data_dir()


def backup_dir(config_path: Optional[Path] = None) -> Path:
    """Master backups: kept on this computer, whatever folder the project lives in.
    Each profile has its own; the default one (and no config given) keeps the original place."""
    from .profiles import DEFAULT, data_dir, profile_of
    if config_path is not None and profile_of(config_path) != DEFAULT:
        return data_dir(config_path) / "backups"
    return Path(os.environ.get("XUI_FLEET_BACKUP_DIR") or APP_DIR / "backups").expanduser()
PLIST = HOME / "Library" / "LaunchAgents" / f"{LABEL}.plist"
LOG = HOME / "Library" / "Logs" / "xui-fleet.log" if sys.platform == "darwin" else APP_DIR / "xui-fleet.log"


def open_folder(path: Path) -> None:
    """Show a folder in Finder / Explorer / the desktop's file manager."""
    if os.name == "nt":
        os.startfile(str(path))  # type: ignore[attr-defined]
    else:
        subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", str(path)],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def restart_self(argv: list) -> None:
    """Replace this process with a fresh one running the same command (new code, same port).
    Windows has no real exec: start the new process, then leave."""
    cmd = [sys.executable, "-m", "xui_fleet.cli"] + argv
    if os.name == "nt":
        subprocess.Popen(cmd, creationflags=0x00000008 | 0x00000200, close_fds=True)  # DETACHED | NEW_GROUP
        os._exit(0)
    os.execv(sys.executable, cmd)


def token() -> str:
    """Stable access token (created once, readable only by this user)."""
    path = APP_DIR / "ui-token"
    if path.exists():
        value = path.read_text().strip()
        if value:
            return value
    APP_DIR.mkdir(parents=True, exist_ok=True)
    value = secrets.token_urlsafe(24)
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(value)
    return value


def url(port: int = PORT) -> str:
    return f"http://127.0.0.1:{port}/?t={token()}"


def code_version() -> str:
    """Fingerprint of the installed code; changes whenever any source or page file changes."""
    root = Path(__file__).resolve().parent
    h = hashlib.sha1()
    for p in sorted(list(root.glob("*.py")) + list(root.glob("*.html"))):
        st = p.stat()
        h.update(f"{p.name}:{st.st_mtime_ns}:{st.st_size}".encode())
    return h.hexdigest()[:12]


def install(config: Path) -> str:
    """Write the LaunchAgent and (re)load it. Safe to run again."""
    config = config.resolve()
    project = config.parent
    python = project / ".venv" / "bin" / "python"
    spec = {
        "Label": LABEL,
        "ProgramArguments": [str(python), "-m", "xui_fleet.cli", "ui", "--service", "--config", str(config)],
        "WorkingDirectory": str(project),
        "RunAtLoad": True,
        "KeepAlive": True,  # restart after a crash and after a code update (the server exits on purpose)
        "ThrottleInterval": 3,
        "StandardOutPath": str(LOG),
        "StandardErrorPath": str(LOG),
        "EnvironmentVariables": {"PYTHONUNBUFFERED": "1", "LANG": "ru_RU.UTF-8"},
    }
    PLIST.parent.mkdir(parents=True, exist_ok=True)
    LOG.parent.mkdir(parents=True, exist_ok=True)
    new = plistlib.dumps(spec)
    changed = not PLIST.exists() or PLIST.read_bytes() != new
    if changed:
        PLIST.write_bytes(new)
    domain = f"gui/{os.getuid()}"
    loaded = subprocess.run(["launchctl", "print", f"{domain}/{LABEL}"], capture_output=True).returncode == 0
    if loaded and changed:
        subprocess.run(["launchctl", "bootout", f"{domain}/{LABEL}"], capture_output=True)
        loaded = False
    if not loaded:
        r = subprocess.run(["launchctl", "bootstrap", domain, str(PLIST)], capture_output=True, text=True)
        if r.returncode != 0 and "already" not in (r.stderr or ""):
            raise RuntimeError(f"не удалось запустить службу: {r.stderr.strip()}")
    return url()


def uninstall() -> None:
    subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}/{LABEL}"], capture_output=True)
    PLIST.unlink(missing_ok=True)


def running(port: int = PORT) -> Optional[str]:
    """Version string of a live interface on the port, or None."""
    import urllib.request
    try:
        req = urllib.request.Request(f"http://127.0.0.1:{port}/api/ping", headers={"X-Fleet-Token": token()})
        with urllib.request.urlopen(req, timeout=2) as resp:
            return resp.read().decode()
    except Exception:
        return None


def is_service_process() -> bool:
    return "--service" in sys.argv
