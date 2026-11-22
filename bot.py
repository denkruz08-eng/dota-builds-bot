# -*- coding: utf-8 -*-
"""
Telegram-бот со сборками по Dota 2 на основе dotabuff.com.

Функции:
  • поиск героя (по-русски и по-английски) или выбор из списка
  • карточка героя: винрейт, популярность, статистика по лейнам, частые предметы
  • гайды по выбранной роли: стартовые предметы + билд с таймингами

Запуск:
  1) pip install -r requirements.txt
  2) вписать токен в переменную окружения BOT_TOKEN или в файл token.txt
  3) python bot.py
"""
import asyncio
import hashlib
import html
import json
import logging
import os
import re
import socket
import ssl
import sys
import time
from pathlib import Path

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (CallbackQuery, FSInputFile, InlineKeyboardButton,
                           InlineKeyboardMarkup, InputMediaPhoto, KeyboardButton,
                           Message, ReplyKeyboardMarkup)

import dotabuff
import d2pt
import items_img
import opendota
import patch

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("dota-bot")


def esc(value) -> str:
    """Экранирование для Telegram HTML: без &#x27; — Bot API их не понимает."""
    return html.escape(str(value), quote=False)


# ----------------------------------------------------------------------------
# Токен: окружение BOT_TOKEN или файл token.txt рядом с ботом
# Прокси: BOT_PROXY / файл proxy.txt (например socks5://127.0.0.1:10808)
# Если у тебя VPN — можно просто включить его на весь ПК, тогда прокси не нужен.
# ----------------------------------------------------------------------------
def _load_token() -> str:
    tok = os.environ.get("BOT_TOKEN", "").strip()
    if not tok:
        f = Path(__file__).with_name("token.txt")
        if f.exists():
            tok = f.read_text(encoding="utf-8").strip()
    return tok


def _load_proxy() -> str:
    pr = os.environ.get("BOT_PROXY", "").strip()
    if not pr:
        f = Path(__file__).with_name("proxy.txt")
        if f.exists():
            pr = f.read_text(encoding="utf-8").strip()
    return pr

TOKEN = _load_token()
PROXY = _load_proxy()

router = Router()

# ----------------------------------------------------------------------------
# Обход DNS/DPI-блокировки api.telegram.org (актуально в РФ):
# системный DNS/DPI режет соединения рывками, поэтому храним пул рабочих IP
# и переключаемся между ними при каждой сетевой ошибке.
# ----------------------------------------------------------------------------
TG_CANDIDATE_IPS = [
    "149.154.167.220",
    "149.154.167.198",
    "149.154.166.110",
    "149.154.167.51",
]

_TG_STATE = {"ips": list(TG_CANDIDATE_IPS), "idx": 0}


def _tg_current_ip() -> str:
    return _TG_STATE["ips"][_TG_STATE["idx"] % len(_TG_STATE["ips"])]


def _tg_next_ip():
    _TG_STATE["idx"] += 1
    return _tg_current_ip()


def _tg_ip_alive(ip: str, timeout: float = 6.0) -> bool:
    """Быстрая проверка: TCP+TLS с SNI api.telegram.org."""
    ctx = ssl.create_default_context()
    try:
        s = socket.create_connection((ip, 443), timeout=timeout)
        w = ctx.wrap_socket(s, server_hostname="api.telegram.org")
        w.close()
        return True
    except Exception:  # noqa: BLE001
        return False


def _apply_tg_dns_override():
    """Патчим getaddrinfo: api.telegram.org всегда резолвится в текущий IP пула."""
    orig = socket.getaddrinfo

    def patched(host, *args, **kwargs):
        if host == "api.telegram.org":
            host = _tg_current_ip()
        return orig(host, *args, **kwargs)

    socket.getaddrinfo = patched

# ----------------------------------------------------------------------------
# Данные и утилиты
# ----------------------------------------------------------------------------
_HEROES_RU = {}
_HERE = Path(__file__).parent
with open(_HERE / "heroes_ru.json", encoding="utf-8") as fh:
    _HEROES_RU = json.load(fh)

PAGE_SIZE = 24  # кнопок героев на страницу (4 колонки x 6 рядов)


def find_hero(query: str):
    """Ищет героя по подстроке (EN, RU, алиасы). Возвращает список вариантов."""
    q = query.strip().lower().replace("ё", "е")
    if not q:
        return []
    heroes = dotabuff.get_heroes()
    exact, starts, contains = [], [], []
    for h in heroes:
        en = h["en"]
        aliases = _HEROES_RU.get(en, {}).get("aliases", [])
        haystacks = [en.lower(), en.lower().replace("-", "")] + \
                    [a.lower().replace("ё", "е") for a in aliases]
        if q in haystacks:
            exact.append(h)
        elif any(s.startswith(q) for s in haystacks):
            starts.append(h)
        elif any(q in s for s in haystacks):
            contains.append(h)
    return (exact + starts + contains)[:PAGE_SIZE]


def hero_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📜 Список героев", callback_data="page:0")],
        [InlineKeyboardButton(text="✍️ Поиск героя", callback_data="search")],
        [InlineKeyboardButton(text="🎲 Герой дня", callback_data="daily"),
         InlineKeyboardButton(text="⚔️ Контр-пик", callback_data="counter")],
    ])


# постоянное меню снизу (над полем ввода)
MENU_HOME = "🏠 Главное меню"
MENU_PICK = "🛡 Выбрать героя"
MENU_FIND = "✍️ Поиск героя"
MENU_META = "📊 Мета патча"
MENU_COUNTER = "⚔️ Контр-пик"
MENU_DAILY = "🎲 Герой дня"
MENU_MATCH = "📈 Разбор матча"


def main_menu_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=MENU_PICK), KeyboardButton(text=MENU_FIND)],
            [KeyboardButton(text=MENU_META), KeyboardButton(text=MENU_COUNTER)],
            [KeyboardButton(text=MENU_DAILY), KeyboardButton(text=MENU_MATCH)],
            [KeyboardButton(text=MENU_HOME)],
        ],
        resize_keyboard=True,
        is_persistent=True,
        input_field_placeholder="Выбери героя или напиши его имя…",
    )


