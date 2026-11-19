# -*- coding: utf-8 -*-
"""
Актуальный патч Dota 2.

Источник — официальный фид dota2.com (та же страница
https://www.dota2.com/patches/): `datafeed/patchnoteslist`.

Особенности:
  • `current()` никогда не ходит в сеть — отдаёт кеш (или патч из
    patch_static.json), поэтому стартовый экран не подвисает;
  • `refresh()` скачивает список патчей, вызывается в фоне при старте
    и потом раз в несколько часов;
  • если dota2.com недоступен, остаётся последнее известное значение.
"""
import html as _html
import json
import re
import threading
import time
from pathlib import Path

from curl_cffi import requests as crequests

URL = "https://www.dota2.com/datafeed/patchnoteslist?language=english"
_TTL = 6 * 3600
_HERE = Path(__file__).parent

_cur = None
_fetched_at = 0.0


def _static() -> dict:
    try:
        data = json.loads((_HERE / "patch_static.json").read_text(encoding="utf-8"))
        if data.get("number"):
            return {"number": str(data["number"]),
                    "timestamp": int(data.get("timestamp") or 0)}
    except Exception:  # noqa: BLE001
        pass
    return {"number": "—", "timestamp": 0}


def current() -> dict:
    """Последний известный патч (без обращения к сети)."""
    return _cur or _static()


def refresh(force: bool = False) -> dict:
    """Обновляет сведения о патче с dota2.com. Ошибки не пробрасывает."""
    global _cur, _fetched_at
    if not force and _cur and time.time() - _fetched_at < _TTL:
        return _cur
    try:
        r = crequests.get(URL, impersonate="chrome", timeout=20)
        if r.status_code != 200:
            raise RuntimeError(f"HTTP {r.status_code}")
        patches = [p for p in (r.json().get("patches") or [])
                   if p.get("patch_number")]
        if not patches:
            raise RuntimeError("пустой список патчей")
        latest = max(patches, key=lambda p: p.get("patch_timestamp") or 0)
        _cur = {"number": str(latest["patch_number"]),
                "timestamp": int(latest.get("patch_timestamp") or 0)}
        _fetched_at = time.time()
    except Exception:  # noqa: BLE001
        if _cur is None:
            _cur = _static()
            _fetched_at = time.time()
    return _cur


def date_text(ts: int) -> str:
    if not ts:
        return ""
    return time.strftime("%d.%m.%Y", time.gmtime(ts + 3 * 3600))


def badge() -> str:
    """Строка для сообщений: «🩹 Патч 7.42 · 18.11.2026»."""
    p = current()
    if not p.get("number") or p["number"] == "—":
        return ""
    d = date_text(p.get("timestamp") or 0)
    return f"🩹 Патч <b>{p['number']}</b>" + (f" · {d}" if d else "")


# ---------------------------------------------------------------------------
# Изменения героев в текущем патче
# ---------------------------------------------------------------------------
# Общий фид патчноутов отдаёт всё дерево изменений: база героя, способности,
# таланты. Названия способностей лежат в отдельном фиде herodata — тянем их
# только для тех героев, чью карточку реально открыли, и кешируем id → имя.
NOTES_URL = ("https://www.dota2.com/datafeed/patchnotes"
             "?version={version}&language=english")
HERODATA_URL = ("https://www.dota2.com/datafeed/herodata"
                "?language=english&hero_id={hero_id}")

_DATA_DIR = _HERE / "data"
_notes_lock = threading.Lock()
_notes_cache: dict = {}          # версия патча -> {hero_id: узел фида}
_ability_names: dict = {}        # ability_id -> английское название
_ability_loaded = False


def _clean(text) -> str:
    """Убирает HTML из заметок патча и склеивает пробелы."""
    if not text:
        return ""
    s = re.sub(r"<br\s*/?>", " ", str(text), flags=re.I)
    s = re.sub(r"<[^>]+>", "", s)
    s = _html.unescape(s)
    return re.sub(r"\s+", " ", s).strip()


