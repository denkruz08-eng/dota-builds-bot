# -*- coding: utf-8 -*-
"""Компактный выбор героя: поиск, фильтры, недавние и сетка 4x4.

Модуль не знает про Telegram-хендлеры: он только строит индекс героев,
ищет, фильтрует и возвращает готовые inline-клавиатуры. Карточка героя
и про-билды остаются в bot.py и здесь не затрагиваются.
"""
import json
import re
from pathlib import Path

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

import dotabuff
import opendota

PAGE_SIZE = 16  # 4x4 на коллаже, целиком помещается на телефоне
MAX_RECENT = 6

ATTR_ICONS = {"str": "🔴", "agi": "🟢", "int": "🔵", "all": "🟣"}
ATTR_LABELS = {"str": "Сила", "agi": "Ловкость", "int": "Интеллект",
               "all": "Универсалы"}
ROLE_LABELS = {"carry": "1 Керри", "mid": "2 Мид", "off": "3 Хард",
               "support": "4/5 Саппорты"}

# Атрибут и базовые роли известны заранее — фильтры работают и без сети.
# OpenDota, если доступен, уточняет роли; при отказе остаётся эта таблица.
STATIC_HEROES = {
    "Abaddon": ("str", ["Durable", "Support"]),
    "Alchemist": ("str", ["Carry", "Durable"]),
    "Ancient Apparition": ("int", ["Support", "Nuker"]),
    "Anti-Mage": ("agi", ["Carry", "Escape"]),
    "Arc Warden": ("agi", ["Carry", "Nuker"]),
    "Axe": ("str", ["Durable", "Initiator"]),
    "Bane": ("int", ["Support", "Disabler"]),
    "Batrider": ("agi", ["Initiator", "Disabler"]),
    "Beastmaster": ("str", ["Initiator", "Durable"]),
    "Bloodseeker": ("agi", ["Carry", "Disabler"]),
    "Bounty Hunter": ("agi", ["Support", "Escape"]),
    "Brewmaster": ("str", ["Durable", "Initiator"]),
    "Bristleback": ("str", ["Durable", "Pusher"]),
    "Broodmother": ("agi", ["Carry", "Pusher"]),
    "Centaur Warrunner": ("str", ["Durable", "Initiator"]),
    "Chaos Knight": ("str", ["Carry", "Initiator"]),
    "Chen": ("int", ["Support", "Pusher"]),
    "Clinkz": ("agi", ["Carry", "Escape"]),
    "Clockwerk": ("str", ["Initiator", "Disabler"]),
    "Crystal Maiden": ("int", ["Support", "Disabler"]),
    "Dark Seer": ("int", ["Durable", "Initiator"]),
    "Dark Willow": ("int", ["Support", "Disabler"]),
    "Dawnbreaker": ("str", ["Durable", "Support"]),
    "Dazzle": ("int", ["Support", "Durable"]),
    "Death Prophet": ("int", ["Pusher", "Nuker"]),
    "Disruptor": ("int", ["Support", "Disabler"]),
    "Doom": ("str", ["Durable", "Nuker"]),
    "Dragon Knight": ("str", ["Durable", "Pusher"]),
    "Drow Ranger": ("agi", ["Carry", "Pusher"]),
    "Earth Spirit": ("int", ["Support", "Initiator"]),
    "Earthshaker": ("str", ["Initiator", "Disabler"]),
    "Elder Titan": ("str", ["Durable", "Initiator"]),
    "Ember Spirit": ("agi", ["Carry", "Initiator"]),
    "Enchantress": ("int", ["Support", "Pusher"]),
    "Enigma": ("int", ["Initiator", "Disabler"]),
    "Faceless Void": ("agi", ["Carry", "Initiator"]),
    "Grimstroke": ("int", ["Support", "Disabler"]),
    "Gyrocopter": ("agi", ["Carry", "Pusher"]),
    "Hoodwink": ("agi", ["Support", "Disabler"]),
    "Huskar": ("str", ["Carry", "Durable"]),
    "Invoker": ("int", ["Nuker", "Disabler"]),
    "Io": ("str", ["Support", "Durable"]),
    "Jakiro": ("int", ["Support", "Pusher"]),
    "Juggernaut": ("agi", ["Carry", "Durable"]),
    "Keeper of the Light": ("int", ["Support", "Pusher"]),
    "Kez": ("agi", ["Carry", "Disabler"]),
    "Kunkka": ("str", ["Carry", "Initiator"]),
    "Largo": ("agi", ["Support", "Disabler"]),
    "Legion Commander": ("str", ["Carry", "Initiator"]),
    "Leshrac": ("int", ["Nuker", "Pusher"]),
    "Lich": ("int", ["Support", "Nuker"]),
    "Lifestealer": ("str", ["Carry", "Durable"]),
    "Lina": ("int", ["Carry", "Nuker"]),
    "Lion": ("int", ["Support", "Disabler"]),
    "Lone Druid": ("agi", ["Carry", "Pusher"]),
    "Luna": ("agi", ["Carry", "Pusher"]),
    "Lycan": ("agi", ["Carry", "Pusher"]),
    "Magnus": ("str", ["Initiator", "Durable"]),
    "Marci": ("agi", ["Support", "Initiator"]),
    "Mars": ("str", ["Durable", "Initiator"]),
    "Medusa": ("agi", ["Carry", "Durable"]),
    "Meepo": ("agi", ["Carry", "Pusher"]),
    "Mirana": ("agi", ["Support", "Carry"]),
    "Monkey King": ("agi", ["Carry", "Initiator"]),
    "Morphling": ("agi", ["Carry", "Escape"]),
    "Muerta": ("int", ["Carry", "Nuker"]),
    "Naga Siren": ("agi", ["Carry", "Pusher"]),
    "Nature's Prophet": ("int", ["Carry", "Pusher"]),
    "Necrophos": ("int", ["Carry", "Durable"]),
    "Night Stalker": ("str", ["Carry", "Durable"]),
    "Nyx Assassin": ("int", ["Support", "Disabler"]),
    "Ogre Magi": ("int", ["Support", "Durable"]),
    "Omniknight": ("str", ["Support", "Durable"]),
    "Oracle": ("int", ["Support", "Durable"]),
    "Outworld Destroyer": ("int", ["Carry", "Nuker"]),
    "Pangolier": ("agi", ["Durable", "Initiator"]),
    "Phantom Assassin": ("agi", ["Carry", "Escape"]),
    "Phantom Lancer": ("agi", ["Carry", "Escape"]),
    "Phoenix": ("int", ["Initiator", "Support"]),
    "Primal Beast": ("str", ["Initiator", "Durable"]),
    "Puck": ("int", ["Initiator", "Escape"]),
    "Pudge": ("str", ["Durable", "Initiator"]),
    "Pugna": ("int", ["Nuker", "Pusher"]),
    "Queen of Pain": ("int", ["Carry", "Nuker"]),
    "Razor": ("agi", ["Carry", "Durable"]),
    "Riki": ("agi", ["Carry", "Escape"]),
    "Ringmaster": ("int", ["Support", "Disabler"]),
    "Rubick": ("int", ["Support", "Disabler"]),
    "Sand King": ("str", ["Initiator", "Disabler"]),
    "Shadow Demon": ("int", ["Support", "Disabler"]),
    "Shadow Fiend": ("agi", ["Carry", "Nuker"]),
    "Shadow Shaman": ("int", ["Support", "Pusher"]),
    "Silencer": ("int", ["Support", "Disabler"]),
    "Skywrath Mage": ("int", ["Support", "Nuker"]),
    "Slardar": ("str", ["Durable", "Initiator"]),
    "Slark": ("agi", ["Carry", "Escape"]),
    "Snapfire": ("int", ["Support", "Disabler"]),
    "Sniper": ("agi", ["Carry", "Pusher"]),
    "Spectre": ("agi", ["Carry", "Durable"]),
    "Spirit Breaker": ("str", ["Initiator", "Support"]),
    "Storm Spirit": ("agi", ["Carry", "Initiator"]),
    "Sven": ("str", ["Carry", "Durable"]),
    "Techies": ("int", ["Nuker", "Pusher"]),
    "Templar Assassin": ("agi", ["Carry", "Durable"]),
    "Terrorblade": ("agi", ["Carry", "Pusher"]),
    "Tidehunter": ("str", ["Durable", "Initiator"]),
    "Timbersaw": ("str", ["Durable", "Nuker"]),
    "Tinker": ("int", ["Nuker", "Pusher"]),
    "Tiny": ("str", ["Carry", "Pusher"]),
    "Treant Protector": ("str", ["Support", "Durable"]),
    "Troll Warlord": ("agi", ["Carry", "Durable"]),
    "Tusk": ("str", ["Initiator", "Support"]),
    "Underlord": ("str", ["Durable", "Pusher"]),
    "Undying": ("str", ["Support", "Durable"]),
    "Ursa": ("agi", ["Carry", "Durable"]),
    "Vengeful Spirit": ("agi", ["Support", "Disabler"]),
    "Venomancer": ("agi", ["Support", "Pusher"]),
    "Viper": ("agi", ["Carry", "Durable"]),
    "Visage": ("int", ["Support", "Pusher"]),
    "Void Spirit": ("agi", ["Carry", "Initiator"]),
    "Warlock": ("int", ["Support", "Durable"]),
    "Weaver": ("agi", ["Carry", "Escape"]),
    "Windranger": ("agi", ["Carry", "Initiator"]),
    "Winter Wyvern": ("int", ["Support", "Durable"]),
    "Witch Doctor": ("int", ["Support", "Disabler"]),
    "Wraith King": ("str", ["Carry", "Durable"]),
    "Zeus": ("int", ["Nuker", "Carry"]),
}