def hero_nav_rows(page: int, total_pages: int):
    """Ряд навигации + ряд поиска для клавиатуры списка героев."""
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="⬅️ Назад", callback_data=f"page:{page-1}"))
    nav.append(InlineKeyboardButton(text=f"{page+1} / {total_pages}",
                                    callback_data="noop"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton(text="Вперёд ➡️", callback_data=f"page:{page+1}"))
    return [nav, [InlineKeyboardButton(text="✍️ Поиск героя", callback_data="search")]]


def hero_card_keyboard(slug: str, en: str) -> InlineKeyboardMarkup:
    e = en.replace(" ", "+")
    rows = [
        [
            InlineKeyboardButton(text="🏹 Pos 1 Carry", callback_data=f"pos:{en}:1"),
            InlineKeyboardButton(text="🎯 Pos 2 Mid", callback_data=f"pos:{en}:2"),
            InlineKeyboardButton(text="🛡 Pos 3 Off", callback_data=f"pos:{en}:3"),
        ],
        [
            InlineKeyboardButton(text="🧙 Pos 4 Soft Sup", callback_data=f"pos:{en}:4"),
            InlineKeyboardButton(text="💉 Pos 5 Hard Sup", callback_data=f"pos:{en}:5"),
            InlineKeyboardButton(text="📊 All", callback_data=f"pos:{en}:all"),
        ],
        [
            InlineKeyboardButton(text="📖 Гайды", callback_data=f"dbguide:{slug}"),
            InlineKeyboardButton(text="🩹 Патч", callback_data=f"changes:{slug}"),
            InlineKeyboardButton(text="🔄 Обновить", callback_data=f"hero:{slug}"),
        ],
        [
            InlineKeyboardButton(text="🌐 Dotabuff",
                                 url=f"{dotabuff.BASE}/heroes/{slug}"),
            InlineKeyboardButton(text="🌐 D2PT",
                                 url=f"{d2pt.BASE}/hero/{e}/builds"),
        ],
        [InlineKeyboardButton(text="⬅️ К выбору героя", callback_data="page:0")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def format_hero_card(h: dict, changes: dict | None = None) -> str:
    ru = h["en"]
    lines = [f"🛡 <b>{esc(ru)}</b>"]
    if h.get("source") == "opendota":
        lines.append("<i>источник: OpenDota API</i>")
    if h.get("attrs"):
        lines.append("🏷 " + " · ".join(esc(a) for a in h["attrs"]))
    if h.get("winrate"):
        lines.append(f"📈 Популярный герой · Винрейт: <b>{h['winrate']}</b>")

    if h["lanes"]:
        lines.append("\n🛣 <b>Lane stats</b> (presence / win rate):")
        for l in h["lanes"]:
            lines.append(f"  • {esc(l['lane'])}: {l['presence']} / {l['winrate']}")

    if h["items"]:
        lines.append("\n🧰 <b>Самые частые предметы</b> (за неделю):")
        lines.append(", ".join(
            f"{esc(i['name'])} ({i['winrate']})" for i in h["items"]))

    if h["best_vs"]:
        lines.append("\n🟢 <b>Силён против:</b> " + ", ".join(
            f"{esc(v['hero'])} ({v['adv']})" for v in h["best_vs"]))
    if h["worst_vs"]:
        lines.append("\n🔴 <b>Слаб против:</b> " + ", ".join(
            f"{esc(v['hero'])} ({v['adv']})" for v in h["worst_vs"]))

    if changes and changes.get("items"):
        lines.append(f"\n🩹 <b>Что изменилось в {esc(changes['version'])}</b>")
        for title, note in changes["items"][:5]:
            head = "" if title in ("База", "") else f"<b>{esc(title)}:</b> "
            lines.append(f"  • {head}{esc(_clip(note, 150))}")
        rest = changes.get("total", 0) - 5
        if rest > 0:
            lines.append(f"  <i>…и ещё {rest} — открой кнопку «🩹 Патч»</i>")

    lines.append(f"\n🔗 {dotabuff.BASE}/heroes/{h['slug']}")
    return "\n".join(lines)


def format_guides(h: dict, role: str, guides: list[dict]) -> str:
    title = dotabuff.ROLE_TITLES[role]
    lines = [f"📖 <b>Guides · {esc(h['en'])} · {esc(title)}</b>",
             "Топ-выступления недели на dotabuff:", ""]

    if not guides:
        lines.append("Свежих гайдов по этой роли нет (или их мало).")
        lines.append("Попробуй другую роль или «📊 All Roles».")
        return "\n".join(lines)

    for i, g in enumerate(guides, 1):
        res = "✅ Победа" if g["won"] else "❌ Поражение"
        lines.append(f"<b>{i}. {esc(g['lane'])} · {g['duration']} · {res}</b>")
        lines.append(f"👤 {esc(g['player'])} · {g['side']} · ~{g['mmr']} MMR · {g['date']}")
        if g["starting"]:
            lines.append("🎒 Старт: " + ", ".join(esc(x) for x in g["starting"]))
        if g["build"]:
            build = " → ".join(f"{esc(b['item'])} ({b['time']})" for b in g["build"])
            lines.append("📦 Билд: " + build)
        if g["abilities"]:
            order = " → ".join(
                ("⭐ " + esc(a["ability"])) if a["ability"].lower().startswith("talent")
                else esc(a["ability"])
                for a in g["abilities"][:17])
            lines.append("⚡ Прокачка: " + order)
        if g["match_id"]:
            lines.append(f"🔗 {dotabuff.BASE}/matches/{g['match_id']}")
        lines.append("")
    return "\n".join(lines)


# ----------------------------------------------------------------------------
# FSM
# ----------------------------------------------------------------------------
class Search(StatesGroup):
    waiting = State()


class Match(StatesGroup):
    waiting = State()


class Counter(StatesGroup):
    waiting = State()


def format_meta(meta: dict) -> str:
    lines = ["📊 <b>Мета патча</b>"]
    badge = patch.badge()
    if badge:
        lines.append(badge)

    name = meta.get("bracket_name") or "Все ранги"
    mmr = meta.get("mmr") or ""
    matches = meta.get("matches") or 0
    head = f"🏅 <b>{esc(name)}</b>"
    if mmr:
        head += f" · ≈{mmr} MMR"
    head += f" · {_num(matches)} матчей"
    lines.append(head)
    lines.append("<i>публичные матчи · OpenDota</i>")

    lines.append("\n🏆 <b>Топ по винрейту</b>:")
    for i, r in enumerate(meta["by_winrate"], 1):
        lines.append(f"{i}. <b>{esc(r['hero'])}</b> — "
                     f"{r['winrate']:.1f}% · {r['pickrate']:.1f}% пиков")

    lines.append("\n🔥 <b>Топ по популярности</b>:")
    for i, r in enumerate(meta["by_pick"], 1):
        lines.append(f"{i}. <b>{esc(r['hero'])}</b> — "
                     f"{r['pickrate']:.1f}% пиков · {r['winrate']:.1f}%")

    lines.append("\n<i>Выбери свой ранг кнопками ниже 👇</i>")
    return "\n".join(lines)


def meta_caption(meta: dict) -> str:
    """Короткая подпись под постером меты."""
    name = meta.get("bracket_name") or "Все ранги"
    lines = [f"📊 <b>Мета патча · {esc(name)}</b>"]
    badge = patch.badge()
    if badge:
        lines.append(badge)
    lines.append("<i>выбери ранг кнопками ниже 👇</i>")
    return "\n".join(lines)


def _num(v: int) -> str:
    if v >= 1_000_000:
        return f"{v / 1_000_000:.1f}M"
    if v >= 1_000:
        return f"{v / 1_000:.0f}k"
    return str(v)


def meta_keyboard(current: int) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(
        text=("✅ " if current == 0 else "") + "🌐 Все ранги",
        callback_data="meta:0")]]
    keys = [k for k in sorted(opendota.BRACKETS) if k != 0]
    for i in range(0, len(keys), 2):
        row = []
        for k in keys[i:i + 2]:
            b = opendota.BRACKETS[k]
            mark = "✅ " if current == k else ""
            row.append(InlineKeyboardButton(
                text=f"{mark}{b['name']} · {b['mmr']}",
                callback_data=f"meta:{k}"))
        rows.append(row)
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _meta_poster(meta: dict) -> str | None:
    """Собирает постер меты во временный файл; None — если не вышло."""
    p = patch.current()
    num = (p.get("number") or "").strip()
    parts = []
    if num and num != "—":
        parts.append(f"ПАТЧ {num}")
    date = patch.date_text(p.get("timestamp") or 0)
    if date:
        parts.append(date)
    out = str(Path(__file__).with_name("_meta.png"))
    try:
        await asyncio.to_thread(items_img.build_meta_poster, meta, out,
                                " · ".join(parts))
        return out
    except Exception:  # noqa: BLE001
        log.exception("meta poster failed")
        return None


async def _push_meta(bot, chat_id: int, meta: dict, bracket: int,
                     wait=None):
    """Постер меты с кнопками рангов; текст — только как запасной вариант."""
    kb = meta_keyboard(bracket)
    poster = await _meta_poster(meta)
    if poster:
        if wait is not None:
            try:
                await wait.delete()
            except Exception:  # noqa: BLE001
                pass
        try:
            await bot.send_photo(chat_id, FSInputFile(poster),
                                 caption=meta_caption(meta), reply_markup=kb)
            return
        except Exception:  # noqa: BLE001
            log.exception("meta poster send failed")
        finally:
            try:
                Path(poster).unlink()
            except Exception:  # noqa: BLE001
                pass
        await bot.send_message(chat_id, format_meta(meta), reply_markup=kb,
                               disable_web_page_preview=True)
        return
    if wait is not None:
        await wait.edit_text(format_meta(meta), reply_markup=kb)


MATCH_PROMPT = ("📈 Пришли номер матча — только цифры.\n"
                "Его видно в конце ссылки: dotabuff.com/matches/<b>8123456789</b>")


def _mmss(sec: int) -> str:
    return f"{sec // 60}:{sec % 60:02d}"


def _k(v: int) -> str:
    return f"{v / 1000:.1f}k" if v >= 1000 else str(v)


def _plural(n: int, one: str, few: str, many: str) -> str:
    n10, n100 = abs(n) % 10, abs(n) % 100
    if n10 == 1 and n100 != 11:
        return one
    if 2 <= n10 <= 4 and not 12 <= n100 <= 14:
        return few
    return many


def _bar(pct: float) -> str:
    n = max(0, min(10, round(pct * 10)))
    return "▰" * n + "▱" * (10 - n)


def _clip(text: str, limit: int) -> str:
    """Обрезает длинную заметку по границе слова."""
    s = (text or "").strip()
    if len(s) <= limit:
        return s
    cut = s[:limit].rsplit(" ", 1)[0].strip()
    return (cut or s[:limit]).rstrip(" ,.;:") + "…"


async def _swap(cq: CallbackQuery) -> None:
    """Убирает сообщение, на котором нажали кнопку: новый экран встаёт на его место."""
    try:
        await cq.message.delete()
    except Exception:  # noqa: BLE001
        log.debug("не удалось удалить сообщение", exc_info=True)


def match_keyboard(m: dict) -> InlineKeyboardMarkup:
    rows, cur = [], []
    for p in m["players"]:
        cur.append(InlineKeyboardButton(text=p["hero"],
                                        callback_data=f"mself:{m['id']}:{p['slot']}"))
        if len(cur) == 2:
            rows.append(cur)
            cur = []
    if cur:
        rows.append(cur)
    return InlineKeyboardMarkup(inline_keyboard=rows)


def format_match(m: dict) -> str:
    r_kills, d_kills = m["score"]
    winner = "🟢 Radiant" if m["radiant_win"] else "🔴 Dire"
    lines = [f"📈 <b>Матч {m['id']}</b> · {_mmss(m['duration'])}",
             f"🏆 <b>{winner}</b> побеждает · счёт <b>{r_kills}:{d_kills}</b>"]

    extra = []
    if m.get("first_blood"):
        extra.append(f"🩸 первая кровь {_mmss(m['first_blood'])}")
    ob = m.get("objectives")
    if ob:
        extra.append(f"🏰 башни {ob['towers'][0]}:{ob['towers'][1]}")
        if ob["roshan"][0] or ob["roshan"][1]:
            extra.append(f"🐉 Рошан {ob['roshan'][0]}:{ob['roshan'][1]}")
    if extra:
        lines.append(" · ".join(extra))

    for side, title in ((True, "🟢 <b>Radiant</b>"), (False, "🔴 <b>Dire</b>")):
        players = [p for p in m["players"] if p["radiant"] == side]
        if not players:
            continue
        t = m["totals"][side]
        kills = m["score"][0 if side else 1]
        lines.append("")
        lines.append(f"{title} · {kills} {_plural(kills, 'килл', 'килла', 'киллов')} · "
                     f"{_k(t['dmg'])} урона · {_k(t['net'])} нетворса")
        for p in players:
            lines.append(f"• <b>{esc(p['hero'])}</b> — "
                         f"{p['k']}/{p['d']}/{p['a']} · {p['gpm']} GPM · "
                         f"{_k(p['dmg'])} dmg · {p['lh']} LH")

    g = m.get("gold")
    if g and abs(g["peak"]) >= 1000:
        who = "Radiant" if g["peak"] >= 0 else "Dire"
        lines.append(f"\n💰 Максимальный перевес: <b>{who} +{_k(abs(g['peak']))}</b> "
                     f"на {g['peak_min']}-й минуте")
        if abs(g["swing"]) >= 3000:
            sw = "Radiant" if g["swing"] > 0 else "Dire"
            lines.append(f"📈 Рывок: <b>{sw} +{_k(abs(g['swing']))}</b> за минуту "
                         f"на {max(1, g['swing_min'])}-й")

    f = m.get("fight")
    if f:
        lines.append(f"⚔️ Крупнейший файт: {_mmss(f['start'])} · {f['deaths']} "
                     f"{_plural(f['deaths'], 'смерть', 'смерти', 'смертей')} · "
                     f"{_k(f['radiant_dmg'] + f['dire_dmg'])} урона")

    if m.get("mvp"):
        lines.append(f"⭐ <b>MVP: {esc(m['mvp']['hero'])}</b>")

    if not m.get("parsed"):
        lines.append("\n<i>ℹ️ Матч ещё не распарсен OpenDota: башни, файты и таймлайн "
                     "появятся позже. Перцентили игроков уже работают.</i>")

    lines.append(f"\n🔗 https://www.dotabuff.com/matches/{m['id']}")
    lines.append("<i>Нажми на своего героя — покажу перцентили и что улучшить.</i>")
    return "\n".join(lines)


_METRICS = (
    ("gold_per_min", "GPM", "{:.0f}"),
    ("xp_per_min", "XPM", "{:.0f}"),
    ("last_hits_per_min", "Добивания/мин", "{:.1f}"),
    ("hero_damage_per_min", "Урон по героям/мин", "{:.0f}"),
    ("tower_damage", "Урон по строениям", "{:.0f}"),
)


def format_personal(m: dict, slot) -> str:
    p = next((x for x in m["players"] if str(x["slot"]) == str(slot)), None)
    if not p:
        return "🤔 Не нашёл этого игрока в матче."
    side = "🟢 Radiant" if p["radiant"] else "🔴 Dire"
    team_kills = m["totals"][p["radiant"]]["kills"] or 1
    part = (p["k"] + p["a"]) / team_kills * 100
    rank = p.get("net_rank", "?")

    lines = [f"🧠 <b>{esc(p['hero'])}</b> · {side} · матч {m['id']}",
             f"{p['k']}/{p['d']}/{p['a']} · {_k(p['net'])} нетворса ({rank}-й в команде) · "
             f"участие в убийствах {part:.0f}%"]

    if p.get("items"):
        lines.append("🎒 " + ", ".join(esc(i["name"]) for i in p["items"]))

    b = p.get("benchmarks") or {}

    def pct(key: str) -> float:
        return float((b.get(key) or {}).get("pct") or 0)

    if b:
        lines.append("\n📊 <b>Перцентили на герое</b> <i>(100% — лучше всех)</i>:")
        for key, label, fmt in _METRICS:
            v = b.get(key)
            if not v:
                continue
            pr = float(v.get("pct") or 0)
            lines.append(f"{_bar(pr)} {pr * 100:.0f}% · {label} "
                         f"{fmt.format(float(v.get('raw') or 0))}")
        d = b.get("deaths_per_min")
        if d:
            pr = float(d.get("pct") or 0)
            lines.append(f"{_bar(pr)} {pr * 100:.0f}% · Выживаемость "
                         f"({p['d']} {_plural(p['d'], 'смерть', 'смерти', 'смертей')})")

        hints = []
        if pct("deaths_per_min") <= 0.3:
            hints.append(f"❌ {p['d']} {_plural(p['d'], 'смерть', 'смерти', 'смертей')} — "
                         f"чаще, чем у {(1 - pct('deaths_per_min')) * 100:.0f}% "
                         f"игроков на герое. Чаще смотри карту и не дерись без ульты.")
        elif pct("deaths_per_min") >= 0.7:
            hints.append(f"✅ {p['d']} {_plural(p['d'], 'смерть', 'смерти', 'смертей')} — "
                         f"реже, чем у {pct('deaths_per_min') * 100:.0f}% игроков.")
        if rank != "?" and rank <= 3 and pct("last_hits_per_min") < 0.4:
            lines_min = m["duration"] / 60 or 1
            hints.append(f"⚠️ Мало добиваний для кора: {p['lh']} "
                         f"({p['lh'] / lines_min:.1f}/мин).")
        if pct("hero_damage_per_min") >= 0.8:
            hints.append(f"✅ Урон по героям выше, чем у "
                         f"{pct('hero_damage_per_min') * 100:.0f}% игроков.")
        elif pct("hero_damage_per_min") and pct("hero_damage_per_min") <= 0.3:
            hints.append("⚠️ Мало урона по героям — чаще участвуй в файтах.")
        if pct("gold_per_min") and pct("gold_per_min") <= 0.3:
            hints.append("⚠️ Мало золота: пропускай меньше волн и фарми безопаснее.")
        if p.get("tower", 0) >= 3000:
            hints.append(f"✅ Хорошо ломал строения: {_k(p['tower'])} урона.")
        if p.get("obs") is None:
            hints.append("ℹ️ Варды/стаки недоступны — матч не распарсен.")
        if hints:
            lines.append("\n💡 <b>Что улучшить</b>:")
            lines.extend(hints[:4])
    else:
        lines.append("\n<i>Перцентилей нет — OpenDota не отдал статистику по герою.</i>")

    lines.append(f"\n🔗 https://www.dotabuff.com/matches/{m['id']}")
    return "\n".join(lines)


_MATCH_CACHE: dict = {}


def _cache_match(m: dict) -> dict:
    """Кеш последних матчей, чтобы кнопки «мой герой» не дёргали API заново."""
    _MATCH_CACHE[m["id"]] = (time.time(), m)
    if len(_MATCH_CACHE) > 8:
        oldest = min(_MATCH_CACHE, key=lambda k: _MATCH_CACHE[k][0])
        _MATCH_CACHE.pop(oldest, None)
    return m


async def _get_match(match_id: int) -> dict:
    hit = _MATCH_CACHE.get(match_id)
    if hit and time.time() - hit[0] < 1800:
        return hit[1]
    data = await asyncio.to_thread(opendota.get_match, match_id)
    return _cache_match(data)


async def show_match(m: Message, digits: str) -> None:
    """Разбор матча по номеру (цифры строкой)."""
    wait = await m.answer("⏳ Разбираю матч...")
    try:
        data = await _get_match(int(digits))
    except Exception:  # noqa: BLE001
        log.exception("match failed")
        await wait.edit_text("😔 Матч не найден или OpenDota ещё не разобрал его. "
                             "Попробуй другой ID.")
        return
    try:
        await wait.edit_text(format_match(data),
                             reply_markup=match_keyboard(data),
                             disable_web_page_preview=True)
    except Exception:  # noqa: BLE001
        await m.answer(format_match(data), reply_markup=match_keyboard(data),
                       disable_web_page_preview=True)

    # итоговые сборки всех 10 игроков — картинкой
    try:
        out = Path(__file__).with_name(f"_match_{data['id']}.png")
        path = await asyncio.to_thread(items_img.build_match_image,
                                       data, str(out))
        if path:
            await m.answer_photo(FSInputFile(path),
                                 caption=f"🎒 Финальные сборки · матч {data['id']}")
    except Exception:  # noqa: BLE001
        log.warning("match image failed", exc_info=True)



async def fetch_heroes():
    return await asyncio.to_thread(dotabuff.get_heroes)


async def _warm_heroes():
    """Фоновый прогрев списка героев: ошибки только в лог, без падения бота."""
    try:
        await fetch_heroes()
    except Exception as e:  # noqa: BLE001
        log.warning("не удалось прогреть список героев: %s", repr(e)[:120])
    try:
        await asyncio.to_thread(opendota._load_heroes)
    except Exception as e:  # noqa: BLE001
        log.warning("не удалось прогреть heroStats: %s", repr(e)[:120])


async def find_slug(en: str) -> str:
    """Английское имя героя -> slug на dotabuff."""
    try:
        for h in await fetch_heroes():
            if h["en"].lower() == (en or "").lower():
                return h["slug"]
    except Exception:  # noqa: BLE001
        pass
    return (en or "").lower().replace(" ", "-")


def back_to_hero_kb(slug: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="⬅️ Назад к герою", callback_data=f"hero:{slug}")]])


