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
        page.wait_for_function("document.getElementById('syncText').textContent === 'Исправить · 1'")
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
        for tab in ("users", "inbounds", "panels", "backups", "help", "servers"):
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

        # the address book of the page and the server agree (a mistyped name would 404 here)
        assert page.evaluate("typeof renderDomains === 'function' && typeof attentionOf === 'function'")
        browser.close()
    assert not errors, "\n".join(errors)
