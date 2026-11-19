# -*- coding: utf-8 -*-
"""
Скрапер dotabuff.com: список героев, обзор героя, гайды по ролям.
Использует curl_cffi (имитация TLS-отпечатка Chrome) для обхода Cloudflare,
результаты кешируются в памяти, чтобы не слать лишние запросы.
"""
import re
import time
import json
import os
from pathlib import Path
from curl_cffi import requests as crequests
from bs4 import BeautifulSoup

BASE = "https://www.dotabuff.com"
# Cloudflare у dotabuff режет chrome-отпечаток (403), safari проходит
IMPERSONATE = "safari"
_IMPERSONATIONS = ("safari", "chrome", "safari", "chrome")
HEADERS = {"Accept-Language": "en-US,en;q=0.9"}

# dotabuff работает напрямую; запасной вариант — локальный прокси из proxy.txt
_PROXIES = None
_p = (os.environ.get("DOTABUFF_PROXY") or "").strip()
if _p:
    _PROXIES = {"http": _p, "https": _p}

_FALLBACK_PROXIES = None
_fb = (os.environ.get("BOT_PROXY") or "").strip()
if not _fb:
    try:
        _fb = (Path(__file__).with_name("proxy.txt").read_text(encoding="utf-8") or "").strip()
    except OSError:
        _fb = ""
if _fb and _fb != _p:
    _FALLBACK_PROXIES = {"http": _fb, "https": _fb}

_HEROES_TTL = 24 * 3600   # список героев меняется редко
_PAGE_TTL = 30 * 60       # страницы героев/гайдов — 30 минут


class _TTLCache:
    def __init__(self):
        self._d = {}

    def get(self, key, ttl):
        v = self._d.get(key)
        if v and time.time() - v[0] < ttl:
            return v[1]
        return None

    def set(self, key, value):
        self._d[key] = (time.time(), value)


_cache = _TTLCache()


def _get(url: str, tries: int = 4) -> str:
    """GET с ретраями и сменой TLS-отпечатка (Cloudflare то 403, то 200)."""
    last_exc = None
    proxies_variants = [_PROXIES]
    if _FALLBACK_PROXIES and _PROXIES != _FALLBACK_PROXIES:
        proxies_variants.append(_FALLBACK_PROXIES)
    for attempt in range(tries):
        imp = _IMPERSONATIONS[attempt % len(_IMPERSONATIONS)]
        for proxies in proxies_variants:
            try:
                r = crequests.get(url, impersonate=imp, headers=HEADERS,
                                  timeout=30, proxies=proxies)
                if r.status_code == 200:
                    return r.text
                last_exc = RuntimeError(f"HTTP {r.status_code}: {url}")
            except Exception as e:  # noqa: BLE001
                last_exc = e
        if attempt < tries - 1:
            time.sleep(1.5)
    raise last_exc


# ----------------------------------------------------------------------------
# Список героев
# ----------------------------------------------------------------------------