async def send_hero_list(chat_id, page: int, bot: Bot, message: Message | None = None):
    """Одно сообщение: сетка портретов + подпись + кнопки с именами героев."""
    heroes = await fetch_heroes()
    total = len(heroes)
    total_pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    page = max(0, min(page, total_pages - 1))
    chunk = heroes[page * PAGE_SIZE:(page + 1) * PAGE_SIZE]

    # по 3 имени в ряд — так они не обрезаются на телефоне
    rows = []
    for i in range(0, len(chunk), 3):
        rows.append([
            InlineKeyboardButton(text=h["en"], callback_data=f"hero:{h['slug']}")
            for h in chunk[i:i + 3]
        ])
    rows.extend(hero_nav_rows(page, total_pages))
    kb = InlineKeyboardMarkup(inline_keyboard=rows)

    caption = (f"🛡 <b>Выбери героя</b> · страница {page+1}/{total_pages}\n"
               f"<i>{total} героев · нажми на имя — карточка и про-билды</i>")

    grid = None
    try:
        grid_path = str(Path(__file__).with_name(f"_heroes_grid_{page}.png"))
        grid = await asyncio.to_thread(items_img.build_heroes_grid, chunk, grid_path)
    except Exception:  # noqa: BLE001
        log.exception("heroes grid failed")

    if grid:
        # при листании просто обновляем то же сообщение, не плодим новые
        if message is not None:
            try:
                await bot.edit_message_media(
                    chat_id=chat_id,
                    message_id=message.message_id,
                    media=InputMediaPhoto(media=FSInputFile(grid), caption=caption),
                    reply_markup=kb,
                )
                return
            except Exception:  # noqa: BLE001
                log.debug("edit_message_media failed, отправляю новое", exc_info=True)
        await bot.send_photo(chat_id, FSInputFile(grid), caption=caption, reply_markup=kb)
        return

    await bot.send_message(chat_id, caption, reply_markup=kb)


