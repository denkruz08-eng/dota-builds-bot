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
_item_internals = None  # id предмета -> internal name (для иконок)
_by_slug = None         # slug -> строка heroStats


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
    global _items, _item_internals
    if _items is not None:
        return _items
    raw = _get("/constants/items")
    _items = {}
    _item_internals = {}
    for k, v in raw.items():
        if not isinstance(v, dict) or not v.get("id"):
            continue
        _items[v["id"]] = v.get("dname") or k
        _item_internals[v["id"]] = k
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
# Мета патча по MMR-брекетам
# ----------------------------------------------------------------------------
# OpenDota отдаёт статистику по рангам в полях 1_pick..8_pick (1 — Herald).
BRACKETS = {
    0: {"name": "Все ранги", "mmr": ""},
    1: {"name": "Herald", "mmr": "0–770"},
    2: {"name": "Guardian", "mmr": "770–1540"},
    3: {"name": "Crusader", "mmr": "1540–2310"},
    4: {"name": "Archon", "mmr": "2310–3080"},
    5: {"name": "Legend", "mmr": "3080–3850"},
    6: {"name": "Ancient", "mmr": "3850–4620"},
    7: {"name": "Divine", "mmr": "4620–5420"},
}

_meta_cache: dict = {}


def get_meta(bracket: int = 0, top: int = 10) -> dict:
    """Топ героев по винрейту и популярности — для выбранного MMR-брекета."""
    bracket = int(bracket)
    if bracket not in BRACKETS:
        bracket = 0
    hit = _meta_cache.get(bracket)
    if hit and time.time() - hit[0] < 3600:
        return hit[1]

    heroes = _load_heroes()
    pick_key = "pub_pick" if bracket == 0 else f"{bracket}_pick"
    win_key = "pub_win" if bracket == 0 else f"{bracket}_win"
    # порог отсечения мелких выборок: у топ-рангов игр заметно меньше
    min_picks = 5000 if bracket == 0 else 300

    rows = []
    total_pick = 0
    for h in heroes:
        pick = h.get(pick_key) or 0
        win = h.get(win_key) or 0
        total_pick += pick
        if pick < min_picks:
            continue
        rows.append({
            "hero": h["localized_name"],
            "pick": pick,
            "winrate": win / pick * 100,
        })
    if not rows or not total_pick:
        raise RuntimeError(f"OpenDota: нет данных для брекета {BRACKETS[bracket]['name']}")

    for r in rows:
        r["pickrate"] = r["pick"] / total_pick * 100

    data = {
        "bracket": bracket,
        "bracket_name": BRACKETS[bracket]["name"],
        "mmr": BRACKETS[bracket]["mmr"],
        "matches": total_pick,
        "by_winrate": sorted(rows, key=lambda r: -r["winrate"])[:top],
        "by_pick": sorted(rows, key=lambda r: -r["pick"])[:top],
        "heroes": len(rows),
    }
    _meta_cache[bracket] = (time.time(), data)
    return data


# ----------------------------------------------------------------------------
# Контр-пик по драфту
# ----------------------------------------------------------------------------
_matchup_cache: dict = {}


def _load_matchups(hero_id: int):
    """Матчапы героя (винрейт против каждого другого героя). Кеш 6 часов."""
    hit = _matchup_cache.get(hero_id)
    if hit and time.time() - hit[0] < 6 * 3600:
        return hit[1]
    data = _get(f"/heroes/{hero_id}/matchups")
    _matchup_cache[hero_id] = (time.time(), data)
    return data


