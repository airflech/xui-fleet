"""The interface API as a table: fields are checked before anything runs, and the page only calls what exists."""
import re
from importlib import resources

import pytest

from xui_fleet import api


def test_fields_are_checked_and_normalised_with_a_message_for_the_person():
    body = api.check({"name": "  Аня ", "id": "7", "devices": "", "mode": "both"},
                     {"name": "str", "id": "int", "devices": "int?", "mode": "enum:main|both|reserve"})
    assert body == {"name": "Аня", "id": 7, "devices": "", "mode": "both"}
    with pytest.raises(ValueError, match="не заполнено поле «имя»"):
        api.check({"name": "   "}, {"name": "str"})
    with pytest.raises(ValueError, match="целым числом"):
        api.check({"id": "семь"}, {"id": "int"})
    with pytest.raises(ValueError, match="отрицательным"):
        api.check({"devices": -1}, {"devices": "int"})
    with pytest.raises(ValueError, match="допустимо main, both, reserve"):
        api.check({"mode": "all"}, {"mode": "enum:main|both|reserve"})
    with pytest.raises(AssertionError):
        api.check({"x": 1}, {"x": "float"})  # a typo in the table fails loudly


def test_every_declared_field_type_in_the_table_is_known():
    for route in list(api.GET.values()) + list(api.POST.values()) + [r for _p, r in api.PREFIX_POST]:
        for key, kind in route.needs.items():
            kind = kind.rstrip("?")
            assert kind in ("str", "int", "bool") or kind.startswith("enum:"), (route.fn.__name__, key, kind)


def _page_sources():
    root = resources.files("xui_fleet")
    texts = [root.joinpath("ui.html").read_text(encoding="utf-8")]
    static = root.joinpath("static")
    if static.is_dir():
        texts += [p.read_text(encoding="utf-8") for p in static.iterdir() if p.name.endswith(".js")]
    return "\n".join(texts)


def test_every_address_the_page_calls_exists():
    src = _page_sources()
    called = set(re.findall(r"""['"](/api/[a-z/]+)""", src))
    assert len(called) > 30
    for path in called:
        if path in ("/api/ping", "/api/app/export"):
            continue  # answered by the server itself: plain text and a file
        exists = path in api.GET or path in api.POST or any(path.startswith(p) for p, _r in api.PREFIX_POST)
        assert exists, f"страница вызывает {path}, а такого адреса нет"


class _App:
    class job:
        running = False

    def overview(self):
        return {"ok": 1}


def test_dispatch_refuses_unknown_addresses_and_busy_states():
    app = _App()
    with pytest.raises(api.NotFound):
        api.dispatch(app, "GET", "/api/nope", {})
    assert api.dispatch(app, "GET", "/api/overview", {}) == {"ok": 1}
    app.job.running = True
    try:
        assert api.dispatch(app, "GET", "/api/inbounds", {}) == {"busy": True}
        with pytest.raises(RuntimeError, match="уже выполняется"):
            api.dispatch(app, "POST", "/api/user/edit", {"name": "x"})
    finally:
        app.job.running = False


def test_the_page_files_and_fonts_are_served_and_nothing_else():
    from xui_fleet.ui import _static
    body, ctype = _static("fonts/inter-tight-cyrillic.woff2")
    assert body[:4] == b"wOF2" and ctype == "font/woff2"
    assert _static("fonts/jetbrains-mono-LICENSE.txt")[0].startswith(b"Copyright")
    assert _static("core.js")[1].startswith("text/javascript")
    for bad in ("../ui.py", "fonts/../../ui.py", "fonts/x.ttf", "Core.js", "fonts/a/b.woff2", "nope.js"):
        assert _static(bad) is None, bad