# Явные позиционные корзины — фильтр работает даже без OpenDota.
POSITION_HEROES = {
    "carry": {
        "Anti-Mage", "Arc Warden", "Bloodseeker", "Broodmother", "Chaos Knight",
        "Clinkz", "Drow Ranger", "Faceless Void", "Gyrocopter", "Huskar",
        "Juggernaut", "Legion Commander", "Lifestealer", "Lina", "Lone Druid",
        "Luna", "Lycan", "Medusa", "Meepo", "Monkey King", "Morphling", "Muerta",
        "Naga Siren", "Nature's Prophet", "Necrophos", "Night Stalker",
        "Phantom Assassin", "Phantom Lancer", "Razor", "Riki", "Shadow Fiend",
        "Slark", "Sniper", "Spectre", "Sven", "Templar Assassin", "Terrorblade",
        "Troll Warlord", "Ursa", "Viper", "Weaver", "Windranger", "Wraith King",
        "Kez",
    },
    "mid": {
        "Alchemist", "Arc Warden", "Batrider", "Bloodseeker", "Brewmaster",
        "Broodmother", "Chaos Knight", "Clockwerk", "Dark Seer", "Death Prophet",
        "Doom", "Dragon Knight", "Drow Ranger", "Earth Spirit", "Ember Spirit",
        "Grimstroke", "Gyrocopter", "Huskar", "Invoker", "Keeper of the Light",
        "Kunkka", "Largo", "Leshrac", "Lich", "Lina", "Magnus", "Medusa",
        "Meepo", "Morphling", "Muerta", "Nature's Prophet", "Necrophos",
        "Nyx Assassin", "Outworld Destroyer", "Puck", "Pugna", "Queen of Pain",
        "Razor", "Rubick", "Shadow Fiend", "Skywrath Mage", "Storm Spirit",
        "Templar Assassin", "Tinker", "Tiny", "Viper", "Void Spirit",
        "Windranger", "Zeus",
    },
    "off": {
        "Abaddon", "Axe", "Beastmaster", "Bristleback", "Centaur Warrunner",
        "Clockwerk", "Dark Seer", "Doom", "Dragon Knight", "Earthshaker",
        "Elder Titan", "Enigma", "Legion Commander", "Magnus", "Mars",
        "Night Stalker", "Pangolier", "Primal Beast", "Pudge", "Sand King",
        "Slardar", "Spirit Breaker", "Timbersaw", "Tiny", "Treant Protector",
        "Tusk", "Underlord", "Undying", "Visage",
    },
    "support": {
        "Ancient Apparition", "Bane", "Bounty Hunter", "Chen", "Crystal Maiden",
        "Dazzle", "Disruptor", "Earth Spirit", "Enchantress", "Grimstroke",
        "Hoodwink", "Io", "Jakiro", "Keeper of the Light", "Lich", "Lion",
        "Marci", "Nyx Assassin", "Ogre Magi", "Omniknight", "Oracle", "Phoenix",
        "Ringmaster", "Rubick", "Shadow Demon", "Shadow Shaman", "Silencer",
        "Skywrath Mage", "Snapfire", "Spirit Breaker", "Techies",
        "Treant Protector", "Tusk", "Undying", "Vengeful Spirit", "Venomancer",
        "Visage", "Warlock", "Winter Wyvern", "Witch Doctor", "Dawnbreaker",
    },
}