# ----------------------------------------------------------------------------
# Хендлеры
# ----------------------------------------------------------------------------
START_TEXT = (
    "🎮 <b>DOTA 2 · СБОРКИ И МЕТА</b>\n"
    "<i>Актуальные билды для всех 127 героев</i>\n"
    "{patch}"
    "\n"
    "🛡 <b>Выбрать героя</b> — постер, сборки и винрейт\n"
    "🔎 <b>Поиск</b> — напиши имя: <code>tinker</code>, <code>пудж</code>\n"
    "📊 <b>Мета по MMR</b> · ⚔️ <b>Контр-пик</b> · 🎲 <b>Герой дня</b>\n"
    "📈 <b>Разбор матча</b> · 🏠 <b>Главное меню</b>\n"
    "\n"
    "<blockquote>📊 Карточка героя с dotabuff · про-билды <b>Pos 1–5</b> ·"
    " иконки предметов · тайминги закупа · таланты</blockquote>\n"
    "\n"
    "<b>Выбери героя кнопкой ниже</b> 👇"
    "\n\n"
    "<i>Данные: OpenDota · Dotabuff · изображения: Valve. "
    "Фан-проект, не связан с Valve.</i>"
)

# --- таймер до перезапуска --------------------------------------------------
# На GitHub Actions бот живёт 5 ч 50 мин (timeout 21000 в workflow), затем
# процесс завершается и его поднимает новый запуск. Показываем, сколько
# осталось, — только в облаке, на ПК/VPS таймер не нужен.
_RUN_SECONDS = int(os.environ.get("BOT_RUN_SECONDS", "21000"))
_TIMER_ON = (os.environ.get("GITHUB_ACTIONS") == "true"
             or os.environ.get("BOT_TIMER") == "1")
_PROCESS_START = time.time()
_start_tasks: dict[int, "asyncio.Task"] = {}


def _left_text(seconds: float) -> str:
    sec = max(0, int(seconds))
    hours, minutes = sec // 3600, (sec % 3600) // 60
    if hours and minutes:
        return f"{hours} ч {minutes} мин"
    if hours:
        return f"{hours} ч"
    if minutes:
        return f"{minutes} мин"
    return "меньше минуты"


def _msk_time(ts: float) -> str:
    return time.strftime("%H:%M", time.gmtime(ts + 3 * 3600))


def start_text() -> str:
    """Приветствие + актуальный патч + строка таймера (если бот в облаке)."""
    badge = patch.badge()
    text = START_TEXT.replace("{patch}", (badge + "\n") if badge else "")
    if not _TIMER_ON:
        return text
    left = _RUN_SECONDS - (time.time() - _PROCESS_START)
    if left <= 0:
        line = "⏳ <b>Перезапуск бота</b> — вот-вот, подожди пару минут"
    else:
        line = (f"⏳ <b>До перезапуска бота:</b> {_left_text(left)} "
                f"<i>(≈ {_msk_time(_PROCESS_START + _RUN_SECONDS)} МСК)</i>")
    return f"{text}\n\n{line}"


def with_patch(text: str) -> str:
    """Вставляет строку патча сразу после заголовка сообщения."""
    badge = patch.badge()
    if not badge:
        return text
    first, sep, rest = text.partition("\n")
    return f"{first}\n{badge}" + (f"\n{rest}" if sep else "")


async def _countdown(bot: Bot, chat_id: int, message_id: int, photo: bool):
    """Раз в минуту обновляет строку таймера в стартовом сообщении."""
    while True:
        left = _RUN_SECONDS - (time.time() - _PROCESS_START)
        if left <= 0:
            return
        await asyncio.sleep(min(60, left))
        if _RUN_SECONDS - (time.time() - _PROCESS_START) <= 0:
            return
        text = start_text()
        try:
            if photo:
                await bot.edit_message_caption(chat_id=chat_id,
                                               message_id=message_id,
                                               caption=text)
            else:
                await bot.edit_message_text(chat_id=chat_id,
                                            message_id=message_id, text=text)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — сообщение удалили/заменили
            return


