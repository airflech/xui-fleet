"""Screenshots for the documentation, taken from a running interface with real data — but nothing real gets in.

Every answer of the interface's API (and, before each shot, every text on the page) goes through one replacement
list: server addresses, domains, user names, subscription ids, keys, panel paths and ports become example values
(192.0.2.x / 198.51.100.x / 203.0.113.x — addresses reserved for documentation, example.com names, «Анна», …).
The list is built at run time from the profile itself and is never written anywhere.

    PYTHONPATH=src python scripts/doc_screens.py --url http://127.0.0.1:8791/?t=… --config путь/fleet.yaml
        [--out docs/assets/screens] [--only servers,users]

Look at every picture before publishing it: the replacement covers what the program knows about, not what someone
typed into a comment by hand.
"""
from __future__ import annotations

import argparse
import itertools
import json
import re
import sys
import time
from pathlib import Path
from typing import Callable, Dict, List, Tuple

NAMES = ["anna", "boris", "vera", "gleb", "dasha", "egor", "zhenya", "ivan", "katya", "lev", "masha", "nikita",
         "olya", "pasha", "rita", "sasha", "tanya", "ulyana", "fedor", "yana", "artem", "polina", "denis", "sonya",
         "kirill", "liza", "maxim", "nina", "oleg", "inna", "roman", "alla"]

IPV4 = re.compile(r"(?<![\d.])(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})(?![\d./])")  # «/21»: a public network (Valve), not a server
UUID = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
KEEP_IP = {"127.0.0.1", "0.0.0.0", "1.1.1.1", "8.8.8.8", "255.255.255.255", "1.2.3.4", "1.2.3.0"}


class Masker:
    """One consistent mapping real → example for the whole run (the same IP is the same example everywhere)."""

    def __init__(self) -> None:
        self.pairs: List[Tuple[str, str]] = []
        self.names: Dict[str, str] = {}  # example user name → real one: what the page asks for goes back real
        self.ips: Dict[str, str] = {}
        self._ip_pool = itertools.chain((f"203.0.113.{i}" for i in range(10, 250)),
                                        (f"198.51.100.{i}" for i in range(10, 250)))

    def add(self, real: object, fake: str) -> None:
        real = str(real or "").strip()
        if len(real) >= 3 and real != fake and all(real != r for r, _ in self.pairs):
            self.pairs.append((real, fake))

    def ip(self, real: str) -> str:
        if real not in self.ips:
            self.ips[real] = next(self._ip_pool)
        return self.ips[real]

    def __call__(self, text: str) -> str:
        for real, fake in sorted(self.pairs, key=lambda p: -len(p[0])):
            if real in text:
                text = text.replace(real, fake)
        text = IPV4.sub(lambda m: m.group(0) if m.group(0) in KEEP_IP or m.group(0).startswith(("192.0.2.", "198.51.100.", "203.0.113.", "10.", "192.168.", "172.16."))
                        else self.ip(m.group(0)), text)
        return UUID.sub("00000000-0000-4000-8000-000000000000", text)


def build_masker(config: Path) -> Masker:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
    from xui_fleet.engine import Fleet
    m = Masker()
    fleet = Fleet(config, log=lambda _m: None)
    cfg = fleet.cfg
    tls = cfg.tls_for(cfg.master)
    m.add(tls.domain, "sub.example.com")
    for i, d in enumerate(tls.reserve_domains or [], 1):
        m.add(d, f"reserve{i if i > 1 else ''}.example.com")
    for d in (tls.domain, *(tls.reserve_domains or [])):  # bare second-level parts: the brand in a site title
        if d and d.count(".") >= 1:
            m.add(d.split(".")[-2], "example")
    # this computer: the user's home folder shows up in paths (backups, logs) — plain and as JSON escapes it
    home = str(Path.home())
    for form in (home, home.replace("\\", "/"), home.replace("\\", "\\\\")):
        m.add(form, "C:\\\\Users\\\\user" if "\\\\" in form else ("C:/Users/user" if "/" in form else "C:\\Users\\user"))
    if len(Path.home().name) >= 4:
        m.add(Path.home().name, "user")
    vpn = getattr(cfg.clients, "vpn_name", "") or ""
    m.add(vpn, "Мой VPN")
    sup = str(getattr(cfg.clients, "support_url", "") or "")
    if "t.me/" in sup:
        m.add(sup.split("t.me/")[-1].strip("/@ "), "my_support")
    # what the profile knows about each server: panel paths and ports, Reality neighbours, sub-gate paths
    for name in [cfg.master.name, *[n.name for n in cfg.nodes]]:
        st = fleet.state.server(name) or {}
        bp = str(st.get("base_path") or "").strip("/")
        m.add(bp, "panel-path")
        for k in ("panel_port",):
            if st.get(k):
                m.add(f":{st[k]}", ":2053")
        nb = st.get("reality_neighbor") or {}
        m.add(nb.get("name"), f"neighbour-{len(m.pairs)}.example.net")
        gate = st.get("sub_gate") or {}
        for part in str(gate.get("uri") or "").split("/"):
            if len(part) >= 8 and "." not in part and ":" not in part:
                m.add(part, "sub-path")
    # users: names, comments, subscription ids, Telegram ids
    try:
        with fleet.master_panel(provision=False) as panel:
            names = iter(NAMES)
            for c in panel.clients():
                email = str(c.get("email") or "")
                if email.startswith("fleet-"):
                    continue
                fake = next(names, f"user{len(m.pairs)}")
                m.add(email, fake)
                m.names[fake] = email
                m.add(c.get("subId"), "s" + "0" * 15)
                m.add(c.get("comment"), "")
                if c.get("tgId"):
                    m.add(str(c["tgId"]), "100000000")
            s = panel.settings()
            for k in ("subPath", "subJsonPath", "webBasePath"):
                v = str(s.get(k) or "").strip("/")
                if len(v) >= 6:
                    m.add(v, "sub-path" if k.startswith("sub") else "panel-path")
    except Exception as exc:  # no panel: addresses and domains are still hidden, names are not — say so
        print(f"⚠ список пользователей не получен ({exc}); имена пользователей НЕ скрыты — не публикуйте экраны с ними")
    return m