# Игровой сленг и сокращения. Один алиас может указывать на несколько героев —
# тогда бот покажет компактный список, а не угадает за пользователя.
SLANG = {
    "Anti-Mage": ["ам", "антимаг", "анти-маг", "am", "antimage"],
    "Crystal Maiden": ["цмка", "цм", "кристал", "кристалмайден", "cm"],
    "Windranger": ["вк", "вкер", "вин", "винд", "виндраннер", "wr", "wind"],
    "Shadow Fiend": ["сф", "шф", "невер", "невермор", "sf", "shadow fiend"],
    "Slardar": ["тракса", "тракс", "слардар", "slardar"],
    "Phantom Assassin": ["па", "фантом", "пан", "pa", "phantom"],
    "Juggernaut": ["джага", "джаг", "джагер", "jug", "jugg"],
    "Invoker": ["инвокер", "инв", "инвок", "inv", "invoke"],
    "Earth Spirit": ["ес", "ез", "earth spirit", "es"],
    "Faceless Void": ["фв", "войд", "фейс", "fv", "void", "faceless"],
    "Pudge": ["пудж", "pudge", "pd"],
    "Axe": ["акса", "акс", "аксe", "axe"],
    "Lina": ["лина", "lina"],
    "Leshrac": ["лешрак", "леш", "lesh"],
    "Sven": ["свен", "sv"],
    "Kunkka": ["кунка", "кунк", "kung"],
    "Meepo": ["мипо", "mi", "meepo"],
    "Timbersaw": ["тимбер", "тимберсо", "timber"],
    "Shadow Shaman": ["шаман", "шама", "ss", "shaman"],
    "Warlock": ["варлок", "варлок", "wl"],
    "Drow Ranger": ["дрова", "дров", "dr", "drow"],
    "Medusa": ["медуза", "мед", "medusa"],
    "Nyx Assassin": ["нья", "ньякс", "никс", "nyx"],
    "Razor": ["разор", "рэзор", "ra"],
    "Sand King": ["санка", "ск", "sk", "sand"],
    "Tinker": ["тинкер", "тинк", "tinker"],
    "Storm Spirit": ["сторм", "шторм", "storm"],
    "Ember Spirit": ["ембер", "ember"],
    "Templar Assassin": ["та", "темплар", "тасси", "ta"],
    "Queen of Pain": ["копа", "qop", "куин"],
    "Rubick": ["рубик", "руб", "rubick"],
    "Lion": ["лион", "lion"],
    "Lich": ["лич", "lich"],
    "Zeus": ["зеус", "зевс", "zeus"],
    "Sniper": ["снайпер", "снипер", "sniper"],

    "Treant Protector": ["треб", "дерево", "treant"],
    "Nature's Prophet": ["фурри", "пророк", "прист", "np", "natures"],
    "Lone Druid": ["лд", "лон druid", "медведь", "ld"],
    "Lycan": ["ликан", "волк", "lycan"],
    "Bristleback": ["брамбл", "бристи", "bristle"],
    "Centaur Warrunner": ["центавр", "кентавр", "цет", "centaur"],
    "Magnus": ["магнус", "маг", "magnus"],
    "Mars": ["марс", "mars"],
    "Puck": ["пак", "puck"],
    "Tusk": ["туск", "tuskar", "tusk"],
    "Undying": ["андаинг", "унди", "undying"],
    "Omniknight": ["омник", "омни", "omni"],
    "Abaddon": ["абаддон", "аба", "abaddon"],
    "Alchemist": ["алх", "алхимик", "alch"],

    "Bloodseeker": ["бс", "кровосос", "blood"],
    "Bounty Hunter": ["баунти", "бх", "bh"],
    "Clockwerk": ["клок", "часовой", "clock"],
    "Dark Seer": ["дарк сир", "дс", "ds"],
    "Dazzle": ["дазл", "даз"],
    "Doom": ["дум", "doom"],
    "Dragon Knight": ["дк", "дракон", "dk"],
    "Earthshaker": ["ешакер", "esh", "es"],
    "Enigma": ["энигма", "енигма", "enigma"],
    "Gyrocopter": ["гиро", "gyro"],
    "Huskar": ["хускар", "хуска"],
    "Io": ["айо", "ио", "io"],
    "Jakiro": ["джакиро", "джак", "jakiro"],
    "Keeper of the Light": ["котл", "кот", "kotl"],
    "Legion Commander": ["легион", "лк", "lc"],
    "Luna": ["луна", "luna"],
    "Mirana": ["мирана", "миа", "mirana"],
    "Monkey King": ["мк", "обезьяна", "mk"],
    "Morphling": ["морф", "морфлинг", "morph"],
    "Naga Siren": ["нага", "naga"],
    "Necrophos": ["некрос", "некр", "necro"],
    "Night Stalker": ["нс", "ночной", "ns"],
    "Ogre Magi": ["огре", "огр", "ogre"],
    "Oracle": ["оракул", "ора", "oracle"],
    "Outworld Destroyer": ["од", "аутворлд", "od"],
    "Pangolier": ["панго", "панг", "pango"],
    "Phantom Lancer": ["пл", "лансер", "pl"],
    "Phoenix": ["феникс", "phoenix"],
    "Primal Beast": ["прайм", "пб", "pb"],
    "Pugna": ["пугна", "pugna"],
    "Riki": ["рики", "rik"],
    "Ringmaster": ["рингмастер", "rm"],
    "Shadow Demon": ["шд", "шадоу демон", "sd"],
    "Silencer": ["силенсер", "сил", "sil"],
    "Skywrath Mage": ["скай", "свм", "sky"],
    "Slark": ["сларк", "slark"],
    "Snapfire": ["снап", "снапфайр", "snap"],
    "Spectre": ["спектр", "спек", "spec"],
    "Spirit Breaker": ["сб", "бык", "sb"],

    "Techies": ["техи", "тех", "techies"],
    "Terrorblade": ["тб", "террор", "tb"],
    "Tidehunter": ["тайд", "тид", "th"],
    "Tiny": ["тини", "тини", "tiny"],
    "Troll Warlord": ["тролль", "трол", "troll"],
    "Underlord": ["андерлорд", "ул", "underlord"],
    "Ursa": ["урса", "медведь", "ursa"],
    "Vengeful Spirit": ["венга", "венж", "vs"],
    "Venomancer": ["веном", "вампир", "veno"],
    "Viper": ["вайпер", "випер", "viper"],
    "Visage": ["висаж", "visage"],
    "Void Spirit": ["вс", "войд спирит", "vs"],
    "Weaver": ["вивер", "weaver"],
    "Winter Wyvern": ["вв", "вайверн", "ww"],
    "Witch Doctor": ["вд", "доктор", "wd"],
    "Wraith King": ["вк?", " wk", "wraith", "skeleton king"],
    "Kez": ["кез", "kez"],
    "Largo": ["ларго", "largo"],
    "Dawnbreaker": ["даунбрейкер", "дб", "db"],
    "Batrider": ["бат", "батрайдер", "bat"],
    "Beastmaster": ["бист", "бестмастер", "bm"],
    "Brewmaster": ["брю", "brew"],
    "Broodmother": ["бруд", "паучка", "brood"],
    "Chaos Knight": ["кн", "хаос", "ck"],
    "Chen": ["чен", "chen"],
    "Clinkz": ["клинкс", "clinkz"],
    "Dark Willow": ["дарк вилоу", "дв", "dw"],
    "Death Prophet": ["дп", "профет", "dp"],
    "Disruptor": ["дисраптор", "дис", "dis"],
    "Elder Titan": ["эт", "титан", "et"],
    "Enchantress": ["енчан", "енчантресс", "ench"],
    "Grimstroke": ["грим", "гримстроук", "grim"],
    "Hoodwink": ["худвинк", "hood"],
    "Earth Spirit ": ["земля"],
    "Ringmaster ": ["цирк"],
    "Skywrath Mage ": ["небо"],
    "Ancient Apparition": ["аа", "каппа", "аппа", "древний", "aa"],
    "Arc Warden": ["арк", "ав", "arc"],
    "Bane": ["бейн", "bane"],
    "Clockwerk ": ["клокверк"],
    "Dawnbreaker ": ["рассвет"],
    "Faceless Void ": ["фацелесс"],
    "Gyrocopter ": ["гирокоптер"],
    "Invoker ": ["инвокер"],
    "Keeper of the Light ": ["страж света"],
    "Monkey King ": ["май"],
    "Night Stalker ": ["ночной stalker"],
    "Outworld Destroyer ": ["аутворлд"],
    "Primal Beast ": ["первобыт"],
    "Shadow Demon ": ["тёмный"],
    "Storm Spirit ": ["гроза"],
    "Templar Assassin ": ["темплар ассасин"],
    "Winter Wyvern ": ["зимняя"],
}

