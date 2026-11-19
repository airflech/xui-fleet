"""The page in a real browser: every script loads, every tab and the main dialogs open, and nothing throws.

Runs where Playwright with Chromium is installed (the CI job «ui» does that); skipped elsewhere. Servers are not
needed: a fleet that is set up in fleet.yaml but not yet applied is enough to draw every part of the page.
"""
import threading
from http.server import ThreadingHTTPServer

import pytest

playwright = pytest.importorskip("playwright.sync_api")

FLEET = """version: 1
master:
  name: ru
  host: 192.0.2.10
  tls:
    mode: domain
    domain: sub.example.com
    reserve_domains: [reserve.example.com]
nodes:
  - name: de
    host: 192.0.2.20
users:
  - name: alice
"""


@pytest.fixture
def page_url(tmp_path, monkeypatch):
    from xui_fleet import service
    from xui_fleet import update as upd
    from xui_fleet.ui import App, make_handler
    data = tmp_path / "data"
    data.mkdir()
    monkeypatch.setattr(service, "APP_DIR", data)
    monkeypatch.setattr(upd, "releases", lambda fresh=False: [])  # no GitHub from tests
    (tmp_path / "fleet.yaml").write_text(FLEET, encoding="utf-8")
    app = App(tmp_path / "fleet.yaml")
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(app))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}/?t={app.token}"
    server.shutdown()


def test_every_tab_and_main_dialog_opens_without_errors(page_url):
    errors = []
    with playwright.sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1100, "height": 800})
        page.on("pageerror", lambda exc: errors.append(f"pageerror: {exc}"))
        page.on("console", lambda m: errors.append(f"console: {m.text}") if m.type == "error" else None)
        page.goto(page_url)
        page.wait_for_function("typeof overview !== 'undefined' && overview && overview.configured")

        # the main server card and the node are drawn
        assert page.locator("#nodeCards .card").count() == 2
        # the route map: the RU server and the node, joined by a line (no live data yet: drawn as "unknown")
        assert page.locator("#routeMap svg .rm-box").count() == 2
        # instead of an unexplained «Применить»: the state, and what fixing it would do
        page.wait_for_function("document.getElementById('driftText').textContent === 'Исправить · 1'")
        page.click("#btnApply")
        page.wait_for_selector("#dlgConfirm[open]")
        assert "главный сервер ещё не настроен" in page.inner_text("#confirmText")
        page.keyboard.press("Escape")
        assert page.locator("#routeMap svg .rm-line.idle").count() >= 2
        page.set_viewport_size({"width": 390, "height": 800})  # phones get the vertical map
        page.wait_for_function("document.querySelector('#routeMap svg').viewBox.baseVal.width === 360")
        page.set_viewport_size({"width": 1100, "height": 800})
        # the brand fonts come from the app itself, Cyrillic included (a missing file rejects the load)
        loaded = page.evaluate("""Promise.all([document.fonts.load("16px 'Inter Tight'", 'Серверы UK'),
                                             document.fonts.load("12px 'JetBrains Mono'", '16 мс')])
                                  .then(r => r.map(faces => faces.length))""")
        assert loaded == [2, 2], loaded  # latin and cyrillic faces of each
        # country flags come from the app too: Windows has none of its own
        assert page.evaluate("""document.fonts.load("26px 'Twemoji Country Flags'", '🇬🇧🇩🇪').then(f => f.length)""") == 1
        for tab in ("users", "inbounds", "panels", "backups", "stats", "help", "servers"):
            page.click(f".tab[data-tab={tab}]")
            assert page.locator(f"section[data-pane={tab}]").is_visible(), tab

        # dialogs that need no server: versions, add server, add user, the job history
        page.click("#appVer")
        page.wait_for_selector("#dlgVersions[open]")
        page.keyboard.press("Escape")
        page.click("#btnAddNode")
        page.wait_for_selector("#dlgNode[open]")
        page.keyboard.press("Escape")
        page.click(".tab[data-tab=users]")
        page.click("#btnAddUser")
        page.wait_for_selector("#dlgUser[open]")
        page.keyboard.press("Escape")
        page.evaluate("document.getElementById('drawer').classList.remove('hidden'); openHistory()")
        page.wait_for_selector("#jobhist:not(.hidden)")
        assert "Операций пока не было" in page.inner_text("#jobhist")

        # a new user's invitation: the link and what to do with it, ready for a messenger
        assert page.evaluate("inviteText('https://x.example/s/abc').includes('https://x.example/s/abc')")
        page.evaluate("openInvite('alice', 'https://x.example/s/abc', true)")
        page.wait_for_selector("#dlgInvite[open]")
        assert "alice" in page.inner_text("#invTitle") and "Установите приложение" in page.inner_text("#invText")
        page.keyboard.press("Escape")

        # quick search: Ctrl+K, Cyrillic finds a Latin name, Enter runs the action
        page.keyboard.press("Control+k")
        page.wait_for_selector("#dlgCmd[open]")
        page.fill("#palInput", "domeny")  # typed in Latin, found in Cyrillic (users need live data: none here)
        assert "Домены и адреса" in page.inner_text("#palList")
        page.fill("#palInput", "тема")
        page.keyboard.press("Enter")
        assert page.evaluate("document.documentElement.dataset.theme") == "light"  # auto → light
        page.click("#themeBtn")
        assert page.evaluate("document.documentElement.dataset.theme") == "dark"
        page.click("#themeBtn")
        assert page.evaluate("document.documentElement.dataset.theme") is None  # back to the system's

        # the address book of the page and the server agree (a mistyped name would 404 here)
        assert page.evaluate("typeof renderDomains === 'function' && typeof attentionOf === 'function'")
        browser.close()
    assert not errors, "\n".join(errors)


def test_first_run_goes_step_by_step_and_draws_the_network(tmp_path, monkeypatch):
    from xui_fleet import service
    from xui_fleet.ui import App, make_handler
    data = tmp_path / "data"
    data.mkdir()
    monkeypatch.setattr(service, "APP_DIR", data)
    app = App(tmp_path / "fleet.yaml")  # nothing set up yet
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(app))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    errors = []
    try:
        with playwright.sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page(viewport={"width": 1100, "height": 800})
            page.on("pageerror", lambda exc: errors.append(f"pageerror: {exc}"))
            page.goto(f"http://127.0.0.1:{server.server_port}/?t={app.token}")
            page.wait_for_selector("#setup:not(.hidden)")
            assert page.locator("#setupMap .rm-box.master.ghost").count() == 1  # drawn before anything is typed
            page.check("#setupRoles input[value=vpn]")
            page.click("[data-step='1'] [data-next]")
            page.fill("#s_host", "198.51.100.7")
            assert "198.51.100.7" in page.inner_text("#setupMap")
            page.click("[data-step='2'] [data-next]")  # no SSH password yet: stays
            assert page.get_attribute("[data-step='2']", "class").split().count("active") == 1
            page.fill("[data-step='2'] [name=sshPassword]", "secret")
            page.click("[data-step='2'] [data-next]")
            assert "active" in page.get_attribute("[data-step='3']", "class")
            assert page.inner_text("[data-step='1'] .sum") == "сам VPN"
            browser.close()
    finally:
        server.shutdown()
    assert not errors, "\n".join(errors)