DOM_PASS = """(pairs) => {
  const ip = /(^|[^\\d.])(\\d{1,3}\\.\\d{1,3}\\.\\d{1,3}\\.\\d{1,3})(?![\\d./])/g;
  const fix = (s) => { for (const [a, b] of pairs) if (s.includes(a)) s = s.split(a).join(b); return s; };
  const w = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  const left = [];
  while (w.nextNode()) {
    const n = w.currentNode; const v = fix(n.nodeValue); if (v !== n.nodeValue) n.nodeValue = v;
    for (const m of n.nodeValue.matchAll(ip)) if (!/^(127\\.|0\\.0\\.0\\.0|192\\.0\\.2\\.|198\\.51\\.100\\.|203\\.0\\.113\\.|10\\.|192\\.168\\.|1\\.1\\.1\\.1|1\\.2\\.3\\.)/.test(m[2])) left.push(m[2]);
  }
  for (const el of document.querySelectorAll('input, textarea, a, [title]')) {
    if ('value' in el && el.value) el.value = fix(el.value);
    if (el.getAttribute('href')) el.setAttribute('href', fix(el.getAttribute('href')));
    if (el.getAttribute('title')) el.setAttribute('title', fix(el.getAttribute('title')));
  }
  return left;
}"""


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", required=True)
    ap.add_argument("--config", required=True, type=Path)
    ap.add_argument("--out", default="docs/assets/screens", type=Path)
    ap.add_argument("--only", default="")
    a = ap.parse_args()
    from playwright.sync_api import sync_playwright

    mask = build_masker(a.config)
    a.out.mkdir(parents=True, exist_ok=True)
    only = {x for x in a.only.split(",") if x}

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1280, "height": 860}, device_scale_factor=2, locale="ru-RU")

        from urllib.parse import quote

        def unmask_url(url: str) -> str:
            for fake, real in mask.names.items():
                url = re.sub(rf"(?<=[/=]){re.escape(fake)}(?=$|[/?&#])", quote(real, safe=""), url)
            return url

        def unmask_body(body: str) -> str:
            for fake, real in mask.names.items():
                body = body.replace(json.dumps(fake), json.dumps(real, ensure_ascii=False))
            return body

        def masked(route) -> None:
            req = route.request
            body = req.post_data
            r = route.fetch(url=unmask_url(req.url), post_data=unmask_body(body) if body else None)
            ctype = r.headers.get("content-type", "")
            if any(t in ctype for t in ("json", "text", "javascript", "html")):
                route.fulfill(response=r, body=mask(r.text()))
            else:
                route.fulfill(response=r)

        page.route(re.compile(r".*/api/.*"), masked)
        page.goto(a.url)
        page.wait_for_function("typeof overview !== 'undefined' && overview && overview.configured", timeout=60000)
        page.wait_for_timeout(1500)

        def shot(name: str, prepare: Callable[[], None], target: str = "", wait: int = 800) -> None:
            if only and name not in only:
                return
            prepare()
            page.wait_for_timeout(wait)
            left = page.evaluate(DOM_PASS, [[r, f] for r, f in sorted(mask.pairs, key=lambda x: -len(x[0]))])
            if left:
                print(f"⚠ {name}: остались адреса {sorted(set(left))} — экран не сохранён")
                return
            path = a.out / f"{name}.png"
            if target:
                page.locator(target).first.screenshot(path=str(path))
            else:
                page.screenshot(path=str(path))
            print(f"✓ {path}")

        # what a person sees when they open their link in a browser: the real page, served at an example address
        if not only or "sub-page" in only:
            from xui_fleet import provision as pv
            phone = browser.new_page(viewport={"width": 390, "height": 1000}, device_scale_factor=2, locale="ru-RU",
                                     user_agent="Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 Mobile/15E148")
            html_page = pv.unsupported_page(["happ", "incy"], title="Мой VPN", support="https://t.me/my_support")
            phone.route("https://sub.example.com/**", lambda r: r.fulfill(body=html_page, content_type="text/html; charset=utf-8"))
            phone.goto("https://sub.example.com/sub-path/s000000000000000")
            phone.wait_for_timeout(500)
            phone.screenshot(path=str(a.out / "sub-page.png"), full_page=True)
            print(f"✓ {a.out / 'sub-page.png'}")
            phone.close()

        for name, prepare, target, wait in SHOTS(page):
            try:
                shot(name, prepare, target, wait)
            except Exception as exc:
                print(f"✗ {name}: {exc}")
            page.keyboard.press("Escape")
            page.wait_for_timeout(300)
        browser.close()