SHORT = {
    "Ancient Apparition": "AA", "Anti-Mage": "AM", "Crystal Maiden": "CM",
    "Outworld Destroyer": "OD", "Phantom Assassin": "PA", "Queen of Pain": "QoP",
    "Shadow Fiend": "SF", "Shadow Shaman": "SS", "Windranger": "WR",
    "Earth Spirit": "ES", "Earthshaker": "ESh", "Faceless Void": "FV",
    "Juggernaut": "Jugg", "Legion Commander": "LC", "Lone Druid": "LD",
    "Nature's Prophet": "NP", "Night Stalker": "NS", "Phantom Lancer": "PL",
    "Shadow Demon": "SD", "Skywrath Mage": "Sky", "Spirit Breaker": "SB",
    "Templar Assassin": "TA", "Terrorblade": "TB", "Tidehunter": "Tide",
    "Treant Protector": "Treant", "Vengeful Spirit": "VS", "Void Spirit": "VS",
    "Winter Wyvern": "WW", "Witch Doctor": "WD", "Wraith King": "WK",
}

_INDEX = None


def _norm(value) -> str:
    s = str(value or "").lower().replace("ё", "е")
    return re.sub(r"[^a-z0-9а-я]", "", s)


def _load_ru() -> dict:
    try:
        return json.loads((Path(__file__).parent / "heroes_ru.json")
                          .read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}


