"""The documentation as one PDF: build the site, open its one-page print version in Chromium and print it.

    python scripts/build_docs_pdf.py [--out xui-fleet-docs.pdf] [--version 0.19.6]

Needs the «docs» extra (pip install -e ".[docs]") and Chromium for Playwright (python -m playwright install chromium).
The same Markdown makes the site (mkdocs build) — the PDF never drifts from it.
"""
from __future__ import annotations

import argparse
import functools
import os
import subprocess
import sys
import tempfile
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]



def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="xui-fleet-docs.pdf", type=Path)
    ap.add_argument("--version", default="")
    a = ap.parse_args()
    from playwright.sync_api import sync_playwright

    with tempfile.TemporaryDirectory() as tmp:
        site = Path(tmp) / "site"
        env = dict(os.environ, NO_MKDOCS_2_WARNING="1")
        if a.version:
            env["DOCS_VERSION"] = f"версия {a.version}"
        subprocess.run([sys.executable, "-m", "mkdocs", "build", "--strict", "-q", "-d", str(site)],
                       cwd=ROOT, env=env, check=True)
        class Quiet(SimpleHTTPRequestHandler):
            def log_message(self, *args) -> None:  # no line per file in the build log
                pass
        server = ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(Quiet, directory=str(site)))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch()
                page = browser.new_page()
                page.emulate_media(media="print")
                page.goto(f"http://127.0.0.1:{server.server_port}/print_page/", wait_until="networkidle")
                page.evaluate("document.fonts.ready")
                a.out.parent.mkdir(parents=True, exist_ok=True)
                # page size, margins and the footer with page numbers come from the CSS @page (docs/stylesheets)
                page.pdf(path=str(a.out), print_background=True, prefer_css_page_size=True)
                browser.close()
        finally:
            server.shutdown()
    print(f"PDF: {a.out} ({a.out.stat().st_size // 1024} КБ)")


if __name__ == "__main__":
    main()
