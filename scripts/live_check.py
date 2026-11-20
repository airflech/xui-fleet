"""Before every release: the everyday functions, run for real against the live servers.

Only the service test user (clients.check_email, «fleet-check») is touched, and everything changed on it is put back.
Usage, from the folder with fleet.yaml, with the code to be released:

    PYTHONPATH=<checkout>/src <venv>/bin/python <checkout>/scripts/live_check.py [fleet.yaml]

Exit code 0 — all good; 1 — something failed (each step says what).
"""
import sys
import time
from pathlib import Path

from xui_fleet import api
from xui_fleet.ui import App

FIELDS = ("comment", "totalGB", "expiryTime", "enable", "limitHwid", "limitIp")
KEYS = ("subId", "uuid", "auth", "flow", "group", "password")


def main(config: Path) -> int:
    app = App(config.resolve())
    fleet = app.fleet()
    name = fleet.cfg.clients.check_email
    failed = []

    def raw():
        with fleet.master_panel(provision=False) as p:
            c = p.client(name)
            return dict(c["client"], inboundIds=sorted(c["inboundIds"]))

    def step(title, ok, detail=""):
        print(("✅ " if ok else "❌ ") + title + (f" — {detail}" if detail and not ok else ""), flush=True)
        if not ok:
            failed.append(title)

    def save(**kw):  # exactly what the «Save» button of the user card sends
        body = {"name": name, "comment": "", "totalGb": 0, "expiry": 0, "enabled": True, "devices": 0, "ips": 0}
        body.update(kw)
        return api.dispatch(app, "POST", "/api/user/edit", body)

    before = raw()
    try:
        # 1. the user card: every field at once
        exp = int(time.time() + 30 * 86400) * 1000
        save(comment="live-check", totalGb=5, expiry=exp, devices=3, ips=2)
        got = raw()
        want = {"comment": "live-check", "totalGB": 5 * 1024 ** 3, "expiryTime": exp, "enable": True, "limitHwid": 3, "limitIp": 2}
        step("карточка пользователя: сохраняются все поля", {k: got.get(k) for k in want} == want,
             str({k: got.get(k) for k in want}))
        step("карточка пользователя: ключи, ссылка и подключения не тронуты",
             all(got.get(k) == before.get(k) for k in KEYS + ("inboundIds",)))
        # 2. renew (+1 month, as the quick buttons and the search do)
        exp2 = exp + 31 * 86400 * 1000
        save(comment="live-check", totalGb=5, expiry=exp2, devices=3, ips=2)
        step("продление срока", raw().get("expiryTime") == exp2)
        # 3. switch off and on
        save(comment="live-check", enabled=False)
        off = raw().get("enable") is False
        save(comment="live-check", enabled=True)
        step("выключить и включить", off and raw().get("enable") is True)
        # 4. reset traffic
        r = api.dispatch(app, "POST", "/api/user/reset", {"name": name})
        step("сброс трафика", bool(r.get("ok")))
        # 5. the user card opens
        u = api.dispatch(app, "GET", "/api/user", {"name": name})
        step("карточка пользователя открывается", u.get("enabled") is True)
    except Exception as exc:  # noqa: BLE001
        step("пользовательские функции", False, f"{type(exc).__name__}: {exc}")
    finally:
        try:
            save(comment=before.get("comment") or "", totalGb=(before.get("totalGB") or 0) / 1024 ** 3,
                 expiry=before.get("expiryTime") or 0, enabled=bool(before.get("enable", True)),
                 devices=before.get("limitHwid") or 0, ips=before.get("limitIp") or 0)
            after = raw()
            step("тестовый пользователь возвращён как был", all(after.get(k) == before.get(k) for k in FIELDS + KEYS))
        except Exception as exc:  # noqa: BLE001
            step("тестовый пользователь возвращён как был", False, str(exc))
    # 6. settings and servers agree; every link of the subscription works
    try:
        drift = app.fleet().drift()
        step("настройки и серверы совпадают", not drift.get("changes"), "; ".join(drift.get("changes") or []))
        report = app.fleet(lambda *a: None).check_links()
        step(f"все подключения подписки работают", not report.warnings, "; ".join(report.warnings[:5]))
    except Exception as exc:  # noqa: BLE001
        step("проверка серверов", False, f"{type(exc).__name__}: {exc}")
    print("\nИТОГ: " + ("всё работает" if not failed else "не прошло: " + ", ".join(failed)))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(Path(sys.argv[1] if len(sys.argv) > 1 else "fleet.yaml")))
