import json
from datetime import date

import pytest

from session_store import load_notes, safe_session_name, save_notes, session_directory


def test_session_directory_uses_date_and_safe_name(tmp_path):
    result = session_directory(tmp_path, " 홍길동 / 세미나 ", date(2026, 9, 16))
    assert result == tmp_path / "2026-09-16_홍길동___세미나"
    assert safe_session_name('bad<>:"/\\|?* name') == "bad__________name"


def test_blank_session_name_preserves_base_for_compatibility(tmp_path):
    assert session_directory(tmp_path, "  ") == tmp_path


def test_notes_round_trip_unicode_and_remove_empty(tmp_path):
    path = save_notes(tmp_path, {"slide_1.png": "핵심 내용", "slide_2.png": ""})
    assert path.name == "notes.json"
    assert load_notes(tmp_path) == {"slide_1.png": "핵심 내용"}
    assert json.loads(path.read_text(encoding="utf-8")) == {"slide_1.png": "핵심 내용"}


def test_missing_notes_is_empty(tmp_path):
    assert load_notes(tmp_path) == {}


@pytest.mark.parametrize("value", [[], {"slide.png": 3}])
def test_invalid_notes_are_rejected(tmp_path, value):
    (tmp_path / "notes.json").write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(ValueError, match="형식이 올바르지"):
        load_notes(tmp_path)
