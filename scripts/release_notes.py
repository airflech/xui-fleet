"""Release notes for a tag, from the commit messages since the previous version.

Releases here are commits pushed to main, not pull requests, so GitHub's --generate-notes finds nothing and
leaves only a "Full Changelog" link. The commit messages are written for people, so they are the notes.

    python3 scripts/release_notes.py v0.5.2 [owner/repo]   # prints Markdown

A stable version is compared with the previous stable one (its betas are included in it); a prerelease
with the version right before it.
"""
from __future__ import annotations

import re
import subprocess
import sys
from typing import List, Optional, Tuple

TAG = re.compile(r"^v(\d+)\.(\d+)\.(\d+)(?:-([0-9A-Za-z.]+))?$")
TRAILERS = re.compile(r"^(Co-Authored-By|Signed-off-by|Reviewed-by):", re.I)


def _key(tag: str) -> Tuple:
    m = TAG.match(tag)
    if not m:
        return ()
    pre = m.group(4)
    return (int(m.group(1)), int(m.group(2)), int(m.group(3)), pre is None, _pre_key(pre))


def _pre_key(pre: Optional[str]) -> Tuple:
    if pre is None:
        return ()
    return tuple((0, int(p)) if p.isdigit() else (1, p) for p in pre.split("."))


def is_prerelease(tag: str) -> bool:
    m = TAG.match(tag)
    return bool(m and m.group(4))


def previous(tag: str, tags: List[str]) -> Optional[str]:
    """The release this one follows: the latest older stable one (any older one for a prerelease)."""
    older = [t for t in tags if TAG.match(t) and t != tag and _key(t) < _key(tag)
             and (is_prerelease(tag) or not is_prerelease(t))]
    return max(older, key=_key) if older else None


def render(tag: str, prev: Optional[str], commits: List[Tuple[str, str]], repo: str) -> str:
    version = tag[1:]
    lines = ["## Что нового", ""]
    for subject, body in commits:
        lines += [f"### {subject.strip()}", ""]
        kept = [line.rstrip() for line in body.strip().splitlines() if not TRAILERS.match(line.strip())]
        while kept and not kept[-1]:
            kept.pop()
        if kept:
            lines += kept + [""]
    if not commits:
        lines += ["Изменений в коде нет.", ""]
    lines += ["---", "",
              f"Установка: скачайте xui-fleet-{version}.zip, распакуйте и запустите файл для своей системы. "
              "Установленные копии обновятся сами."]
    if prev:
        lines += ["", f"Все изменения: https://github.com/{repo}/compare/{prev}...{tag}"]
    return "\n".join(lines) + "\n"


def _git(*args: str) -> str:
    return subprocess.run(["git", *args], check=True, capture_output=True, text=True, encoding="utf-8").stdout


def commits_between(prev: Optional[str], tag: str) -> List[Tuple[str, str]]:
    out = _git("log", "--no-merges", "--reverse", "--format=%s%x00%b%x1e", f"{prev}..{tag}" if prev else tag)
    result = []
    for record in out.split("\x1e"):
        if "\x00" in record:
            subject, body = record.strip("\n").split("\x00", 1)
            result.append((subject, body))
    return result


def notes_for(tag: str, repo: str) -> str:
    prev = previous(tag, _git("tag", "--list", "v*").split())
    return render(tag, prev, commits_between(prev, tag), repo)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        raise SystemExit("usage: release_notes.py vX.Y.Z [owner/repo]")
    sys.stdout.write(notes_for(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else "airflech/xui-fleet"))
