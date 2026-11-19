# -*- coding: utf-8 -*-
"""
Мини-хранилище состояния бота (избранные герои).

На GitHub Actions диск между запусками не сохраняется, поэтому файл
`data/state.json` кешируется workflow-ом (actions/cache restore/save)
и возвращается при следующем старте. Локально просто лежит рядом с ботом.
"""
import json
import threading
from pathlib import Path

_HERE = Path(__file__).parent
_DIR = _HERE / "data"
_FILE = _DIR / "state.json"

_lock = threading.Lock()
_data = None


def _load() -> dict:
    global _data
    if _data is None:
        try:
            data = json.loads(_FILE.read_text(encoding="utf-8"))
            _data = data if isinstance(data, dict) else {}
        except Exception:  # noqa: BLE001
            _data = {}
    return _data


def _save() -> None:
    try:
        _DIR.mkdir(parents=True, exist_ok=True)
        tmp = _FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(_data or {}, ensure_ascii=False, indent=0),
                       encoding="utf-8")
        tmp.replace(_FILE)
    except Exception:  # noqa: BLE001
        pass


def init() -> None:
    """Создаёт пустой data/state.json, чтобы кеш workflow всегда что-то сохранял."""
    with _lock:
        _load()
        if not _FILE.exists():
            _save()


def _chat(chat_id) -> list:
    favs = _load().setdefault("favs", {})
    return favs.setdefault(str(chat_id), [])


def favorites(chat_id) -> list[str]:
    """Список slug-ов избранных героев (в порядке добавления)."""
    with _lock:
        return list(_chat(chat_id))


def is_favorite(chat_id, slug: str) -> bool:
    with _lock:
        return slug in _chat(chat_id)


def toggle_favorite(chat_id, slug: str) -> bool:
    """True — добавили, False — убрали."""
    with _lock:
        lst = _chat(chat_id)
        if slug in lst:
            lst.remove(slug)
            added = False
        else:
            lst.append(slug)
            added = True
        _save()
        return added


def remove_favorite(chat_id, slug: str) -> None:
    with _lock:
        lst = _chat(chat_id)
        if slug in lst:
            lst.remove(slug)
            _save()
