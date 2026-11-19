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
import json
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