def get_counters(enemy_slugs: list[str], top: int = 5) -> dict:
    """Топ героев против выбранного драфта (по матчап-статистике OpenDota)."""
    heroes = _load_heroes()
    by_id = {h["id"]: h for h in heroes}

    enemies = []
    for slug in enemy_slugs:
        h = find_hero(str(slug).replace("-", " "))
        if h and h not in enemies:
            enemies.append(h)
    if not enemies:
        raise RuntimeError("OpenDota: герои драфта не найдены")

    enemy_ids = {h["id"] for h in enemies}
    scores: dict[int, dict] = {}

    for enemy in enemies:
        for m in _load_matchups(enemy["id"]) or []:
            games = m.get("games_played") or 0
            wins = m.get("wins") or 0
            other = m.get("hero_id")
            if games < 20 or not other or other == enemy["id"]:
                continue
            if other in enemy_ids:
                continue
            adv = (wins / games - 0.5) * 100
            rec = scores.setdefault(other, {"sum": 0.0, "details": {}})
            rec["sum"] += adv
            rec["details"][enemy["localized_name"]] = adv

    rows = []
    for hid, rec in scores.items():
        h = by_id.get(hid)
        if not h:
            continue
        # среднее по всему драфту: герой, который плох против одного врага,
        # не должен выигрывать за счёт одного удачного матчапа
        rows.append({
            "hero": h["localized_name"],
            "advantage": rec["sum"] / len(enemies),
            "details": rec["details"],
        })
    rows.sort(key=lambda r: -r["advantage"])
    rows = [r for r in rows if r["advantage"] > 0][:top]

    return {
        "enemies": [h["localized_name"] for h in enemies],
        "rows": rows,
    }


# ----------------------------------------------------------------------------
# Разбор матча
# ----------------------------------------------------------------------------
def _objectives_summary(objectives) -> dict:
    """Башни/бараки/Рошан по событиям матча (только для распарсенных игр)."""
    res = {"towers": [0, 0], "rax": [0, 0], "roshan": [0, 0],
           "first_tower": None, "first_blood": 0, "throne": None}
    for o in objectives or []:
        kind = o.get("type")
        if kind == "CHAT_MESSAGE_FIRSTBLOOD":
            res["first_blood"] = o.get("time") or 0
        elif kind == "building_kill":
            key = o.get("key") or ""
            # goodguys — строения Radiant, badguys — Dire
            victim_radiant = key.startswith("npc_dota_goodguys_")
            destroyer = 1 if victim_radiant else 0
            if "fort" in key:
                res["throne"] = o.get("time") or 0
            elif "rax" in key:
                res["rax"][destroyer] += 1
            elif "tower" in key:
                res["towers"][destroyer] += 1
                if res["first_tower"] is None:
                    res["first_tower"] = (o.get("time") or 0,
                                          destroyer == 0)
        elif kind == "CHAT_MESSAGE_ROSHAN_KILL":
            team = o.get("team")
            if team in (2, 3):
                res["roshan"][0 if team == 2 else 1] += 1
    return res


def _fights_summary(teamfights, radiant_flags) -> dict | None:
    """Самый крупный файт: время, урон сторон, число смертей."""
    best = None
    for tf in teamfights or []:
        parts = tf.get("players") or []
        rad = sum((p.get("damage") or 0) for i, p in enumerate(parts)
                  if i < len(radiant_flags) and radiant_flags[i])
        dire = sum((p.get("damage") or 0) for i, p in enumerate(parts)
                   if i < len(radiant_flags) and not radiant_flags[i])
        if not (rad or dire):
            continue
        item = {"start": tf.get("start") or 0,
                "radiant_dmg": rad, "dire_dmg": dire,
                "deaths": sum(p.get("deaths") or 0 for p in parts)}
        if best is None or (rad + dire) > (best["radiant_dmg"] + best["dire_dmg"]):
            best = item
    return best


