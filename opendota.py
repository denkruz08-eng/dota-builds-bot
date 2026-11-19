# -*- coding: utf-8 -*-
"""
Запасной источник данных — OpenDota API (api.opendota.com).

Нужен, когда dotabuff недоступен (например, Cloudflare блокирует IP сервера,
как это бывает у GitHub Actions / дата-центров). Формат ответа совпадает с
`dotabuff.get_hero_overview`, чтобы бот ничего не заметил.
"""
import re
import time
from curl_cffi import requests as crequests

API = "https://api.opendota.com/api"
_ATTR_RU = {"str": "Strength", "agi": "Agility", "int": "Intelligence",
            "all": "Universal"}

_heroes = None          # список героев из heroStats
_items = None           # id предмета -> display name
_by_slug = None         # slug -> строка heroStats
_meta_cache = (0.0, None)


def _get(path: str):
    r = crequests.get(f"{API}{path}", impersonate="chrome", timeout=30)
    if r.status_code != 200:
        raise RuntimeError(f"OpenDota HTTP {r.status_code}: {path}")
    return r.json()


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def _load_heroes():
    global _heroes, _by_slug
    if _heroes is not None:
        return _heroes
    data = _get("/heroStats")
    _heroes = data
    _by_slug = {_norm(h["localized_name"]): h for h in data}
    return _heroes


def _load_items():
    global _items
    if _items is not None:
        return _items
    raw = _get("/constants/items")
    _items = {v.get("id"): (v.get("dname") or k)
              for k, v in raw.items() if isinstance(v, dict) and v.get("id")}
    return _items


def find_hero(name_or_slug: str):
    _load_heroes()
    key = _norm(name_or_slug)
    if key in _by_slug:
        return _by_slug[key]
    # «nature's prophet» -> naturesprophet и т.п. уже нормализовано
    for k, h in _by_slug.items():
        if key and (key in k or k in key):
            return h
    return None


def get_hero_overview(slug: str) -> dict:
    """Карточка героя из OpenDota в формате dotabuff.get_hero_overview."""
    hero = find_hero(slug.replace("-", " "))
    if not hero:
        raise RuntimeError(f"OpenDota: герой {slug} не найден")

    pick, win = hero.get("pub_pick") or 0, hero.get("pub_win") or 0
    winrate = f"{win / pick * 100:.2f}%" if pick else None

    attrs = [hero.get("attack_type", "")]
    attrs.append(_ATTR_RU.get(hero.get("primary_attr", ""), ""))
    attrs += hero.get("roles") or []
    attrs = [a for a in attrs if a]

    # популярные предметы (середина игры)
    items = []
    try:
        pop = _get(f"/heroes/{hero['id']}/itemPopularity")
        names = _load_items()
        mid = pop.get("mid_game_items") or {}
        total = sum(mid.values()) or 1
        top = sorted(mid.items(), key=lambda kv: -kv[1])[:10]
        for item_id, cnt in top:
            dname = names.get(int(item_id))
            if not dname:
                continue
            items.append({
                "name": dname,
                "matches": str(cnt),
                "winrate": f"{cnt / total * 100:.0f}%",
            })
    except Exception:  # noqa: BLE001
        pass

    # контрпики / слабые противники
    best, worst = [], []
    try:
        ms = _get(f"/heroes/{hero['id']}/matchups")
        rows = []
        for m in ms:
            games, wins = m.get("games_played") or 0, m.get("wins") or 0
            if games < 20:
                continue
            other = next((h for h in _heroes if h["id"] == m["hero_id"]), None)
            if not other:
                continue
            diff = (wins / games - 0.5) * 100
            rows.append({
                "hero": other["localized_name"],
                "adv": f"{diff:+.2f}%",
                "winrate": f"{wins / games * 100:.2f}%",
            })
        rows.sort(key=lambda r: float(r["adv"].rstrip("%")))
        worst, best = rows[:5], rows[-5:][::-1]
    except Exception:  # noqa: BLE001
        pass

    return {
        "en": hero["localized_name"],
        "slug": slug,
        "attrs": attrs,
        "winrate": winrate,
        "lanes": [],
        "items": items,
        "best_vs": best,
        "worst_vs": worst,
        "source": "opendota",
    }


# ----------------------------------------------------------------------------
# Мета патча
# ----------------------------------------------------------------------------
def get_meta(min_picks: int = 5000, top: int = 10) -> dict:
    """Топ героев по винрейту и по популярности (публичные матчи)."""
    global _meta_cache
    ts, cached = _meta_cache
    if cached is not None and time.time() - ts < 3600:
        return cached

    heroes = _load_heroes()
    rows = []
    for h in heroes:
        pick = h.get("pub_pick") or 0
        win = h.get("pub_win") or 0
        if pick < min_picks:
            continue
        rows.append({
            "hero": h["localized_name"],
            "pick": pick,
            "winrate": win / pick * 100,
        })
    if not rows:
        raise RuntimeError("OpenDota: нет данных по героям")

    total_pick = sum(r["pick"] for r in rows) or 1
    for r in rows:
        r["pickrate"] = r["pick"] / total_pick * 100

    data = {
        "by_winrate": sorted(rows, key=lambda r: -r["winrate"])[:top],
        "by_pick": sorted(rows, key=lambda r: -r["pick"])[:top],
        "heroes": len(rows),
    }
    _meta_cache = (time.time(), data)
    return data


# ----------------------------------------------------------------------------
# Разбор матча
# ----------------------------------------------------------------------------
def get_match(match_id: int) -> dict:
    """Краткая сводка матча: результат, длительность, игроки."""
    data = _get(f"/matches/{match_id}")
    players = data.get("players") or []
    if not players or not data.get("duration") or not any(p.get("hero_id") for p in players):
        raise RuntimeError("OpenDota: матч не найден или ещё не разобран")

    heroes = {h["id"]: h["localized_name"] for h in _load_heroes()}
    rows = []
    for p in players:
        rows.append({
            "hero": heroes.get(p.get("hero_id"), "?"),
            "radiant": bool(p.get("isRadiant")),
            "k": p.get("kills") or 0,
            "d": p.get("deaths") or 0,
            "a": p.get("assists") or 0,
            "gpm": p.get("gold_per_min") or 0,
            "xpm": p.get("xp_per_min") or 0,
            "net": p.get("net_worth") or 0,
            "lh": p.get("last_hits") or 0,
            "dn": p.get("denies") or 0,
            "level": p.get("level") or 0,
            "dmg": p.get("hero_damage") or 0,
        })
    rows.sort(key=lambda r: (not r["radiant"], -r["net"]))
    return {
        "id": match_id,
        "radiant_win": bool(data.get("radiant_win")),
        "duration": data.get("duration") or 0,
        "start": data.get("start_time") or 0,
        "players": rows,
    }