def _load_opendota() -> dict:
    try:
        rows = opendota._load_heroes() or []
        return {_norm(h.get("localized_name")): h for h in rows}
    except Exception:  # noqa: BLE001
        return {}


def build_index(force: bool = False) -> list[dict]:
    """127 героев: en/slug/ru/атрибут/роли/все алиасы для поиска."""
    global _INDEX
    if _INDEX is not None and not force:
        return _INDEX

    base = dotabuff.get_heroes() or []
    if not base:
        try:
            base = json.loads((Path(__file__).parent / "heroes_static.json")
                              .read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            base = []

    ru_map = _load_ru()
    od_map = _load_opendota()
    out = []
    for h in base:
        en = h.get("en") or ""
        slug = h.get("slug") or _norm(en)
        od = od_map.get(_norm(en)) or od_map.get(_norm(slug)) or {}
        static_attr, static_roles = STATIC_HEROES.get(en, ("all", []))
        raw_attr = str(od.get("primary_attr") or static_attr).lower()
        attr_key = {"str": "str", "strength": "str", "agi": "agi",
                    "agility": "agi", "int": "int", "intelligence": "int",
                    "all": "all", "universal": "all"}.get(raw_attr, "all")
        roles = od.get("roles") or static_roles
        info = ru_map.get(en) or {}
        aliases = set(info.get("aliases") or [])
        if info.get("ru"):
            aliases.add(info["ru"])
        aliases.update(SLANG.get(en) or [])
        aliases.add(en)
        aliases.add(en.replace("-", ""))
        aliases.add(en.lower().replace(" ", ""))
        norms = sorted({_norm(a) for a in aliases if _norm(a)})
        out.append({
            "en": en,
            "slug": slug,
            "ru": info.get("ru") or "",
            "aliases": sorted(aliases),
            "attr_key": attr_key,
            "attr_icon": ATTR_ICONS.get(attr_key, "🟣"),
            "roles": roles,
            "_norms": norms,
            "_en_norm": _norm(en),
        })
    out.sort(key=lambda h: h["en"])
    _INDEX = out
    return out


def fuzzy_find(query: str, heroes: list[dict] | None = None,
               limit: int = 5) -> list[dict]:
    """Подстрочный + сленговый поиск. 1 точное совпадение — сразу герой."""
    q = _norm(query)
    if not q:
        return []
    heroes = heroes if heroes is not None else build_index()
    tokens = [t for t in re.split(r"[\s\-]+", q) if t]
    scored = []
    for h in heroes:
        best = 0
        norms = h.get("_norms") or [_norm(a) for a in h.get("aliases", [])]
        for alias in norms:
            if not alias:
                continue
            if alias == q:
                best = max(best, 100)
            elif alias.startswith(q):
                best = max(best, 80 - min(10, len(alias) - len(q)))
            elif q in alias:
                best = max(best, 55 - min(15, alias.index(q)))
        if q in (h.get("_en_norm") or _norm(h["en"])):
            best = max(best, 45)
        if len(tokens) > 1:
            cover = sum(any(t in a for a in norms) for t in tokens)
            if cover == len(tokens):
                best = max(best, 75)
        if best:
            scored.append((best, h["en"], h))
    scored.sort(key=lambda row: (-row[0], row[1]))
    return [h for _, _, h in scored[:limit]]


class RecentStore:
    """Последние герои пользователя в памяти процесса (без БД)."""

    def __init__(self, max_size: int = MAX_RECENT):
        self.max_size = max_size
        self._data: dict = {}

    def get(self, user_key) -> list[str]:
        return list(self._data.get(user_key, []))

    def add(self, user_key, slug: str) -> None:
        if not user_key or not slug:
            return
        items = [s for s in self._data.get(user_key, []) if s != slug]
        items.insert(0, slug)
        self._data[user_key] = items[:self.max_size]

    def clear(self, user_key=None) -> None:
        if user_key is None:
            self._data.clear()
        else:
            self._data.pop(user_key, None)


def encode_filter(kind: str, value: str = "") -> str:
    return f"{kind}-{value}" if value else kind


def decode_filter(key: str) -> tuple[str, str]:
    kind, _, value = (key or "all").partition("-")
    return kind, value


def filter_heroes(filter_key: str = "all", page: int = 0,
                  page_size: int = PAGE_SIZE, recent_store: RecentStore | None = None,
                  user_key=None) -> tuple[list[dict], int, int, int]:
    """Возвращает (страница, всего страниц, всего героев, номер страницы)."""
    kind, value = decode_filter(filter_key)
    heroes = build_index()
    if kind == "attr":
        heroes = [h for h in heroes if h["attr_key"] == value]
    elif kind == "role":
        pool = POSITION_HEROES.get(value, set())
        heroes = [h for h in heroes if h["en"] in pool]
    elif kind == "recent":
        slugs = recent_store.get(user_key) if recent_store else []
        by_slug = {h["slug"]: h for h in heroes}
        heroes = [by_slug[s] for s in slugs if s in by_slug]
    total = len(heroes)
    total_pages = max(1, (total + page_size - 1) // page_size)
    page = max(0, min(page, total_pages - 1))
    chunk = heroes[page * page_size:(page + 1) * page_size]
    return chunk, total_pages, total, page


def filter_title(filter_key: str) -> str:
    kind, value = decode_filter(filter_key)
    if kind == "attr":
        icon = ATTR_ICONS.get(value, "🟣")
        label = ATTR_LABELS.get(value, value)
        return f"{icon} {label}"
    if kind == "role":
        return ROLE_LABELS.get(value, value)
    if kind == "recent":
        return "⭐ Недавние"
    return "Все герои"


def short_label(h: dict) -> str:
    en = h.get("en") or ""
    if en in SHORT:
        return SHORT[en]
    ru = h.get("ru") or ""
    if 0 < len(ru) <= 10:
        return ru
    return (en.split()[0] if en else "?")[:10]


def filters_keyboard() -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text="🔍 Быстрый поиск", callback_data="search")],
        [
            InlineKeyboardButton(text="🔴 Сила",
                                 callback_data=f"f:{encode_filter('attr', 'str')}"),
            InlineKeyboardButton(text="🟢 Ловкость",
                                 callback_data=f"f:{encode_filter('attr', 'agi')}"),
            InlineKeyboardButton(text="🔵 Интеллект",
                                 callback_data=f"f:{encode_filter('attr', 'int')}"),
            InlineKeyboardButton(text="🟣 Универсалы",
                                 callback_data=f"f:{encode_filter('attr', 'all')}"),
        ],
        [
            InlineKeyboardButton(text="1 Керри",
                                 callback_data=f"f:{encode_filter('role', 'carry')}"),
            InlineKeyboardButton(text="2 Мид",
                                 callback_data=f"f:{encode_filter('role', 'mid')}"),
            InlineKeyboardButton(text="3 Хард",
                                 callback_data=f"f:{encode_filter('role', 'off')}"),
            InlineKeyboardButton(text="4/5 Сап",
                                 callback_data=f"f:{encode_filter('role', 'support')}"),
        ],
        [InlineKeyboardButton(text="📜 Все 127 героев",
                              callback_data=f"f:{encode_filter('all')}")],
        [InlineKeyboardButton(text="⭐ Недавние",
                              callback_data=f"f:{encode_filter('recent')}")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _nav_row(filter_key: str, page: int, total_pages: int) -> list:
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="⬅️ Назад",
                                        callback_data=f"p:{filter_key}:{page-1}"))
    nav.append(InlineKeyboardButton(text=f"{page+1}/{total_pages}",
                                    callback_data="noop"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton(text="Вперёд ➡️",
                                        callback_data=f"p:{filter_key}:{page+1}"))
    return nav


def hero_grid_keyboard(chunk: list[dict], filter_key: str, page: int,
                       total_pages: int) -> InlineKeyboardMarkup:
    rows = [_nav_row(filter_key, page, total_pages)]
    for i in range(0, len(chunk), 3):
        rows.append([
            InlineKeyboardButton(text=short_label(h),
                                 callback_data=f"hero:{h['slug']}")
            for h in chunk[i:i + 3]
        ])
    bottom = _nav_row(filter_key, page, total_pages)
    bottom.append(InlineKeyboardButton(text="🎛 Фильтры", callback_data="filters"))
    rows.append(bottom)
    return InlineKeyboardMarkup(inline_keyboard=rows)


def results_keyboard(matches: list[dict]) -> InlineKeyboardMarkup:
    """2–5 совпадений: по 2 в ряд, короткое имя + роль."""
    rows = []
    for i in range(0, len(matches), 2):
        row = []
        for h in matches[i:i + 2]:
            label = short_label(h)
            role = ""
            for hrole in (h.get("roles") or []):
                if str(hrole).lower() in ("carry", "support", "initiator",
                                          "nuker", "disabler", "durable",
                                          "pusher", "escape"):
                    role = {"carry": "Carry", "support": "Sup",
                            "initiator": "Init", "nuker": "Nuke",
                            "disabler": "Dis", "durable": "Tank",
                            "pusher": "Push", "escape": "Esc"}[str(hrole).lower()]
                    break
            text = f"{label} · {role}" if role else label
            row.append(InlineKeyboardButton(text=text,
                                            callback_data=f"hero:{h['slug']}"))
        rows.append(row)
    return InlineKeyboardMarkup(inline_keyboard=rows)