def _start_countdown(bot: Bot, chat_id: int, message_id: int, photo: bool):
    if not _TIMER_ON:
        return
    old = _start_tasks.pop(chat_id, None)
    if old is not None and not old.done():
        old.cancel()
    _start_tasks[chat_id] = asyncio.create_task(
        _countdown(bot, chat_id, message_id, photo))


@router.message(CommandStart())
async def cmd_start(m: Message, state: FSMContext):
    await state.clear()
    await send_start(m)


_banner_path = ""
_anim_path = ""


def _local_file(name: str) -> str:
    """Путь к файлу рядом с bot.py или пустая строка, если файла нет."""
    p = Path(__file__).with_name(name)
    return str(p) if p.exists() else ""


async def send_start(m: Message):
    """Стартовый экран: баннер + приветствие + нижнее меню.

    Если рядом лежит start_anim.mp4 — отправляем его как animation:
    Telegram сам зацикливает такие файлы и крутит без остановки.
    """
    global _banner_path, _anim_path
    if not _anim_path:
        _anim_path = _local_file("start_anim.mp4") or "-"
    if _anim_path != "-":
        try:
            sent = await m.answer_animation(
                FSInputFile(_anim_path),
                caption=start_text(),
                reply_markup=main_menu_kb(),
            )
        except Exception:  # noqa: BLE001 — нет файла/старый клиент, уходим в PNG
            log.exception("animation banner send failed")
        else:
            _start_countdown(m.bot, m.chat.id, sent.message_id, True)
            return
    if not _banner_path:
        p = Path(__file__).with_name("start_banner.png")
        if p.exists():
            _banner_path = str(p)
        else:
            try:
                _banner_path = await asyncio.to_thread(
                    items_img.build_start_banner, str(p), dotabuff.get_heroes()) or ""
            except Exception:  # noqa: BLE001
                log.exception("start banner failed")
                _banner_path = ""
    if _banner_path:
        try:
            sent = await m.answer_photo(FSInputFile(_banner_path),
                                        caption=start_text(),
                                        reply_markup=main_menu_kb())
        except Exception:  # noqa: BLE001
            log.exception("banner send failed")
        else:
            _start_countdown(m.bot, m.chat.id, sent.message_id, True)
            return
    sent = await m.answer(start_text(), reply_markup=main_menu_kb())
    _start_countdown(m.bot, m.chat.id, sent.message_id, False)


@router.message(Command("heroes"))
async def cmd_heroes(m: Message, state: FSMContext):
    await state.clear()
    await send_hero_list(m.chat.id, 0, m.bot)


@router.message(Command("search"))
async def cmd_search(m: Message, state: FSMContext):
    await state.set_state(Search.waiting)
    await m.answer("✍️ Напиши имя героя (по-русски или по-английски):\n"
                   "например: <i>пудж</i>, <i>па</i>, <i>invoker</i>")


@router.message(Command("meta"))
async def cmd_meta(m: Message, state: FSMContext):
    await state.clear()
    await send_meta(m)


@router.message(Command("match"))
async def cmd_match(m: Message, state: FSMContext):
    await state.clear()
    digits = "".join(ch for ch in (m.text or "") if ch.isdigit())
    if len(digits) >= 6:
        # команда сразу с номером: /match 8123456789
        await show_match(m, digits)
        return
    await state.set_state(Match.waiting)
    await m.answer(MATCH_PROMPT)


@router.message(Command("daily"))
async def cmd_daily(m: Message, state: FSMContext):
    await state.clear()
    await send_daily(m)


@router.message(Command("counter"))
async def cmd_counter(m: Message, state: FSMContext):
    await state.clear()
    tail = (m.text or "").partition(" ")[2].strip()
    if tail:
        await run_counter(m, tail)
        return
    await state.set_state(Counter.waiting)
    await m.answer(COUNTER_PROMPT)


# кнопки нижнего меню (регистрируется до поиска, чтобы работали в любом состоянии)
@router.message(F.text.in_({MENU_HOME, MENU_PICK, MENU_FIND, MENU_META,
                            MENU_COUNTER, MENU_DAILY, MENU_MATCH}))
async def on_menu_button(m: Message, state: FSMContext):
    await state.clear()
    if m.text == MENU_PICK:
        await send_hero_list(m.chat.id, 0, m.bot)
    elif m.text == MENU_FIND:
        await state.set_state(Search.waiting)
        await m.answer("✍️ Напиши имя героя (по-русски или по-английски):\n"
                       "например: <i>пудж</i>, <i>па</i>, <i>tinker</i>")
    elif m.text == MENU_META:
        await send_meta(m)
    elif m.text == MENU_COUNTER:
        await state.set_state(Counter.waiting)
        await m.answer(COUNTER_PROMPT)
    elif m.text == MENU_DAILY:
        await send_daily(m)
    elif m.text == MENU_MATCH:
        await state.set_state(Match.waiting)
        await m.answer(MATCH_PROMPT)
    else:
        await send_start(m)


async def send_meta(m: Message, bracket: int = 0):
    wait = await m.answer("⏳ Загружаю мету патча...")
    try:
        meta = await asyncio.to_thread(opendota.get_meta, bracket)
    except Exception:  # noqa: BLE001
        log.exception("meta failed")
        await wait.edit_text("😔 Не удалось получить мету, попробуй позже.")
        return
    await _push_meta(m.bot, m.chat.id, meta, bracket, wait)


# ---------------------------------------------------------------------------
# Изменения героя в текущем патче
# ---------------------------------------------------------------------------
async def hero_changes(en: str) -> dict | None:
    """Заметки текущего патча по герою (или None)."""
    try:
        hid = await asyncio.to_thread(opendota.hero_id, en)
        if not hid:
            return None
        return await asyncio.to_thread(patch.hero_changes, hid)
    except Exception:  # noqa: BLE001
        log.debug("hero_changes failed", exc_info=True)
        return None


def format_changes(hero_en: str, changes: dict) -> str:
    lines = [f"🩹 <b>{esc(hero_en)}</b> · патч <b>{esc(changes['version'])}</b>",
             "<i>официальные заметки dota2.com</i>", ""]
    used = sum(len(x) for x in lines)
    shown = 0
    current = None
    for title, note in changes["items"]:
        head = ""
        if title != current:
            head = f"\n<b>{esc(title or 'Изменения')}</b>"
            current = title
        piece = f"{head}\n  • {esc(note)}"
        if used + len(piece) > 3800:
            break
        lines.append(piece)
        used += len(piece)
        shown += 1
    rest = changes.get("total", 0) - shown
    if rest > 0:
        lines.append(f"\n<i>Показаны первые {shown} из "
                     f"{changes['total']} изменений.</i>")
    return "\n".join(lines)


# Сборки от конкретных игроков: показываем в подписи под постером.
PLAYER_BUILDS = {
    "lone-druid": {
        "author": "@tgkpyatka69",
        "author_url": "https://t.me/tgkpyatka69",
        "guide_url": "https://steamcommunity.com/sharedfiles/filedetails/?id=2992251175",
    },
}


def hero_caption(en: str, changes: dict | None, slug: str = "") -> str:
    """Короткая подпись под постером: имя, патч, подсказка про кнопки."""
    lines = [f"🛡 <b>{esc(en)}</b>"]
    if changes and changes.get("total"):
        n = changes["total"]
        lines.append(f"🩹 в патче {esc(changes['version'])}: {n} "
                     f"{_plural(n, 'изменение', 'изменения', 'изменений')}")
    lines.append("<i>сборки по позициям — кнопками ниже</i>")

    player = (PLAYER_BUILDS.get((slug or "").lower())
              or PLAYER_BUILDS.get(en.lower().replace(" ", "-")))
    if player:
        lines.append("")
        lines.append(f"⭐ <b>Сборка от игрока</b> · "
                     f"<a href=\"{player['author_url']}\">{esc(player['author'])}</a>")
        lines.append(f"<a href=\"{player['guide_url']}\">Гайд со сборкой на Steam →</a>")
    return "\n".join(lines)


