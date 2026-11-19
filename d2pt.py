# -*- coding: utf-8 -*-
"""
dota2protracker.com: мета-билды про-игроков по позициям (pos 1..5).

Источник: страница /hero/<Hero Name>/builds — там SvelteKit кладёт
в HTML сериализованные данные: buildData (по позициям) со стартовыми
предметами, кор-предметами с таймингами, финальным инвентарём,
порядком прокачки способностей и талантами.
"""
import re
import os
import time
from pathlib import Path
from curl_cffi import requests as crequests

BASE = "https://dota2protracker.com"
IMPERSONATE = "chrome"

# d2pt работает напрямую (прокси можно включить через env D2PT_PROXY)
_PROXIES = None
_p = (os.environ.get("D2PT_PROXY") or "").strip()
if _p:
    _PROXIES = {"http": _p, "https": _p}

# запасной вариант: если прямой доступ дёрнулся — пробуем через локальный прокси
_FALLBACK_PROXIES = None
if (os.environ.get("BOT_PROXY") or "").strip():
    _fb = os.environ["BOT_PROXY"].strip()
else:
    try:
        _fb = (Path(__file__).with_name("proxy.txt").read_text(encoding="utf-8") or "").strip()
    except OSError:
        _fb = ""
if _fb and _fb != _p:
    _FALLBACK_PROXIES = {"http": _fb, "https": _fb}

POS_TITLES = {
    1: "Pos 1 — Carry",
    2: "Pos 2 — Mid",
    3: "Pos 3 — Offlane",
    4: "Pos 4 — Soft Support",
    5: "Pos 5 — Hard Support",
    "all": "All Positions",
}

_TTL = 60 * 60  # билды меняются редко
_cache = {}


def _get(url: str, tries: int = 2) -> str:
    last = None
    variants = [_PROXIES]
    if _FALLBACK_PROXIES and variants[0] != _FALLBACK_PROXIES:
        variants.append(_FALLBACK_PROXIES)
    for proxies in variants:
        for attempt in range(tries):
            try:
                r = crequests.get(url, impersonate=IMPERSONATE,
                                  headers={"Accept-Language": "en-US,en;q=0.9"},
                                  timeout=35, proxies=proxies)
                if r.status_code == 200:
                    return r.text
                last = RuntimeError(f"HTTP {r.status_code}: {url}")
            except Exception as e:  # noqa: BLE001
                last = e
            if attempt < tries - 1:
                time.sleep(2)
    raise last


def _balanced(s: str, start: int, open_ch: str, close_ch: str) -> str:
    """Возвращает подстроку от s[start] до парной закрывающей скобки."""
    depth, i, in_str, esc = 0, start, False, False
    n = len(s)
    while i < n:
        c = s[i]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
        elif c == '"':
            in_str = True
        elif c == open_ch:
            depth += 1
        elif c == close_ch:
            depth -= 1
            if depth == 0:
                return s[start:i + 1]
        i += 1
    return s[start:]


def _fnum(x, default=0.0):
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def _hero_url(en: str) -> str:
    return f"{BASE}/hero/{en.replace(' ', '+').replace('%27', chr(39))}/builds"