def _gold_summary(adv, duration: int) -> dict | None:
    """Пик преимущества и самый большой рывок по графику золота."""
    if not adv:
        return None
    step = duration / max(1, len(adv))
    peak_i = max(range(len(adv)), key=lambda i: abs(adv[i]))
    diffs = [(adv[i + 1] - adv[i], i + 1) for i in range(len(adv) - 1)]
    swing, swing_i = max(diffs, key=lambda x: abs(x[0])) if diffs else (0, 0)
    return {"peak": adv[peak_i], "peak_min": int(peak_i * step // 60),
            "swing": swing, "swing_min": int(swing_i * step // 60)}


def get_match(match_id: int) -> dict:
    """Разбор матча: счёт, экономика, перцентили игроков, файты, башни."""
    data = _get(f"/matches/{match_id}")
    players = data.get("players") or []
    if not players or not data.get("duration") or not any(p.get("hero_id") for p in players):
        raise RuntimeError("OpenDota: матч не найден или ещё не разобран")

    names = _load_items()
    internals = _item_internals or {}
    heroes = {h["id"]: h["localized_name"] for h in _load_heroes()}

    rows = []
    for p in players:
        items = []
        for i in range(6):
            iid = p.get(f"item_{i}") or 0
            if iid and internals.get(iid):
                items.append({"internal": internals[iid],
                              "name": names.get(iid, internals[iid])})
        neutral = p.get("item_neutral") or 0
        if neutral and internals.get(neutral):
            items.append({"internal": internals[neutral],
                          "name": names.get(neutral, internals[neutral])})
        rows.append({
            "slot": p.get("player_slot"),
            "hero": heroes.get(p.get("hero_id"), "?"),
            "hero_id": p.get("hero_id"),
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
            "tower": p.get("tower_damage") or 0,
            "heal": p.get("hero_healing") or 0,
            "obs": p.get("obs_placed"),
            "sen": p.get("sen_placed"),
            "stuns": p.get("stuns"),
            "items": items,
            "benchmarks": p.get("benchmarks") or {},
        })

    duration = data.get("duration") or 0
    score = (data.get("radiant_score") or 0, data.get("dire_score") or 0)
    radiant_flags = [r["radiant"] for r in rows]

    totals = {}
    for side in (True, False):
        team = [r for r in rows if r["radiant"] == side]
        totals[side] = {
            "kills": sum(r["k"] for r in team),
            "dmg": sum(r["dmg"] for r in team),
            "net": sum(r["net"] for r in team),
            "tower": sum(r["tower"] for r in team),
        }

    # MVP: урон (40%), участие в убийствах (30%), нетворс (30%) внутри команды
    for side in (True, False):
        team = [r for r in rows if r["radiant"] == side]
        kills = totals[side]["kills"] or 1
        gold = totals[side]["net"] or 1
        dmg = totals[side]["dmg"] or 1
        for r in team:
            r["impact"] = (0.4 * r["dmg"] / dmg
                           + 0.3 * (r["k"] + r["a"]) / kills
                           + 0.3 * r["net"] / gold)
    mvp = None
    winner_rows = [r for r in rows if r["radiant"] == bool(data.get("radiant_win"))]
    if winner_rows:
        best = max(winner_rows, key=lambda r: r["impact"])
        mvp = {"hero": best["hero"], "impact": best["impact"]}

    # место по нетворсу в своей команде (1 — самый богатый)
    for side in (True, False):
        team = sorted([r for r in rows if r["radiant"] == side],
                      key=lambda r: -r["net"])
        for i, r in enumerate(team, 1):
            r["net_rank"] = i

    objectives = _objectives_summary(data.get("objectives"))
    has_ob = bool(data.get("objectives"))
    parsed = has_ob or bool(data.get("radiant_gold_adv"))

    return {
        "id": match_id,
        "radiant_win": bool(data.get("radiant_win")),
        "duration": duration,
        "start": data.get("start_time") or 0,
        "first_blood": (data.get("first_blood_time")
                        or objectives.get("first_blood") or 0),
        "score": score,
        "parsed": parsed,
        "objectives": objectives if has_ob else None,
        "fight": _fights_summary(data.get("teamfights"), radiant_flags),
        "gold": _gold_summary(data.get("radiant_gold_adv"), duration),
        "totals": totals,
        "mvp": mvp,
        "players": sorted(rows, key=lambda r: (not r["radiant"], -r["net"])),
    }

