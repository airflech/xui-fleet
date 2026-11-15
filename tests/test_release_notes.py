"""Release notes come from the commit messages since the previous version."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import release_notes as rn  # noqa: E402

TAGS = ["v0.2.4", "v0.2.5-beta.1", "v0.2.5", "v0.2.6", "v0.2.10", "v0.2.11", "v0.3.0", "junk", "v1.0.0-rc.2", "v1.0.0-rc.10"]


def test_a_stable_version_follows_the_previous_stable_one_and_a_prerelease_the_one_before_it():
    assert rn.previous("v0.2.5", TAGS) == "v0.2.4"  # the beta's changes belong to 0.2.5 too
    assert rn.previous("v0.2.5-beta.1", TAGS) == "v0.2.4"
    assert rn.previous("v0.3.0", TAGS) == "v0.2.11"  # numbers, not text: 0.2.11 > 0.2.6
    assert rn.previous("v1.0.0-rc.10", TAGS) == "v1.0.0-rc.2"
    assert rn.previous("v1.0.0", TAGS) == "v0.3.0"
    assert rn.previous("v0.2.4", TAGS) is None


def test_notes_list_every_commit_without_trailers_and_link_the_comparison():
    notes = rn.render("v0.5.1", "v0.5.0", [
        ("Профили и выход; версия 0.5.1", "Несколько наборов серверов.\n\nCo-Authored-By: Bot <x@y>\n"),
        ("Тесты не обращаются к GitHub", ""),
    ], "airflech/xui-fleet")
    assert notes.startswith("## Что нового\n\n### Профили и выход; версия 0.5.1\n\nНесколько наборов серверов.\n")
    assert "Co-Authored-By" not in notes and "### Тесты не обращаются к GitHub" in notes
    assert "xui-fleet-0.5.1.zip" in notes and notes.rstrip().endswith("compare/v0.5.0...v0.5.1")


def test_a_release_without_new_commits_says_so():
    assert "Изменений в коде нет." in rn.render("v0.1.0", None, [], "a/b")
