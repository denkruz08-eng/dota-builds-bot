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
import html
import json
import logging
import os
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

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("dota-bot")

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
    ])


# постоянное меню снизу (над полем ввода)
MENU_HOME = "🏠 Главное меню"
MENU_PICK = "🛡 Выбрать героя"
MENU_FIND = "✍️ Поиск героя"


def main_menu_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=MENU_PICK), KeyboardButton(text=MENU_FIND)],
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
            InlineKeyboardButton(text="📖 Dotabuff Guides", callback_data=f"dbguide:{slug}"),
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


def format_hero_card(h: dict) -> str:
    ru = h["en"]
    lines = [f"🛡 <b>{html.escape(ru)}</b>"]
    if h.get("attrs"):
        lines.append("🏷 " + " · ".join(html.escape(a) for a in h["attrs"]))
    if h.get("winrate"):
        lines.append(f"📈 Популярный герой · Винрейт: <b>{h['winrate']}</b>")

    if h["lanes"]:
        lines.append("\n🛣 <b>Lane stats</b> (presence / win rate):")
        for l in h["lanes"]:
            lines.append(f"  • {html.escape(l['lane'])}: {l['presence']} / {l['winrate']}")

    if h["items"]:
        lines.append("\n🧰 <b>Самые частые предметы</b> (за неделю):")
        lines.append(", ".join(
            f"{html.escape(i['name'])} ({i['winrate']})" for i in h["items"]))

    if h["best_vs"]:
        lines.append("\n🟢 <b>Силён против:</b> " + ", ".join(
            f"{html.escape(v['hero'])} ({v['adv']})" for v in h["best_vs"]))
    if h["worst_vs"]:
        lines.append("\n🔴 <b>Слаб против:</b> " + ", ".join(
            f"{html.escape(v['hero'])} ({v['adv']})" for v in h["worst_vs"]))

    lines.append(f"\n🔗 {dotabuff.BASE}/heroes/{h['slug']}")
    return "\n".join(lines)


def format_guides(h: dict, role: str, guides: list[dict]) -> str:
    title = dotabuff.ROLE_TITLES[role]
    lines = [f"📖 <b>Guides · {html.escape(h['en'])} · {html.escape(title)}</b>",
             "Топ-выступления недели на dotabuff:", ""]

    if not guides:
        lines.append("Свежих гайдов по этой роли нет (или их мало).")
        lines.append("Попробуй другую роль или «📊 All Roles».")
        return "\n".join(lines)

    for i, g in enumerate(guides, 1):
        res = "✅ Победа" if g["won"] else "❌ Поражение"
        lines.append(f"<b>{i}. {html.escape(g['lane'])} · {g['duration']} · {res}</b>")
        lines.append(f"👤 {html.escape(g['player'])} · {g['side']} · ~{g['mmr']} MMR · {g['date']}")
        if g["starting"]:
            lines.append("🎒 Старт: " + ", ".join(html.escape(x) for x in g["starting"]))
        if g["build"]:
            build = " → ".join(f"{html.escape(b['item'])} ({b['time']})" for b in g["build"])
            lines.append("📦 Билд: " + build)
        if g["abilities"]:
            order = " → ".join(
                ("⭐ " + html.escape(a["ability"])) if a["ability"].lower().startswith("talent")
                else html.escape(a["ability"])
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


async def fetch_heroes():
    return await asyncio.to_thread(dotabuff.get_heroes)


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
    "\n"
    "🛡 <b>Выбрать героя</b> — портреты и статистика\n"
    "🔎 <b>Поиск</b> — напиши имя: <code>tinker</code>, <code>пудж</code>\n"
    "🏠 <b>Главное меню</b> — этот экран\n"
    "\n"
    "<blockquote>📊 Карточка героя с dotabuff · про-билды <b>Pos 1–5</b> ·"
    " иконки предметов · тайминги закупа · таланты</blockquote>\n"
    "\n"
    "<b>Выбери героя кнопкой ниже</b> 👇"
)


@router.message(CommandStart())
async def cmd_start(m: Message, state: FSMContext):
    await state.clear()
    await send_start(m)


_banner_path = ""


async def send_start(m: Message):
    """Стартовый экран: баннер + приветствие + нижнее меню."""
    global _banner_path
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
            await m.answer_photo(FSInputFile(_banner_path),
                                 caption=START_TEXT,
                                 reply_markup=main_menu_kb())
            return
        except Exception:  # noqa: BLE001
            log.exception("banner send failed")
    await m.answer(START_TEXT, reply_markup=main_menu_kb())


@router.message(Command("heroes"))
async def cmd_heroes(m: Message, state: FSMContext):
    await state.clear()
    await send_hero_list(m.chat.id, 0, m.bot)


@router.message(Command("search"))
async def cmd_search(m: Message, state: FSMContext):
    await state.set_state(Search.waiting)
    await m.answer("✍️ Напиши имя героя (по-русски или по-английски):\n"
                   "например: <i>пудж</i>, <i>па</i>, <i>invoker</i>")