def _texts(items) -> list:
    """Собирает все строки заметок из вложенной структуры фида."""
    out = []
    if isinstance(items, list):
        for it in items:
            out.extend(_texts(it))
    elif isinstance(items, dict):
        note = _clean(items.get("note"))
        if note:
            out.append(note)
        for key in ("sub_notes", "notes", "generic"):
            if key in items:
                out.extend(_texts(items[key]))
    elif isinstance(items, str):
        note = _clean(items)
        if note:
            out.append(note)
    return out


def _load_notes(version: str) -> dict:
    """Заметки патча (кеш: память -> файл в data/ -> dota2.com)."""
    hit = _notes_cache.get(version)
    if hit:
        return hit
    with _notes_lock:
        hit = _notes_cache.get(version)
        if hit:
            return hit
        f = _DATA_DIR / f"patchnotes_{version}.json"
        heroes = None
        try:
            heroes = json.loads(f.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            pass
        if not isinstance(heroes, list) or not heroes:
            r = crequests.get(NOTES_URL.format(version=version),
                              impersonate="chrome", timeout=25)
            if r.status_code != 200:
                raise RuntimeError(f"HTTP {r.status_code}")
            heroes = r.json().get("heroes") or []
            try:
                _DATA_DIR.mkdir(parents=True, exist_ok=True)
                f.write_text(json.dumps(heroes, ensure_ascii=False),
                             encoding="utf-8")
            except Exception:  # noqa: BLE001
                pass
        data = {h.get("hero_id"): h for h in heroes if h.get("hero_id")}
        _notes_cache[version] = data
        return data


def _load_ability_names() -> dict:
    global _ability_loaded
    if _ability_loaded:
        return _ability_names
    with _notes_lock:
        if _ability_loaded:
            return _ability_names
        try:
            raw = json.loads((_DATA_DIR / "ability_names.json")
                             .read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                _ability_names.update({int(k): v for k, v in raw.items()})
        except Exception:  # noqa: BLE001
            pass
        _ability_loaded = True
    return _ability_names


def _ability_map(hero_id: int) -> dict:
    """ability_id -> название способности (фид herodata, кеш на диске)."""
    names = _load_ability_names()
    try:
        r = crequests.get(HERODATA_URL.format(hero_id=hero_id),
                          impersonate="chrome", timeout=20)
        if r.status_code != 200:
            return names
        result = r.json().get("result") or {}
        heroes = (result.get("data") or {}).get("heroes") or []
        new = {}
        for a in (heroes[0].get("abilities") or []) if heroes else []:
            aid, name = a.get("id"), a.get("name_loc")
            if aid and name:
                new[int(aid)] = name
        if new:
            names.update(new)
            try:
                _DATA_DIR.mkdir(parents=True, exist_ok=True)
                (_DATA_DIR / "ability_names.json").write_text(
                    json.dumps({str(k): v for k, v in names.items()},
                               ensure_ascii=False), encoding="utf-8")
            except Exception:  # noqa: BLE001
                pass
    except Exception:  # noqa: BLE001
        pass
    return names


def hero_changes(hero_id: int, limit: int = 40) -> dict | None:
    """Изменения героя в текущем патче.

    Возвращает {"version", "items": [(заголовок, текст), ...], "total"}
    или None, если героя нет в патчноутах.
    """
    ver = (current().get("number") or "").strip()
    if not ver or ver == "—" or not hero_id:
        return None
    try:
        notes = _load_notes(ver)
    except Exception:  # noqa: BLE001
        return None
    hero = notes.get(int(hero_id))
    if not hero:
        return None

    items = [("База", t) for t in _texts(hero.get("hero_notes"))]

    abilities = []
    for ab in hero.get("abilities") or []:
        found = _texts(ab.get("ability_notes"))
        if found:
            abilities.append((ab.get("ability_id"), found))
    if abilities:
        names = _ability_map(int(hero_id))
        for aid, found in abilities:
            name = names.get(int(aid)) if aid else None
            for t in found:
                items.append((name or "Способность", t))

    items += [("Таланты", t) for t in _texts(hero.get("talent_notes"))]
    if not items:
        return None
    return {"version": ver, "items": items[:limit], "total": len(items)}