async def _hero_poster(data: dict, slug: str) -> str | None:
    """Собирает постер героя во временный файл; None — если не вышло."""
    num = (patch.current().get("number") or "").strip()
    subtitle = f"ПАТЧ {num}" if num and num != "—" else ""
    out = str(Path(__file__).with_name(f"_hero_{slug}.png"))

    # топ-3 сборки по позициям (D2PT) + предметы лучшей сборки
    poster_data = dict(data)
    poster_data["top_builds"] = []
    poster_data["top_build"] = None
    player = PLAYER_BUILDS.get((slug or "").lower())
    if player:
        poster_data["author_note"] = player["guide_url"].replace("https://", "")
    try:
        builds = await asyncio.wait_for(
            asyncio.to_thread(d2pt.get_builds, data.get("en") or ""),
            timeout=10)
        top = d2pt.top_positions(builds)
        poster_data["top_builds"] = top
        if top:
            pos = top[0]["pos"]
            build = (builds.get("positions") or {}).get(pos)
            if not build:
                build = await asyncio.wait_for(
                    asyncio.to_thread(d2pt.get_position_build,
                                      data.get("en") or "", pos),
                    timeout=10)
            build = dict(build)
            build["label"] = top[0]["short"]
            poster_data["top_build"] = build
    except Exception:  # noqa: BLE001
        log.debug("d2pt builds for poster failed", exc_info=True)

    try:
        await asyncio.to_thread(items_img.build_hero_poster, poster_data, out,
                                subtitle)
        return out
    except Exception:  # noqa: BLE001
        log.exception("hero poster failed")
        return None


# ---------------------------------------------------------------------------
# Герой дня
# ---------------------------------------------------------------------------
def hero_of_day(heroes: list[dict]) -> tuple[dict, str]:
    """Один и тот же герой в течение дня (по МСК)."""
    now = time.gmtime(time.time() + 3 * 3600)
    seed = int(hashlib.sha256(time.strftime("%Y-%m-%d", now).encode()).hexdigest(), 16)
    return heroes[seed % len(heroes)], time.strftime("%d.%m.%Y", now)


async def send_daily(m: Message):
    try:
        heroes = await fetch_heroes()
    except Exception:  # noqa: BLE001
        log.exception("daily heroes failed")
        await m.answer("😔 Не удалось получить список героев, попробуй позже.")
        return
    h, day = hero_of_day(heroes)
    await m.answer(f"🎲 <b>Герой дня</b> · {day}\n"
                   f"<i>Каждый день новый герой — заходи проверить.</i>")
    await show_hero(m, h)


# ---------------------------------------------------------------------------
# Контр-пик по драфту
# ---------------------------------------------------------------------------
COUNTER_PROMPT = ("⚔️ Напиши героев врага через запятую (1–5):\n"
                  "например: <code>pudge, invoker, am</code>\n"
                  "можно по-русски: <code>пудж, инвокер, мортра</code>")


def format_counters(data: dict) -> str:
    enemies = ", ".join(f"<b>{esc(x)}</b>" for x in data["enemies"])
    lines = ["⚔️ <b>Контр-пик по драфту</b>", f"Враги: {enemies}", ""]
    if not data["rows"]:
        lines.append("Не нашёл героев с плюсовым матчапом — попробуй другой драфт.")
        return "\n".join(lines)
    for i, r in enumerate(data["rows"], 1):
        lines.append(f"{i}. <b>{esc(r['hero'])}</b> — "
                     f"{r['advantage']:+.1f}% в среднем")
        detail = " · ".join(f"{esc(k)} {v:+.1f}%"
                            for k, v in r["details"].items())
        if detail:
            lines.append(f"    ↳ {detail}")
    lines.append("\n<i>Преимущество — средняя разница винрейта против выбранных "
                 "врагов (OpenDota, публичные матчи).</i>")
    return "\n".join(lines)


def counter_keyboard(rows: list[dict], heroes: list[dict]):
    by_name = {h["en"]: h["slug"] for h in heroes}
    buttons = []
    for r in rows:
        slug = by_name.get(r["hero"]) or r["hero"].lower().replace(" ", "-")
        buttons.append(InlineKeyboardButton(text=r["hero"],
                                            callback_data=f"hero:{slug}"))
    kb = [buttons[i:i + 2] for i in range(0, len(buttons), 2)]
    return InlineKeyboardMarkup(inline_keyboard=kb) if kb else None


async def run_counter(m: Message, raw: str):
    parts = [p.strip() for p in re.split(r"[,;\n]+", raw or "") if p.strip()][:5]
    if not parts:
        await m.answer(COUNTER_PROMPT)
        return

    chosen, unknown, seen = [], [], set()
    for p in parts:
        try:
            matches = await asyncio.to_thread(find_hero, p)
        except Exception:  # noqa: BLE001
            matches = []
        if not matches:
            unknown.append(p)
            continue
        h = matches[0]
        if h["slug"] not in seen:
            seen.add(h["slug"])
            chosen.append(h)

    if not chosen:
        await m.answer("🤷 Не нашёл ни одного героя. Попробуй ещё раз, например: "
                       "<code>pudge, invoker</code>")
        return

    wait = await m.answer("⏳ Считаю контр-пики...")
    try:
        data = await asyncio.to_thread(opendota.get_counters,
                                       [h["slug"] for h in chosen])
    except Exception:  # noqa: BLE001
        log.exception("counter failed")
        await wait.edit_text("😔 Не удалось посчитать контр-пики, попробуй позже.")
        return

    try:
        heroes = await fetch_heroes()
    except Exception:  # noqa: BLE001
        heroes = []

    text = format_counters(data)
    if unknown:
        text += f"\n\n<i>Не нашёл: {esc(', '.join(unknown))}</i>"
    try:
        await wait.edit_text(text, reply_markup=counter_keyboard(data["rows"], heroes),
                             disable_web_page_preview=True)
    except Exception:  # noqa: BLE001
        log.exception("counter send failed")


# ---------------------------------------------------------------------------
# Премиум-эмодзи (custom emoji): бот может слать их, только если у него есть
# купленный на Fragment юзернейм. Здесь собираем emoji-id из присланных
# сообщений, чтобы вставить их в текст через <tg-emoji emoji-id="...">.
# ---------------------------------------------------------------------------
def tg_emoji(char: str, emoji_id: str) -> str:
    return f'<tg-emoji emoji-id="{emoji_id}">{char}</tg-emoji>'


def _has_custom_emoji(m: Message) -> bool:
    return bool(m.entities) and any(e.type == "custom_emoji" for e in m.entities)


@router.message(_has_custom_emoji)
async def on_custom_emoji(m: Message):
    ids = [e.custom_emoji_id for e in m.entities
           if e.type == "custom_emoji" and e.custom_emoji_id]
    if ids:
        body = "\n".join(f"<code>{i}</code>" for i in ids)
        await m.answer("🆔 <b>Custom emoji IDs</b>\n" + body +
                       "\n\nПришли эти ID мне — вставлю их в оформление.")


@router.message(F.text, Search.waiting)
async def on_search(m: Message, state: FSMContext):
    try:
        matches = await asyncio.to_thread(find_hero, m.text)
    except Exception:  # noqa: BLE001
        log.exception("search failed")
        await m.answer("😔 Сайт статистики не отвечает, попробуй ещё раз через минуту.")
        return
    if not matches:
        await m.answer("🤷 Герой не найден. Попробуй другое написание (например: «pa», «invoker», «pudge»).")
        return
    await state.clear()
    if len(matches) == 1:
        await show_hero(m, matches[0])
        return
    rows = []
    for h in matches:
        rows.append([InlineKeyboardButton(text=h["en"],
                                          callback_data=f"hero:{h['slug']}")])
    await m.answer("🔎 Несколько совпадений — выбери:",
                   reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


@router.message(F.text, Match.waiting)
async def on_match(m: Message, state: FSMContext):
    digits = "".join(ch for ch in (m.text or "") if ch.isdigit())
    if len(digits) < 6:
        await m.answer("🤔 Нужен номер матча — только цифры, например <b>8123456789</b>.")
        return
    await state.clear()
    await show_match(m, digits)


@router.message(F.text, Counter.waiting)
async def on_counter(m: Message, state: FSMContext):
    await state.clear()
    await run_counter(m, m.text or "")


@router.callback_query(F.data.startswith("meta:"))
async def cb_meta(cq: CallbackQuery):
    try:
        key = int(cq.data.split(":", 1)[1])
    except ValueError:
        await cq.answer("Некорректный ранг")
        return
    await cq.answer()
    chat_id = cq.message.chat.id
    await _swap(cq)
    msg = await cq.bot.send_message(chat_id, "⏳ Загружаю мету патча…")
    try:
        meta = await asyncio.to_thread(opendota.get_meta, key)
    except Exception:  # noqa: BLE001
        log.exception("meta bracket failed")
        await msg.edit_text("😔 Нет данных по этому рангу, попробуй другой.")
        return
    await _push_meta(cq.bot, chat_id, meta, key, msg)


@router.callback_query(F.data.startswith("mself:"))
async def cb_match_self(cq: CallbackQuery):
    try:
        _, mid, slot = cq.data.split(":")
        mid = int(mid)
    except ValueError:
        await cq.answer("Некорректная кнопка")
        return
    try:
        data = await _get_match(mid)
    except Exception:  # noqa: BLE001
        log.exception("match load failed")
        await cq.answer("Не удалось загрузить матч", show_alert=True)
        return
    await cq.answer()
    chat_id = cq.message.chat.id
    await _swap(cq)
    back = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="⬅️ К сводке матча",
                             callback_data=f"mmatch:{mid}")]])
    await cq.bot.send_message(chat_id, format_personal(data, slot),
                              reply_markup=back,
                              disable_web_page_preview=True)