# кнопки нижнего меню (регистрируется до поиска, чтобы работали в любом состоянии)
@router.message(F.text.in_({MENU_HOME, MENU_PICK, MENU_FIND}))
async def on_menu_button(m: Message, state: FSMContext):
    await state.clear()
    if m.text == MENU_PICK:
        await send_hero_list(m.chat.id, 0, m.bot)
    elif m.text == MENU_FIND:
        await state.set_state(Search.waiting)
        await m.answer("✍️ Напиши имя героя (по-русски или по-английски):\n"
                       "например: <i>пудж</i>, <i>па</i>, <i>tinker</i>")
    else:
        await send_start(m)


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
    """m_or_cq: Message (ответ) или CallbackQuery (edit)."""
    if isinstance(m_or_cq, Message):
        wait = await m_or_cq.answer("⏳ Собираю данные с dotabuff...")
        try:
            data = await asyncio.to_thread(dotabuff.get_hero_overview, h["slug"])
        except Exception as e:  # noqa: BLE001
            log.exception("hero overview failed")
            await wait.edit_text("😔 Не удалось получить данные с dotabuff, попробуй позже.")
            return
        en = data.get("en") or h.get("en") or h["slug"].replace("-", " ").title()
        await wait.edit_text(format_hero_card(data),
                             reply_markup=hero_card_keyboard(h["slug"], en),
                             disable_web_page_preview=True)
    else:
        cq = m_or_cq
        await cq.answer()
        msg = await cq.bot.send_message(cq.message.chat.id, "⏳ Собираю данные с dotabuff...")
        try:
            data = await asyncio.to_thread(dotabuff.get_hero_overview, h["slug"])
        except Exception:  # noqa: BLE001
            log.exception("hero overview failed")
            await msg.edit_text("😔 Не удалось получить данные с dotabuff, попробуй позже.")
            return
        en = data.get("en") or h.get("en") or h["slug"].replace("-", " ").title()
        await msg.delete()
        try:
            await cq.message.edit_text(format_hero_card(data),
                                       reply_markup=hero_card_keyboard(h["slug"], en),
                                       disable_web_page_preview=True)
        except Exception as e:  # noqa: BLE001
            if "not modified" in str(e):
                await cq.answer("Данные актуальны ✅")
            else:
                # сообщение-источник может быть фото (сетка героев) —
                # тогда просто отправляем карточку новым сообщением
                await cq.bot.send_message(cq.message.chat.id,
                                          format_hero_card(data),
                                          reply_markup=hero_card_keyboard(h["slug"], en),
                                          disable_web_page_preview=True)


@router.callback_query(F.data == "noop")
async def cb_noop(cq: CallbackQuery):
    await cq.answer()


@router.callback_query(F.data == "search")
async def cb_search(cq: CallbackQuery, state: FSMContext):
    await cq.answer()
    await state.set_state(Search.waiting)
    await cq.bot.send_message(
        cq.message.chat.id,
        "✍️ Напиши имя героя (по-русски или по-английски):\n"
        "например: <i>пудж</i>, <i>па</i>, <i>invoker</i>",
    )


@router.callback_query(F.data.startswith("page:"))
async def cb_page(cq: CallbackQuery):
    page = int(cq.data.split(":")[1])
    await cq.answer()
    await send_hero_list(cq.message.chat.id, page, cq.bot, message=cq.message)


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
    msg = await cq.bot.send_message(cq.message.chat.id, "⏳ Загружаю про-билд с D2PT...")
    try:
        data = await asyncio.to_thread(d2pt.get_builds, en)
        text = d2pt.format_position(data, pos)
    except Exception:  # noqa: BLE001
        log.exception("d2pt build failed")
        try:
            await msg.edit_text("😔 D2PT недоступен. Попробуй позже или кнопку «📖 Dotabuff Guides».")
        except Exception:  # noqa: BLE001
            pass
        return
    try:
        await msg.edit_text(text, disable_web_page_preview=True)
    except Exception:  # noqa: BLE001
        await cq.bot.send_message(cq.message.chat.id, text, disable_web_page_preview=True)

    # иконки предметов компактной картинкой (старт / кор с таймингами / финал)
    if pos != "all":
        try:
            grid_path = str(Path(__file__).with_name(
                f"_items_{cq.message.chat.id}_{abs(hash(en)) % 100000}_{pos}.png"))
            grid = await asyncio.to_thread(items_img.build_items_image,
                                           data, pos, grid_path)
            if grid:
                await cq.bot.send_photo(
                    cq.message.chat.id, FSInputFile(grid),
                    caption=f"🛠 Предметы · {en} · {d2pt.POS_TITLES.get(pos, pos)}")
        except Exception:  # noqa: BLE001
            log.exception("items image failed")


@router.callback_query(F.data.startswith("dbguide:"))
async def cb_dbguide(cq: CallbackQuery):
    slug = cq.data.split(":", 1)[1]
    await cq.answer()
    msg = await cq.bot.send_message(cq.message.chat.id, "⏳ Загружаю гайды с dotabuff...")
    try:
        guides = await asyncio.to_thread(dotabuff.get_guides, slug)
        hero = {"en": slug.replace("-", " ").title(), "slug": slug}
        selected = guides[:3]
        await msg.edit_text(format_guides(hero, "all", selected),
                            disable_web_page_preview=True)
    except Exception:  # noqa: BLE001
        log.exception("guides failed")
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


async def main():
    if not TOKEN:
        print("Не найден токен бота!")
        print("Укажи переменную окружения BOT_TOKEN или создай файл token.txt с токеном.")
        return

    _apply_tg_dns_override()
    # прогрев кеша списка героев в фоне
    asyncio.create_task(fetch_heroes())

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