def tab(page, name: str) -> Callable[[], None]:
    def go() -> None:
        page.click(f'[data-tab="{name}"]')
        page.wait_for_timeout(1200)
    return go


def js(page, code: str, before: str = "servers") -> Callable[[], None]:
    def go() -> None:
        tab(page, before)()
        page.evaluate(code)
    return go


def click(page, *selectors: str, before: str = "servers") -> Callable[[], None]:
    def go() -> None:
        tab(page, before)()
        for s in selectors:
            page.locator(s).first.click()
            page.wait_for_timeout(700)
    return go


DIALOG = "dialog[open]"
NODE = "(overview.nodes.find((n) => n.label && /Germany|DE/.test(n.label)) || overview.nodes[overview.nodes.length - 1])"


def SHOTS(page):
    """(file name, how to get there, element to crop to or "" for the whole window, extra wait ms)."""
    return [
        ("servers", tab(page, "servers"), "", 6000),
        ("route-map", tab(page, "servers"), "#routeMap", 500),
        ("server-cards", tab(page, "servers"), "#nodeCards", 500),
        ("maintenance-menu", click(page, "button:has-text('Обслуживание')"), "", 400),
        ("users", tab(page, "users"), "", 2500),
        ("user-card", click(page, "[data-pane=users] .item:has(.avatar) .title", before="users"), DIALOG, 3500),
        ("inbounds", tab(page, "inbounds"), "", 2000),
        ("panels", tab(page, "panels"), "", 1500),
        ("backups", tab(page, "backups"), "", 1500),
        ("stats", tab(page, "stats"), "", 3000),
        ("help", tab(page, "help"), "", 800),
        ("dlg-add-server", click(page, "button:has-text('Добавить сервер')"), DIALOG, 600),
        ("dlg-add-user", click(page, "button:has-text('Добавить пользователя')", before="users"), DIALOG, 600),
        ("dlg-invite", js(page, "openInvite('anna', 'https://sub.example.com:2096/sub-path/s000000000000000', true)",
                          before="users"), DIALOG, 600),
        ("dlg-role", js(page, "openRole()"), DIALOG, 600),
        ("dlg-domains", js(page, "renderDomains(); $('dlgDomains').showModal()"), DIALOG, 2500),
        ("dlg-direct", js(page, "openDirect()"), DIALOG, 2000),
        ("dlg-warp", js(page, "$('dlgWarp').showModal()"), DIALOG, 600),
        ("dlg-versions", js(page, "openVersions()"), DIALOG, 2500),
        ("dlg-sync", js(page, "openSync()", before="backups"), DIALOG, 1500),
        ("dlg-route", js(page, f"openRoute({NODE}.name)"), DIALOG, 3500),
        ("dlg-label", js(page, f"openLabel({NODE})"), DIALOG, 600),
        ("dlg-access", js(page, f"openAccess({NODE}, 'Адрес и доступ по SSH')"), DIALOG, 600),
        ("dlg-remove", js(page, f"openRemove({NODE})"), DIALOG, 600),
        ("dlg-app-export", js(page, "$('dlgAppExport').showModal()", before="backups"), DIALOG, 600),
        ("dlg-migrate", js(page, "openMigrate()", before="backups"), DIALOG, 800),
    ]


if __name__ == "__main__":
    main()