@router.callback_query(F.data.startswith("mmatch:"))
async def cb_match_summary(cq: CallbackQuery):
    try:
        mid = int(cq.data.split(":", 1)[1])
    except ValueError:
        await cq.answer("Некорректная кнопка")
        return
    try:
        data = await _get_match(mid)
    except Exception:  # noqa: BLE001
        log.exception("match load failed")
        await cq.answer("Не удалось загрузить матч", show_alert=True)
        return
    await cq.answer()
    chat_id = cq.message.chat.id
    await _swap(cq)
    await cq.bot.send_message(chat_id, format_match(data),
                              reply_markup=match_keyboard(data),
                              disable_web_page_preview=True)


@router.message(F.text)
async def fallback_text(m: Message):
    # если пользователь просто написал имя героя без команды
    try:
        matches = await asyncio.to_thread(find_hero, m.text)
    except Exception:  # noqa: BLE001
        log.exception("search failed")
        await m.answer("😔 Сайт статистики не отвечает, попробуй ещё раз через минуту.")
        return
    if not matches:
        await m.answer("Не понял 🤔 Используй /start — там список героев и поиск.")
        return
    if len(matches) == 1:
        await show_hero(m, matches[0])
        return
    rows = [[InlineKeyboardButton(text=h["en"], callback_data=f"hero:{h['slug']}")]
            for h in matches]
    await m.answer("🔎 Похоже на несколько героев — выбери:",
                   reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


async def show_hero(m_or_cq, h: dict):
    """Главный экран героя — постер. Message: ответ, CallbackQuery: замена."""
    chat_id = (m_or_cq.chat.id if isinstance(m_or_cq, Message)
               else m_or_cq.message.chat.id)
    bot = m_or_cq.bot
    if isinstance(m_or_cq, Message):
        wait = await m_or_cq.answer("⏳ Загружаю героя...")
    else:
        await m_or_cq.answer()
        wait = await bot.send_message(chat_id, "⏳ Загружаю героя...")

    try:
        data = await asyncio.to_thread(dotabuff.get_hero_overview, h["slug"])
    except Exception:  # noqa: BLE001
        log.exception("hero overview failed")
        await wait.edit_text("😔 Не удалось получить данные с dotabuff, попробуй позже.")
        return

    en = data.get("en") or h.get("en") or h["slug"].replace("-", " ").title()
    changes = await hero_changes(en)
    kb = hero_card_keyboard(h["slug"], en)
    poster = await _hero_poster(data, h["slug"])

    try:
        await wait.delete()
    except Exception:  # noqa: BLE001
        pass
    if not isinstance(m_or_cq, Message):
        # замена экрана: убираем сообщение, на котором нажали кнопку
        await _swap(m_or_cq)

    if poster:
        try:
            await bot.send_photo(chat_id, FSInputFile(poster),
                                 caption=hero_caption(en, changes, h.get("slug", "")),
                                 reply_markup=kb)
            return
        except Exception:  # noqa: BLE001
            log.exception("hero poster send failed")
        finally:
            try:
                Path(poster).unlink()
            except Exception:  # noqa: BLE001
                pass

    # запасной вариант — текстовая карточка
    await bot.send_message(chat_id, format_hero_card(data, changes),
                           reply_markup=kb, disable_web_page_preview=True)


@router.callback_query(F.data == "daily")
async def cb_daily(cq: CallbackQuery):
    await cq.answer()
    chat_id = cq.message.chat.id
    await _swap(cq)
    await send_daily(cq.message)
    # send_daily отвечает на удалённое сообщение — это нормально, ответ уходит в чат


@router.callback_query(F.data.startswith("poster:"))
async def cb_poster(cq: CallbackQuery):
    slug = cq.data.split(":", 1)[1]
    await cq.answer("🖼 Загружаю героя…")
    chat_id = cq.message.chat.id
    await _swap(cq)
    msg = await cq.bot.send_message(chat_id, "⏳ Загружаю героя...")
    try:
        data = await asyncio.to_thread(dotabuff.get_hero_overview, slug)
    except Exception:  # noqa: BLE001
        log.exception("poster hero failed")
        await msg.edit_text("😔 Не удалось собрать постер, попробуй позже.")
        return
    out = await _hero_poster(data, slug)
    if not out:
        await msg.edit_text("😔 Не удалось собрать постер, попробуй позже.")
        return
    try:
        await msg.delete()
    except Exception:  # noqa: BLE001
        pass
    await cq.bot.send_photo(
        chat_id, FSInputFile(out),
        caption=f"🖼 <b>{esc(data.get('en') or slug)}</b> · постер для шаринга",
        reply_markup=back_to_hero_kb(slug))
    try:
        Path(out).unlink()
    except Exception:  # noqa: BLE001
        pass


@router.callback_query(F.data.startswith("changes:"))
async def cb_changes(cq: CallbackQuery):
    slug = cq.data.split(":", 1)[1]
    await cq.answer()
    chat_id = cq.message.chat.id
    en = slug.replace("-", " ").title()
    try:
        for h in await fetch_heroes():
            if h["slug"] == slug:
                en = h["en"]
                break
    except Exception:  # noqa: BLE001
        pass
    await _swap(cq)
    msg = await cq.bot.send_message(chat_id, "⏳ Ищу изменения в патче...")
    changes = await hero_changes(en)
    if not changes:
        num = (patch.current().get("number") or "").strip()
        await msg.edit_text(
            f"🩹 <b>{esc(en)}</b>\n\n"
            f"<i>В патче {esc(num or '')} герой не менялся.</i>",
            reply_markup=back_to_hero_kb(slug))
        return
    await msg.edit_text(format_changes(en, changes),
                        reply_markup=back_to_hero_kb(slug),
                        disable_web_page_preview=True)


@router.callback_query(F.data.startswith("fav:"))
@router.callback_query(F.data == "favclear")
@router.callback_query(F.data.startswith("profile:"))
async def cb_legacy_button(cq: CallbackQuery):
    """Кнопки из старых сообщений: избранное и профиль больше не нужны."""
    await cq.answer("Эта кнопка больше не используется — открой героя заново 🙂")


@router.callback_query(F.data == "counter")
async def cb_counter(cq: CallbackQuery, state: FSMContext):
    await cq.answer()
    chat_id = cq.message.chat.id
    await _swap(cq)
    await state.set_state(Counter.waiting)
    await cq.bot.send_message(chat_id, COUNTER_PROMPT)


@router.callback_query(F.data == "noop")
async def cb_noop(cq: CallbackQuery):
    await cq.answer()


@router.callback_query(F.data == "search")
async def cb_search(cq: CallbackQuery, state: FSMContext):
    await cq.answer()
    chat_id = cq.message.chat.id
    await _swap(cq)
    await state.set_state(Search.waiting)
    await cq.bot.send_message(
        chat_id,
        "✍️ Напиши имя героя (по-русски или по-английски):\n"
        "например: <i>пудж</i>, <i>па</i>, <i>invoker</i>",
    )


@router.callback_query(F.data.startswith("page:"))
async def cb_page(cq: CallbackQuery):
    """Листание списка героев: не удаляем сообщение, а обновляем его на месте."""
    page = int(cq.data.split(":")[1])
    await cq.answer()
    chat_id = cq.message.chat.id
    await send_hero_list(chat_id, page, cq.bot, cq.message)


@router.callback_query(F.data.startswith("hero:"))
async def cb_hero(cq: CallbackQuery):
    slug = cq.data.split(":", 1)[1]
    await show_hero(cq, {"slug": slug, "en": slug.replace("-", " ").title()})


@router.callback_query(F.data.startswith("pos:"))
async def cb_pos(cq: CallbackQuery):
    _, en, pos_raw = cq.data.split(":", 2)
    try:
        pos = pos_raw if pos_raw == "all" else int(pos_raw)
    except ValueError:
        await cq.answer("Неизвестная позиция")
        return
    await cq.answer()
    chat_id = cq.message.chat.id
    await _swap(cq)
    msg = await cq.bot.send_message(chat_id, "⏳ Загружаю про-билд с D2PT...")
    slug = await find_slug(en)
    back_kb = back_to_hero_kb(slug)
    try:
        data = await asyncio.to_thread(d2pt.get_builds, en)
        text = with_patch(d2pt.format_position(data, pos))
    except Exception:  # noqa: BLE001
        log.exception("d2pt build failed")
        try:
            await msg.edit_text("😔 D2PT недоступен. Попробуй позже или кнопку «📖 Dotabuff Guides».",
                                reply_markup=back_kb)
        except Exception:  # noqa: BLE001
            pass
        return
    try:
        await msg.edit_text(text, reply_markup=back_kb,
                            disable_web_page_preview=True)
    except Exception:  # noqa: BLE001
        await cq.bot.send_message(chat_id, text, reply_markup=back_kb,
                                  disable_web_page_preview=True)

    # иконки предметов компактной картинкой (старт / кор с таймингами / финал)
    if pos != "all":
        try:
            grid_path = str(Path(__file__).with_name(
                f"_items_{chat_id}_{abs(hash(en)) % 100000}_{pos}.png"))
            grid = await asyncio.to_thread(items_img.build_items_image,
                                           data, pos, grid_path)
            if grid:
                await cq.bot.send_photo(
                    chat_id, FSInputFile(grid),
                    caption=f"🛠 Предметы · {en} · {d2pt.POS_TITLES.get(pos, pos)}")
        except Exception:  # noqa: BLE001
            log.exception("items image failed")


@router.callback_query(F.data.startswith("dbguide:"))
async def cb_dbguide(cq: CallbackQuery):
    slug = cq.data.split(":", 1)[1]
    await cq.answer()
    chat_id = cq.message.chat.id
    await _swap(cq)
    msg = await cq.bot.send_message(chat_id, "⏳ Загружаю гайды с dotabuff...")
    try:
        guides = await asyncio.to_thread(dotabuff.get_guides, slug)
        hero = {"en": slug.replace("-", " ").title(), "slug": slug}
        selected = guides[:3]
        await msg.edit_text(format_guides(hero, "all", selected),
                            reply_markup=back_to_hero_kb(slug),
                            disable_web_page_preview=True)
        return
    except Exception:  # noqa: BLE001
        log.exception("guides failed")

    # dotabuff недоступен — показываем про-билды с D2PT вместо гайдов
    try:
        en = slug.replace("-", " ").title()
        for h in await fetch_heroes():
            if h["slug"] == slug:
                en = h["en"]
                break
        data = await asyncio.to_thread(d2pt.get_builds, en)
        text = ("📖 <b>Гайды dotabuff недоступны с этого сервера</b>\n"
                "Показываю про-билды с dota2protracker:\n\n"
                + d2pt.format_position(data, "all"))
        await msg.edit_text(with_patch(text), disable_web_page_preview=True)
    except Exception:  # noqa: BLE001
        log.exception("d2pt fallback failed")
        await msg.edit_text("😔 Не удалось загрузить гайды, попробуй позже.")


# ----------------------------------------------------------------------------
# Ретрай сетевых запросов к Telegram API (у некоторых провайдеров
# соединение нестабильное — часть запросов падает по таймауту)
# ----------------------------------------------------------------------------
from aiogram.client.session.middlewares.base import BaseRequestMiddleware
from aiogram.exceptions import TelegramNetworkError


class RetryOnNetworkError(BaseRequestMiddleware):
    def __init__(self, retries: int = 5, backoff: float = 0.75):
        self.retries = retries
        self.backoff = backoff

    async def __call__(self, make_request, bot, method):
        last_exc = None
        for attempt in range(self.retries):
            try:
                return await make_request(bot, method)
            except TelegramNetworkError as e:
                last_exc = e
                if attempt < self.retries - 1:
                    # меняем IP Telegram-сервера и пробуем снова
                    new_ip = await asyncio.to_thread(_rotate_to_alive_ip)
                    log.warning("network error (%s), retry #%d via %s",
                                type(e).__name__, attempt + 1, new_ip)
                    await asyncio.sleep(self.backoff * (attempt + 1))
        raise last_exc


def _rotate_to_alive_ip() -> str:
    """Переключает пул на следующий живой IP (TLS-проверка)."""
    # переставляем пул так, чтобы текущий IP ушёл в конец
    cur = _tg_current_ip()
    ips = [ip for ip in _TG_STATE["ips"] if ip != cur]
    ips.append(cur)
    _TG_STATE["ips"] = ips
    # ищем первый живой; если ни один не ответил — всё равно ротируем
    for _ in range(len(ips)):
        ip = _tg_current_ip()
        if _tg_ip_alive(ip):
            return ip
        _tg_next_ip()
    return _tg_current_ip()


def _log_sources_status():
    """Пишет в лог, какие источники доступны с сервера (для диагностики)."""
    checks = (
        ("dotabuff /heroes", lambda: f"{len(dotabuff.get_heroes())} героев"),
        ("dota2protracker", lambda: sorted(d2pt.get_builds("Tinker")["positions"])),
        ("патч dota2.com", lambda: patch.refresh(True)["number"]),
    )
    for name, fn in checks:
        try:
            log.info("источник %-18s OK: %s", name, fn())
        except Exception as e:  # noqa: BLE001
            log.warning("источник %-18s НЕДОСТУПЕН: %s", name, repr(e)[:120])


async def _patch_watch():
    """Следит за актуальным патчем: обновляет раз в 3 часа в фоне."""
    while True:
        try:
            await asyncio.to_thread(patch.refresh, True)
        except Exception:  # noqa: BLE001
            pass
        await asyncio.sleep(3 * 3600)


async def main():
    if not TOKEN:
        print("Не найден токен бота!")
        print("Укажи переменную окружения BOT_TOKEN или создай файл token.txt с токеном.")
        return

    _apply_tg_dns_override()
    # прогрев кеша списка героев в фоне
    asyncio.create_task(_warm_heroes())
    # актуальный патч Dota 2 (dota2.com/patches)
    asyncio.create_task(_patch_watch())
    # проверка источников данных (не блокирует запуск)
    asyncio.create_task(asyncio.to_thread(_log_sources_status))

    dp = Dispatcher()
    dp.include_router(router)

    while True:
        # при старте каждой сессии выбираем живой IP
        try:
            ip = await asyncio.to_thread(_rotate_to_alive_ip)
            log.info("Telegram API через %s", ip)
        except Exception as e:  # noqa: BLE001
            log.warning("не удалось найти живой IP (%s), повтор через 15с", e)
            await asyncio.sleep(15)
            continue

        bot = Bot(TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML),
                  session=AiohttpSession(proxy=PROXY or None))
        bot.session.middleware(RetryOnNetworkError())
        # команды в кнопке "Menu" слева от поля ввода
        try:
            from aiogram.types import BotCommand
            await bot.set_my_commands([
                BotCommand(command="start", description="🏠 Главное меню"),
                BotCommand(command="heroes", description="🛡 Выбрать героя"),
                BotCommand(command="search", description="✍️ Поиск героя"),
                BotCommand(command="meta", description="📊 Мета патча по MMR"),
                BotCommand(command="counter", description="⚔️ Контр-пик по драфту"),
                BotCommand(command="daily", description="🎲 Герой дня"),
                BotCommand(command="match", description="📈 Разбор матча"),
            ])
        except Exception:  # noqa: BLE001
            log.warning("не удалось установить команды меню")
        try:
            await dp.start_polling(bot)
            break  # корректная остановка (Ctrl+C)
        except Exception as e:  # noqa: BLE001
            log.error("поллинг упал: %r — перезапуск через 10с", e)
            try:
                await bot.session.close()
            except Exception:  # noqa: BLE001
                pass
            await asyncio.sleep(10)


if __name__ == "__main__":
    asyncio.run(main())