def _static_heroes() -> list[dict]:
    """Список героев из файла heroes_static.json (работает без интернета)."""
    try:
        data = json.loads(Path(__file__).with_name("heroes_static.json")
                          .read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return []
    return data if isinstance(data, list) else []


def get_heroes() -> list[dict]:
    """[{en, slug}] — отсортировано по имени.

    Кеш: память (24ч) -> файл heroes_cache.json (24ч) -> сеть.
    Дисковый кеш нужен, чтобы поиск героев работал мгновенно и
    даже когда dotabuff временно недоступен.
    """
    cached = _cache.get("heroes", _HEROES_TTL)
    if cached is not None:
        return cached

    disk = Path(__file__).with_name("heroes_cache.json")
    try:
        data = json.loads(disk.read_text(encoding="utf-8"))
        if time.time() - data.get("ts", 0) < _HEROES_TTL and data.get("heroes"):
            _cache.set("heroes", data["heroes"])
            return data["heroes"]
    except Exception:  # noqa: BLE001
        pass

    try:
        soup = BeautifulSoup(_get(f"{BASE}/heroes"), "html.parser")
    except Exception as e:  # noqa: BLE001
        # dotabuff блокирует IP серверов (Cloudflare 403) — берём список из файла
        heroes = _static_heroes()
        if heroes:
            _cache.set("heroes", heroes)
            return heroes
        raise

    heroes, seen = [], set()
    # только строки таблицы со списком героев (не навигация)
    for a in soup.select("table a[href^='/heroes/']"):
        img = a.find("img")
        if not img:
            continue
        name = (img.get("alt") or "").strip()
        href = a.get("href", "")
        slug = href.split("/heroes/")[-1].strip("/")
        if not name or not slug or slug in seen or "/" in slug or "#" in slug:
            continue
        seen.add(slug)
        heroes.append({"en": name, "slug": slug})

    heroes.sort(key=lambda h: h["en"].lower())
    if not heroes:
        heroes = _static_heroes()
    if not heroes:
        raise RuntimeError("Не удалось получить список героев с dotabuff")
    _cache.set("heroes", heroes)
    try:
        disk.write_text(json.dumps({"ts": time.time(), "heroes": heroes},
                                   ensure_ascii=False), encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass
    return heroes


# ----------------------------------------------------------------------------
# Обзор героя (главная страница)
# ----------------------------------------------------------------------------

_LANE_EN = {
    "Mid Lane": "Mid Lane",
    "Safe Lane": "Safe Lane",
    "Off Lane": "Off Lane",
    "Support": "Support",
    "Jungling": "Jungling",
    "Roaming": "Roaming",
}


def get_hero_overview(slug: str) -> dict:
    key = f"hero:{slug}"
    cached = _cache.get(key, _PAGE_TTL)
    if cached is not None:
        return cached

    try:
        data = _overview_from_dotabuff(slug)
    except Exception as e:  # noqa: BLE001
        # dotabuff блокирует IP серверов (403) — берём данные из OpenDota API
        import opendota
        data = opendota.get_hero_overview(slug)
        data["dotabuff_error"] = repr(e)[:120]
    _cache.set(key, data)
    return data


def _overview_from_dotabuff(slug: str) -> dict:
    soup = BeautifulSoup(_get(f"{BASE}/heroes/{slug}"), "html.parser")
    text = soup.get_text(" ", strip=True)

    # точное EN-имя берём из списка героев (по slug), иначе — из h1
    en_name = None
    try:
        for h in get_heroes():
            if h["slug"] == slug:
                en_name = h["en"]
                break
    except Exception:  # noqa: BLE001
        pass

    attrs = []
    h1 = soup.select_one("h1")
    if h1:
        raw = h1.get_text(" ", strip=True)
        if en_name and raw.startswith(en_name):
            rest = raw[len(en_name):]
        else:
            parts = raw.split()
            if not en_name:
                # возможно имя из нескольких слов — attrs начинаются после "Melee"/"Ranged"
                mm = re.search(r"\b(Melee|Ranged)\b", raw)
                if mm:
                    en_name = raw[:mm.start()].strip()
                    rest = raw[mm.start():]
                else:
                    en_name = slug.replace("-", " ").title()
                    rest = " ".join(parts[1:])
            else:
                rest = " ".join(parts[1:])
        attrs = [p.strip() for p in rest.split(",") if p.strip()]
    if not en_name:
        en_name = slug.replace("-", " ").title()

    m = re.search(r"\d+(?:th|st|nd|rd)\s*Popularity\s*([\d.]+)%\s*Win Rate", text)
    winrate = m.group(1) + "%" if m else None

    lanes = []
    for table in soup.find_all("table"):
        rows = table.select("tr")
        if not rows:
            continue
        head = rows[0].get_text(" ", strip=True)
        if head.startswith("Lane Presence"):
            for tr in rows[1:]:
                tds = [td.get_text(" ", strip=True) for td in tr.find_all("td")]
                if len(tds) >= 3 and tds[0] in _LANE_EN:
                    lanes.append({
                        "lane": _LANE_EN[tds[0]],
                        "presence": tds[1],
                        "winrate": tds[2],
                    })
            break

    items = []
    for table in soup.find_all("table"):
        rows = table.select("tr")
        if not rows:
            continue
        head = rows[0].get_text(" ", strip=True)
        if head.startswith("Item Matches"):
            for tr in rows[1:]:
                tds = [td.get_text(" ", strip=True) for td in tr.find_all("td")]
                img = tr.find("img")
                if img and len(tds) >= 5:
                    items.append({
                        "name": img.get("alt", ""),
                        "matches": tds[2],
                        "winrate": tds[-1],
                    })
            break

    def _versus(keyword):
        out = []
        for table in soup.find_all("table"):
            rows = table.select("tr")
            if not rows:
                continue
            head = rows[0].get_text(" ", strip=True)
            if head.startswith(keyword):
                for tr in rows[1:]:
                    tds = [td.get_text(" ", strip=True) for td in tr.find_all("td")]
                    if len(tds) >= 4:
                        out.append({"hero": tds[1], "adv": tds[2], "winrate": tds[3]})
                break
        return out[:5]

    if not lanes and not items:
        raise RuntimeError(f"Не удалось разобрать страницу героя {slug} (dotabuff?)")

    data = {
        "en": en_name,
        "slug": slug,
        "attrs": attrs,
        "winrate": winrate,
        "lanes": lanes,
        "items": items[:10],
        "best_vs": _versus("Hero Advantage"),
        "worst_vs": _versus("Hero Disadvantage"),
        "source": "dotabuff",
    }
    return data


# ----------------------------------------------------------------------------
# Гайды (последние топ-выступления недели)
# ----------------------------------------------------------------------------

_LANE_RE = re.compile(
    r"(?P<date>\d{4}-\d{2}-\d{2})\s+"
    r"(?P<lane>(?:Safe|Off|Mid) Lane(?: Core| Support)?|Jungling|Roaming|Unknown Lane)\s+"
    r"(?P<dur>\d+:\d+)"
)
_PLAYER_RE = re.compile(r"^\d+\s+(?P<player>.+?)\s+(?P<res>won|lost)")
_SIDE_RE = re.compile(r"(?P<side>Radiant|Dire)\s*~\s*(?P<mmr>[\d,]+)\s*MMR")

# тег роли -> какие метки лейна на dotabuff ему соответствуют
ROLE_FILTERS = {
    "mid": lambda lane: "Mid" in lane,
    "carry": lambda lane: "Safe Lane Core" in lane,
    "off": lambda lane: "Off Lane Core" in lane,
    "sup": lambda lane: "Support" in lane,
    "jungle": lambda lane: "Jungling" in lane or "Roaming" in lane,
    "all": lambda lane: True,
}

ROLE_TITLES = {
    "mid": "Mid",
    "carry": "Carry (Safe Lane)",
    "off": "Offlane",
    "sup": "Support",
    "jungle": "Jungle / Roam",
    "all": "All Roles",
}


def get_guides(slug: str) -> list[dict]:
    key = f"guides:{slug}"
    cached = _cache.get(key, _PAGE_TTL)
    if cached is not None:
        return cached

    soup = BeautifulSoup(_get(f"{BASE}/heroes/{slug}/guides"), "html.parser")
    guides = []
    for article in soup.select("article"):
        txt = article.get_text(" ", strip=True)
        lm = _LANE_RE.search(txt)
        if not lm:
            continue  # это не блок гайда (интро и т.п.)

        pm = _PLAYER_RE.match(txt)
        sm = _SIDE_RE.search(txt)

        # стартовые предметы
        starting = []
        label = article.find(string=lambda t: t and "Starting Items" in t)
        if label:
            blk = label.find_parent("div", class_="kv")
            if blk:
                starting = [i.get("alt", "").strip()
                            for i in blk.find_all("img") if i.get("alt", "").strip()]

        # сборка предметов: иконки с таймингами (match-item-with-time-bigicon)
        build = []
        for el in article.select(".match-item-with-time-bigicon"):
            img = el.find("img")
            t = el.select_one(".time")
            if img and img.get("alt", "").strip():
                build.append({
                    "time": t.get_text(strip=True) if t else "?",
                    "item": img.get("alt", "").strip(),
                })
        if not build:  # запасной вариант: элементы с пометкой важности
            for el in article.select('[class*="x-item-importance"]'):
                img = el.find("img")
                t = el.select_one(".time")
                if img and img.get("alt", "").strip():
                    build.append({
                        "time": t.get_text(strip=True) if t else "?",
                        "item": img.get("alt", "").strip(),
                    })

        # прокачка способностей (div.kv-small-margin с уровнями)
        abilities = []
        for el in article.select("div.kv-small-margin"):
            img = el.find("img")
            if not img:
                continue
            alt = img.get("alt", "").strip()
            if not alt or alt.lower() in ("abaddon",):
                continue
            lvl = None
            # уровень — это текст в оверлее (маленькая цифра на иконке)
            for child in el.select("div"):
                t = child.get_text(strip=True)
                if t.isdigit() and child.find("img") is None:
                    lvl = int(t)
                    break
            if lvl:
                abilities.append({"level": lvl, "ability": alt})
        abilities.sort(key=lambda a: a["level"])

        link = article.find("a", href=re.compile(r"/matches/\d+"))
        match_id = link.get("href", "").rsplit("/", 1)[-1] if link else None

        guides.append({
            "lane": lm.group("lane"),
            "date": lm.group("date"),
            "duration": lm.group("dur"),
            "player": pm.group("player") if pm else "?",
            "won": pm.group("res") == "won" if pm else None,
            "side": sm.group("side") if sm else "?",
            "mmr": sm.group("mmr") if sm else "?",
            "starting": starting,
            "build": build,
            "abilities": abilities,
            "match_id": match_id,
        })

    if guides:
        _cache.set(key, guides)
    return guides