def get_builds(en_name: str) -> dict:
    key = en_name.lower()
    v = _cache.get(key)
    if v and time.time() - v[0] < _TTL:
        return v[1]

    t = _get(_hero_url(en_name))

    # словарь предметов: item_id -> (displayName, внутреннее имя для CDN)
    item_names = {}
    item_internal = {}
    for m in re.finditer(r'item_id:(\d+),name:"([^"]*)",displayName:"([^"]+)"', t):
        item_names[m.group(1)] = m.group(3)
        item_internal[m.group(1)] = m.group(2).replace("item_", "", 1)

    # buildData массив
    i = t.find("buildData:[")
    if i == -1:
        raise RuntimeError("d2pt: buildData не найден")
    arr = _balanced(t, i + len("buildData:"), "[", "]")

    # верхнеуровневые элементы массива
    records = []
    j = 1  # пропускаем '['
    while j < len(arr):
        while j < len(arr) and arr[j] in " \t\r\n,":
            j += 1
        if j >= len(arr) or arr[j] == "]":
            break
        rec = _balanced(arr, j, "{", "}")
        records.append(rec)
        j += len(rec)

    positions = {}
    for rec in records:
        pm = re.search(r'position:"pos (\d)"', rec)
        if not pm:
            continue
        pos = int(pm.group(1))
        nm = re.search(r"num_matches:(\d+)", rec)
        nw = re.search(r"num_wins:(\d+)", rec)
        matches = int(nm.group(1)) if nm else 0
        wins = int(nw.group(1)) if nw else 0

        # скилл-билд: abilities:[{ability_id...
        am = re.search(r"abilities:\[\{ability_id", rec)
        abilities = []
        if am:
            seg = _balanced(rec, am.start() + len("abilities:"), "[", "]")
            abilities = re.findall(r'displayName:"([^"]+)"', seg)

        # таланты
        talents = []
        for m in re.finditer(
                r"lvl:(\d+),left:\{([^{}]*?)\},slot:\d+,wins:\d+,count:\d+,right:\{([^{}]*?)\}",
                rec):
            lvl, lpart, rpart = m.group(1), m.group(2), m.group(3)
            lm = re.search(r'displayName:"([^"]+)"', lpart)
            rm = re.search(r'displayName:"([^"]+)"', rpart)
            lp = re.search(r"pr:([0-9.]+)", lpart)
            rp = re.search(r"pr:([0-9.]+)", rpart)
            if lm and rm:
                left_better = _fnum(lp.group(1) if lp else 0) >= _fnum(rp.group(1) if rp else 0)
                best = lm.group(1) if left_better else rm.group(1)
                talents.append({"level": int(lvl),
                                "name": best.replace("Talent: ", ""),
                                "pr": max(_fnum(lp.group(1) if lp else 0),
                                          _fnum(rp.group(1) if rp else 0))})

        # кор-предметы с таймингами (ранняя + мид/лейт прогрессия)
        core = []
        for arrname in ("anchor_items", "items_mid_late"):
            km = re.search(arrname + r":\[", rec)
            if not km:
                continue
            seg = _balanced(rec, km.start() + len(arrname) + 1, "[", "]")
            for em in re.finditer(
                    r"pr:([0-9.]+),key:\"[^\"]*\",wins:\d+,count:\d+,rel_pr:[0-9.]+,"
                    r"win_rate:[0-9.]+,rel_count:\d+,avg_minute:([0-9.]+),"
                    r"std_minute:[0-9.]+,raw_item_id:(\d+)", seg):
                pr, minute, iid = em.group(1), em.group(2), em.group(3)
                if _fnum(pr) < 0.18:  # отсекаем редкие предметы
                    continue
                name = item_names.get(iid, f"item {iid}")
                core.append({"item": name, "minute": round(_fnum(minute)),
                             "id": item_internal.get(iid, "")})
        # дедуп по имени (оставляем самый ранний тайминг)
        seen = {}
        for c in sorted(core, key=lambda x: x["minute"]):
            seen.setdefault(c["item"], c)
        core = sorted(seen.values(), key=lambda x: x["minute"])[:12]

        # финальный инвентарь (sixslot) по pick_rate
        sixslot = []
        sm = re.search(r"sixslot:\[", rec)
        if sm:
            seg = _balanced(rec, sm.start() + len("sixslot:"), "[", "]")
            entries = []
            for em in re.finditer(r"item_id:(\d+),pick_rate:([0-9.]+),num_matches:(\d+)", seg):
                entries.append(( _fnum(em.group(2)), em.group(1) ))
            entries.sort(reverse=True)
            taken = []
            seen_names = set()
            for _, iid in entries:
                name = item_names.get(iid)
                if name and name not in seen_names:
                    seen_names.add(name)
                    taken.append({"item": name, "id": item_internal.get(iid, "")})
                if len(taken) >= 6:
                    break
            sixslot = taken

        # стартовые предметы: самый популярный вариант
        starting = []
        stm = re.search(r"starting_inventory_options:\[\[", rec)
        if stm:
            seg = _balanced(rec, stm.start() + len("starting_inventory_options:"), "[", "]")
            first = _balanced(seg, seg.find("[") + 1, "[", "]")
            for iid in re.findall(r"item_id:(\d+)", first):
                name = item_names.get(iid)
                if name:
                    starting.append({"item": name, "id": item_internal.get(iid, "")})

        positions[pos] = {
            "pos": pos,
            "matches": matches,
            "winrate": f"{wins / matches * 100:.1f}%" if matches else None,
            "starting": starting,
            "core": core,
            "sixslot": sixslot,
            "abilities": abilities,
            "talents": talents,
        }

    if not positions:
        raise RuntimeError("d2pt: билды не найдены")

    data = {"en": en_name, "positions": positions,
            "url": _hero_url(en_name)}
    _cache[key] = (time.time(), data)
    return data


def format_position(data: dict, pos) -> str:
    positions = data["positions"]
    title = POS_TITLES.get(pos, str(pos))
    lines = [f"🛠 <b>D2PT Pro Build · {data['en']} · {title}</b>"]

    if pos == "all":
        lines.append("\n📊 <b>По позициям</b> (матчи / винрейт):")
        for p in sorted(positions):
            po = positions[p]
            if po["matches"]:
                lines.append(f"  • {POS_TITLES[p]}: {po['matches']} / {po['winrate']}")
        lines.append(f"\nВыбери позицию кнопками выше для полного билда.")
        lines.append(f"🔗 {data['url']}")
        return "\n".join(lines)

    po = positions.get(pos)
    if not po:
        lines.append("Нет данных про-матчей по этой позиции.")
        return "\n".join(lines)

    if po["matches"]:
        lines.append(f"📈 Матчей: {po['matches']} · Винрейт: <b>{po['winrate']}</b>")
    if po["starting"]:
        lines.append("🎒 Старт: " + ", ".join(s["item"] for s in po["starting"]))
    if po["core"]:
        lines.append("🧭 Кор (тайминги): " +
                     " → ".join(f"{c['item']} ({c['minute']}м)" for c in po["core"]))
    if po["sixslot"]:
        lines.append("🏁 Финал: " + ", ".join(s["item"] for s in po["sixslot"]))
    if po["abilities"]:
        ab = po["abilities"]
        lines.append("⚡ Скилл-билд: " + " → ".join(ab[:10]) +
                     (" → ..." if len(ab) > 10 else ""))
    if po["talents"]:
        tl = " · ".join(f"{t['level']}: {t['name']}" for t in po["talents"])
        lines.append("🌟 Таланты: " + tl)
    lines.append(f"\n🔗 {data['url']}")
    return "\n".join(lines)
