"""Editing and deleting users."""
import pytest
import yaml

from xui_fleet import stats
from xui_fleet.config import remove_user_entry, update_user_entry

DOC = {"version": 1, "master": {"name": "ru", "host": "1.1.1.1"}, "nodes": [],
       "users": [{"name": "anna", "comment": "телефон"}, {"name": "boris", "total_gb": 50}]}


def test_page_fields_become_3x_ui_fields():
    f = stats.user_fields({"comment": "  мама   телефон ", "totalGb": "1,5", "expiry": 1_800_000_000_000, "enabled": False})
    assert f == {"comment": "мама телефон", "totalGB": int(1.5 * 1024 ** 3), "expiryTime": 1_800_000_000_000, "enable": False}
    assert stats.user_fields({})["enable"] is True and stats.user_fields({})["totalGB"] == 0


@pytest.mark.parametrize("body", [{"totalGb": -1}, {"totalGb": "много"}, {"expiry": -5}, {"comment": "x" * 201}])
def test_bad_user_fields_are_refused(body):
    with pytest.raises(ValueError):
        stats.user_fields(body)


def test_fleet_yaml_follows_edits_and_forgets_deleted_users(tmp_path):
    path = tmp_path / "fleet.yaml"
    path.write_text(yaml.safe_dump(DOC, allow_unicode=True), encoding="utf-8")
    assert update_user_entry(path, "anna", "", 10.0)
    assert not update_user_entry(path, "vera", "x", 0)  # a user created in the panel only: nothing to keep in step
    assert remove_user_entry(path, "boris") and not remove_user_entry(path, "boris")
    assert yaml.safe_load(path.read_text(encoding="utf-8"))["users"] == [{"name": "anna", "total_gb": 10.0}]
