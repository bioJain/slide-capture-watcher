"""Session folder naming and durable per-capture comments."""

from __future__ import annotations

import json
import os
import re
import tempfile
from datetime import date
from pathlib import Path
from typing import Mapping

NOTES_FILENAME = "notes.json"
_INVALID_WINDOWS_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def safe_session_name(name: str) -> str:
    """Return a Windows-safe folder-name component."""
    cleaned = _INVALID_WINDOWS_CHARS.sub("_", name.strip())
    cleaned = re.sub(r"\s+", "_", cleaned).strip(" ._")
    return cleaned[:80].rstrip(" ._")


def session_directory(base: Path, name: str, today: date | None = None) -> Path:
    """Build ``YYYY-MM-DD_name`` below *base* (or return base for a blank name)."""
    component = safe_session_name(name)
    if not component:
        return Path(base)
    return Path(base) / f"{today or date.today():%Y-%m-%d}_{component}"


def load_notes(directory: Path) -> dict[str, str]:
    """Read notes.json. Missing files are empty; malformed contents raise ValueError."""
    path = Path(directory) / NOTES_FILENAME
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{path} 파일을 읽을 수 없습니다: {exc}") from exc
    if not isinstance(data, dict) or any(not isinstance(k, str) or not isinstance(v, str) for k, v in data.items()):
        raise ValueError(f"{path} 형식이 올바르지 않습니다 (파일명과 코멘트 문자열의 매핑이어야 합니다)")
    return data


def save_notes(directory: Path, notes: Mapping[str, str]) -> Path:
    """Atomically write non-empty comments to the session's notes.json."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / NOTES_FILENAME
    contents = {str(name): text for name, text in notes.items() if text}
    fd, temporary = tempfile.mkstemp(prefix=".notes-", suffix=".json.tmp", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(contents, stream, ensure_ascii=False, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise
    return path
