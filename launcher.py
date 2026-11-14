"""Start xui-fleet on Windows, macOS or Linux: one double-click from any folder.

Only the standard library is used here, so it runs on a bare Python 3.9+. On the first start (or after the
folder was moved/copied to another computer) it creates its own environment in .venv and installs the
dependencies; then it starts the web interface in the background and opens it in the browser.
"""
from __future__ import annotations

import os
import plistlib
import shutil
import subprocess
import sys
import time
import venv
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent
VENV = ROOT / ".venv"
WINDOWS = os.name == "nt"
PY = VENV / ("Scripts/python.exe" if WINDOWS else "bin/python")


def say(text: str) -> None:
    print(text, flush=True)


def env_ok() -> bool:
    """The environment exists and points at this very folder (a copied .venv points at the old one)."""
    if not PY.exists():
        return False
    probe = ("import os, sys, xui_fleet; "
             "sys.exit(0 if os.path.realpath(xui_fleet.__file__).startswith(os.path.realpath(sys.argv[1])) else 1)")
    return subprocess.run([str(PY), "-c", probe, str(ROOT / "src")], capture_output=True).returncode == 0


def build_env() -> None:
    say("Готовлю окружение (первый запуск или папка перенесена) — 1–2 минуты…")
    shutil.rmtree(VENV, ignore_errors=True)
    try:
        venv.EnvBuilder(with_pip=True, clear=True).create(VENV)
    except Exception as exc:
        hint = ("\nНа Debian/Ubuntu: sudo apt install python3-venv" if sys.platform.startswith("linux") else "")
        raise SystemExit(f"Не удалось создать окружение Python: {exc}{hint}")
    pip = [str(PY), "-m", "pip", "-q", "--disable-pip-version-check"]
    for step in (pip + ["install", "-U", "pip"], pip + ["install", "-e", str(ROOT)]):
        if subprocess.run(step).returncode != 0:
            raise SystemExit("Не удалось установить зависимости. Проверьте интернет и запустите снова.")


def service(expr: str) -> str:
    return subprocess.run([str(PY), "-c", f"from xui_fleet import service; print({expr})"],
                          capture_output=True, text=True, cwd=ROOT).stdout.strip()


def start_ui() -> None:
    log_path = Path(service("service.LOG"))
    log_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [str(PY), "-m", "xui_fleet.cli", "ui", "--service", "--config", str(ROOT / "fleet.yaml")]
    log = open(log_path, "a")
    if WINDOWS:
        # pythonw would hide errors; a detached console-less process keeps running after this window closes.
        flags = 0x00000008 | 0x00000200 | 0x08000000  # DETACHED_PROCESS | NEW_PROCESS_GROUP | CREATE_NO_WINDOW
        subprocess.Popen(cmd, cwd=ROOT, stdout=log, stderr=log, stdin=subprocess.DEVNULL, creationflags=flags, close_fds=True)
    else:
        subprocess.Popen(cmd, cwd=ROOT, stdout=log, stderr=log, stdin=subprocess.DEVNULL, start_new_session=True)
    for _ in range(60):
        if service("service.running()") not in ("", "None"):
            return
        time.sleep(0.25)
    say(f"Интерфейс не ответил. Журнал: {log_path}")


def bookmark(url: str) -> None:
    """A file next to the launcher that opens the interface (the address never changes)."""
    try:
        if sys.platform == "darwin":
            (ROOT / "Открыть xui-fleet.webloc").write_bytes(plistlib.dumps({"URL": url}))
        elif WINDOWS:
            (ROOT / "Открыть xui-fleet.url").write_text(f"[InternetShortcut]\nURL={url}\n", encoding="utf-8")
        else:
            p = ROOT / "Открыть xui-fleet.desktop"
            p.write_text(f"[Desktop Entry]\nType=Link\nName=xui-fleet\nURL={url}\n", encoding="utf-8")
    except OSError:
        pass


def main() -> None:
    if sys.version_info < (3, 9):
        raise SystemExit("Нужен Python 3.9 или новее: https://www.python.org/downloads/")
    os.chdir(ROOT)
    if not env_ok():
        build_env()
    url = service("service.url()")
    if service("service.running()") in ("", "None"):
        start_ui()
    bookmark(url)
    webbrowser.open(url)
    say("Интерфейс открыт в браузере. Это окно можно закрыть.")
    say("Постоянный адрес (добавьте в закладки): " + url)


if __name__ == "__main__":
    try:
        main()
    except SystemExit as exc:
        if exc.code not in (None, 0):
            say(str(exc.code))
            if WINDOWS or sys.stdin.isatty():
                input("Нажмите Enter…")
        raise
