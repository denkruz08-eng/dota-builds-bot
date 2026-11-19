# -*- coding: utf-8 -*-
"""
Иконки предметов и портреты героев + сборка компактных картинок-сеток.

Источники:
  • предметы — официальный CDN Valve (cdn.cloudflare.steamstatic.com),
    путь /apps/dota2/images/dota_react/items/<internal>.png
  • герои — dota2protracker.com/static/heroes/<internal>_lg.jpg
    (внутренние имена Valve: zuus, nevermore, furion, ...)

Маппинг "EN имя героя -> внутреннее имя" берётся с главной d2pt
(там рядом портрет и alt-имя) и кешируется на диск.
"""
import io
import json
import os
import time
from pathlib import Path

from curl_cffi import requests as crequests
from PIL import Image, ImageDraw, ImageFilter, ImageFont

import d2pt

ITEM_CDN = ("https://cdn.cloudflare.steamstatic.com/apps/dota2/images/"
            "dota_react/items/{n}.png")
HERO_CDN = d2pt.BASE + "/static/heroes/{n}_lg.jpg"

_HERE = Path(__file__).parent
ITEMS_DIR = _HERE / "icons" / "items"
HEROES_DIR = _HERE / "icons" / "heroes"
HERO_MAP_FILE = _HERE / "heroes_icons.json"

# прокси как запасной вариант скачивания (как в боте)
_PX = None
_p = (os.environ.get("BOT_PROXY") or "").strip()
if not _p:
    try:
        _p = (_HERE / "proxy.txt").read_text(encoding="utf-8").strip()
    except OSError:
        _p = ""
if _p:
    _PX = {"http": _p, "https": _p}

_col = None


def _c(color):
    return tuple(color)


_FONT_DIRS = (str(Path(__file__).with_name("assets").joinpath("fonts")) + "/",
              "", "C:/Windows/Fonts/", "/usr/share/fonts/truetype/dejavu/",
              "/usr/share/fonts/truetype/liberation/",
              "/usr/share/fonts/truetype/msttcorefonts/")
_FONT_NAMES = ("arial.ttf", "DejaVuSans.ttf", "LiberationSans-Regular.ttf")
_FONT_NAMES_BOLD = ("arialbd.ttf", "DejaVuSans-Bold.ttf",
                    "LiberationSans-Bold.ttf")


def _ensure_fonts() -> Path:
    """Шрифты для картинок. Если их нет рядом с ботом (например, на сервере) —
    скачиваем один раз и кешируем, чтобы не зависеть от системных шрифтов."""
    d = _HERE / "assets" / "fonts"
    if getattr(_ensure_fonts, "_done", False):
        return d
    _ensure_fonts._done = True
    try:
        d.mkdir(parents=True, exist_ok=True)
        for name in ("DejaVuSans.ttf", "DejaVuSans-Bold.ttf"):
            p = d / name
            if p.exists() and p.stat().st_size > 100_000:
                continue
            data = _download("https://cdn.jsdelivr.net/npm/dejavu-fonts-ttf@2.37.3/ttf/" + name)
            if data:
                p.write_bytes(data)
    except Exception:  # noqa: BLE001
        pass
    return d


def _font(size: int, bold: bool = False):
    """Сначала шрифт из assets/fonts (одинаково на ПК и сервере), потом системный."""
    bundled = _ensure_fonts()
    candidates = [bundled / ("DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf")]
    for name in (_FONT_NAMES_BOLD if bold else _FONT_NAMES):
        for d in _FONT_DIRS:
            candidates.append(Path(f"{d}{name}"))
    for p in candidates:
        try:
            return ImageFont.truetype(str(p), size)
        except OSError:
            continue
    return ImageFont.load_default()


def _download(url: str, tries: int = 2):
    last = None
    for proxies in ([None, _PX] if _PX else [None]):
        try:
            r = crequests.get(url, impersonate="chrome", timeout=20,
                              proxies=proxies)
            if r.status_code == 200:
                return r.content
            last = RuntimeError(f"HTTP {r.status_code}: {url}")
            if r.status_code == 404:
                return None
        except Exception as e:  # noqa: BLE001
            last = e
    raise last if last else RuntimeError(url)


def _slug(s: str) -> str:
    import re
    s = s.lower().replace("'", "").replace("-", "_").replace(" ", "_")
    return re.sub(r"[^a-z0-9_]", "", s)


# ---------------------------------------------------------------------------
# предметы
# ---------------------------------------------------------------------------
def get_item_icon(internal: str, display: str = ""):
    """Image (RGBA) иконки предмета или None. Кеш на диске."""
    if not internal and not display:
        return None
    # иногда d2pt отдаёт display name вместо внутреннего имени —
    # тогда подстраховываемся слагом от display
    candidates = []
    if internal:
        candidates.append(internal)
    if display and _slug(display) not in candidates:
        candidates.append(_slug(display))
    for c in candidates:
        if not c:
            continue
        ITEMS_DIR.mkdir(parents=True, exist_ok=True)
        f = ITEMS_DIR / f"{c}.png"
        if f.exists():
            try:
                return Image.open(f).convert("RGBA")
            except Exception:  # noqa: BLE001
                pass
        try:
            data = _download(ITEM_CDN.format(n=c))
        except Exception:  # noqa: BLE001
            continue
        if not data:
            continue
        try:
            f.write_bytes(data)
            return Image.open(io.BytesIO(data)).convert("RGBA")
        except Exception:  # noqa: BLE001
            continue
    return None


# ---------------------------------------------------------------------------
# герои: маппинг EN -> внутреннее имя Valve + портреты
# ---------------------------------------------------------------------------
def _hero_name_map() -> dict:
    """{display EN name: internal name}. Кеш: память -> диск -> главная d2pt."""
    global _col
    if _col is not None:
        return _col
    try:
        _col = json.loads(HERO_MAP_FILE.read_text(encoding="utf-8"))
        if _col:
            return _col
    except Exception:  # noqa: BLE001
        pass
    mapping = {}
    try:
        html_text = _download(d2pt.BASE + "/", tries=2)
        if html_text:
            text = html_text.decode("utf-8", "replace") if isinstance(
                html_text, bytes) else html_text
            import re
            for m in re.finditer(
                    r'/static/heroes/([a-z0-9_]+)_lg\.jpg[^>]*alt="([^"]+)"',
                    text):
                internal, display = m.group(1), m.group(2)
                mapping.setdefault(display, internal)
            # обратный порядок: alt стоит перед src в некоторых шаблонах
            for m in re.finditer(
                    r'alt="([^"]+)"[^>]*src="[^"]*/static/heroes/'
                    r'([a-z0-9_]+)_lg\.jpg', text):
                mapping.setdefault(m.group(1), m.group(2))
    except Exception:  # noqa: BLE001
        pass
    if mapping:
        _col = mapping
        try:
            HERO_MAP_FILE.write_text(
                json.dumps(mapping, ensure_ascii=False), encoding="utf-8")
        except Exception:  # noqa: BLE001
            pass
    return _col or {}


# герои, у которых внутреннее имя Valve отличается от отображаемого
_HERO_OVERRIDES = {
    "Anti-Mage": "antimage",
    "Centaur Warrunner": "centaur",
    "Clockwerk": "rattletrap",
    "Doom": "doom_bringer",
    "Io": "wisp",
    "Magnus": "magnataur",
    "Nature's Prophet": "furion",
    "Necrophos": "necrolyte",
    "Outworld Destroyer": "obsidian_destroyer",
    "Queen of Pain": "queenofpain",
    "Shadow Fiend": "nevermore",
    "Timbersaw": "shredder",
    "Treant Protector": "treant",
    "Underlord": "abyssal_underlord",
    "Vengeful Spirit": "vengefulspirit",
    "Windranger": "windrunner",
    "Wraith King": "skeleton_king",
    "Zeus": "zuus",
    "Lifestealer": "life_stealer",
    "Terrorblade": "terrorblade",
    "Sand King": "sand_king",
    "Storm Spirit": "storm_spirit",
    "Faceless Void": "faceless_void",
    "Monkey King": "monkey_king",
    "Ringmaster": "ringmaster",
}


def get_hero_icon(en_name: str):
    """Image (RGBA) портрета героя или None. Кеш на диске."""
    HEROES_DIR.mkdir(parents=True, exist_ok=True)
    mapping = _hero_name_map()
    candidates = []
    if en_name in mapping:
        candidates.append(mapping[en_name])
    if en_name in _HERO_OVERRIDES:
        candidates.append(_HERO_OVERRIDES[en_name])
    guess = _slug(en_name)
    if guess not in candidates:
        candidates.append(guess)
    for internal in candidates:
        f = HEROES_DIR / f"{internal}_lg.jpg"
        if f.exists():
            try:
                return Image.open(f).convert("RGBA")
            except Exception:  # noqa: BLE001
                continue
        try:
            data = _download(HERO_CDN.format(n=internal))
        except Exception:  # noqa: BLE001
            continue
        if not data:
            continue
        try:
            f.write_bytes(data)
            return Image.open(io.BytesIO(data)).convert("RGBA")
        except Exception:  # noqa: BLE001
            continue
    return None


# ---------------------------------------------------------------------------
# сборка картинок
# ---------------------------------------------------------------------------
BG = (17, 21, 30)
FG = (226, 230, 238)
DIM = (150, 158, 172)
ACCENT = (120, 190, 255)

ICON_W, ICON_H = 72, 52      # предметные иконки 88x64 -> слегка уменьшаем
PAD = 6
PER_ROW = 6
CELL_W = ICON_W + PAD * 2


def _icon_at(draw, img, x, y, icon):
    if icon is None:
        return
    ic = icon.resize((ICON_W, ICON_H)) if icon.size != (ICON_W, ICON_H) else icon
    img.paste(ic, (x + PAD, y + PAD), ic)


def _center_text(draw, cx, y, text, font, fill):
    w = draw.textlength(text, font=font)
    draw.text((cx - w / 2, y), text, font=font, fill=fill)


def build_items_image(data: dict, pos, out_path: str):
    """Сетка иконок предметов по позициям. Возвращает путь или None."""
    po = data["positions"].get(pos)
    if not po:
        return None
    sections = []
    if po.get("starting"):
        sections.append(("Старт",
                         [(s["item"], s.get("id", ""), None) for s in po["starting"]]))
    if po.get("core"):
        sections.append(("Кор (тайминги)",
                         [(c["item"], c.get("id", ""), c.get("minute"))
                          for c in po["core"]]))
    if po.get("sixslot"):
        sections.append(("Финал",
                         [(s["item"], s.get("id", ""), None) for s in po["sixslot"]]))
    if not sections:
        return None

    f_title = _font(20, bold=True)
    f_min = _font(13)

    # считаем высоту
    rows_total = 0
    for _, items in sections:
        rows_total += (len(items) + PER_ROW - 1) // PER_ROW
    title_h = 30
    label_h = 16
    row_h = ICON_H + PAD * 2 + label_h
    W = PER_ROW * CELL_W + PAD * 2
    H = PAD + len(sections) * title_h + rows_total * row_h + PAD

    img = Image.new("RGB", (W, H), BG)
    draw = ImageDraw.Draw(img)

    y = PAD
    icons_cache = {}
    for title, items in sections:
        _center_text(draw, W / 2, y, title, f_title, ACCENT)
        y += title_h
        for i, (display, internal, minute) in enumerate(items):
            row, colidx = divmod(i, PER_ROW)
            cx = PAD + colidx * CELL_W
            cy = y + row * row_h
            if internal not in icons_cache or icons_cache[internal] is None:
                icons_cache[internal] = get_item_icon(internal, display=display)
            _icon_at(draw, img, cx, cy, icons_cache[internal])
            if minute is not None:
                _center_text(draw, cx + CELL_W / 2,
                             cy + ICON_H + PAD + 2, f"{minute}м", f_min, DIM)
        y += ((len(items) + PER_ROW - 1) // PER_ROW) * row_h

    img.save(out_path, "PNG")
    return out_path


def _fit_name(draw, name: str, max_w: float, max_size: int = 16,
              min_size: int = 11, max_lines: int = 2):
    """Подбирает размер шрифта и разбивает имя на строки, чтобы оно влезло."""
    for size in range(max_size, min_size - 1, -1):
        f = _font(size)
        if draw.textlength(name, font=f) <= max_w:
            return [name], f
    f = _font(min_size)
    words = name.split()
    lines, cur = [], ""
    for w in words:
        trial = f"{cur} {w}".strip()
        if not cur or draw.textlength(trial, font=f) <= max_w:
            cur = trial
        else:
            lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines[:max_lines], f


def build_heroes_grid(heroes: list[dict], out_path: str,
                      per_row: int = 4, icon_w: int = 160, icon_h: int = 90):
    """Сетка портретов героев с именами под ними. Возвращает путь или None."""
    heroes = [h for h in heroes if h.get("en")]
    if not heroes:
        return None
    PAD2 = 10
    GAP = 14
    LABEL_H = 40
    cell_w = icon_w + GAP
    cell_h = icon_h + PAD2 + LABEL_H
    rows = (len(heroes) + per_row - 1) // per_row
    W = per_row * cell_w + GAP
    H = GAP + rows * cell_h + GAP

    img = Image.new("RGB", (W, H), BG)
    draw = ImageDraw.Draw(img)
    for i, h in enumerate(heroes):
        row, colidx = divmod(i, per_row)
        x = GAP + colidx * cell_w
        y = GAP + row * cell_h
        icon = get_hero_icon(h["en"])
        if icon is not None:
            ic = icon.resize((icon_w, icon_h))
            img.paste(ic, (x, y), ic)
        else:
            draw.rectangle([x, y, x + icon_w, y + icon_h], fill=(30, 36, 48))

        lines, f_name = _fit_name(draw, h["en"], icon_w - 4)
        line_h = int(getattr(f_name, "size", 12) * 1.25)
        ty = y + icon_h + PAD2 + max(0, (LABEL_H - line_h * len(lines)) // 2)
        for ln in lines:
            _center_text(draw, x + icon_w / 2, ty, ln, f_name, FG)
            ty += line_h

    img.save(out_path, "PNG")
    return out_path


def _maple(draw, cx, cy, r, fill, angle=0):
    """Кленовый силуэт: 5 округлых лопастей + черешок."""
    import math
    for ang_deg, k in ((-90, 1.0), (-140, 0.8), (-40, 0.8), (-175, 0.62), (-5, 0.62)):
        a = math.radians(ang_deg + angle)
        lx = cx + math.cos(a) * r * 0.50
        ly = cy + math.sin(a) * r * 0.50
        rr = r * 0.40 * k
        draw.ellipse([lx - rr, ly - rr, lx + rr, ly + rr], fill=fill)
    a = math.radians(angle)
    draw.line([(cx, cy + r * 0.15),
               (cx + math.sin(a) * r * 0.95, cy + math.cos(a) * r * 0.95)],
              fill=fill, width=max(1, int(r * 0.13)))


def build_start_banner(out_path: str, heroes: list[dict] | None = None):
    """Баннер /start в стиле аватарки канала: тёмно-красный, тории, листья."""
    import math
    import random

    W, H = 1280, 640
    rnd = random.Random(7)

    # --- фон: чёрно-багровый градиент с красным свечением ---
    base = Image.new("RGB", (W, H))
    px = base.load()
    gx, gy = W * 0.62, H * 0.48
    maxd = math.hypot(W, H) * 0.55
    for y in range(H):
        for x in range(0, W, 2):
            d = math.hypot(x - gx, y - gy) / maxd
            glow = max(0.0, 1.0 - d) ** 2
            r = int(22 + 150 * glow + 12 * (y / H))
            g = int(3 + 18 * glow + 4 * (y / H))
            b = int(5 + 10 * glow + 6 * (y / H))
            px[x, y] = (min(r, 255), min(g, 255), min(b, 255))
            if x + 1 < W:
                px[x + 1, y] = px[x, y]

    base = base.convert("RGBA")

    # --- силуэт тории справа ---
    tori = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    td = ImageDraw.Draw(tori)
    gate = (8, 3, 4, 235)
    # столбы
    td.polygon([(1000, 170), (1030, 170), (1042, H), (1006, H)], fill=gate)
    td.polygon([(1170, 170), (1200, 170), (1206, H), (1172, H)], fill=gate)
    # верхняя перекладина (слегка выгнутая)
    td.polygon([(955, 150), (1245, 150), (1245, 178), (955, 178)], fill=gate)
    td.polygon([(972, 118), (1228, 118), (1232, 150), (968, 150)], fill=gate)
    # вторая перекладина
    td.polygon([(988, 212), (1212, 212), (1212, 234), (988, 234)], fill=gate)
    base = Image.alpha_composite(base, tori)

    # --- красные кленовые листья ---
    leaves = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    ld = ImageDraw.Draw(leaves)
    zones = [(0, 0, 360, 420), (240, 0, 620, 240), (880, 0, 1280, 320),
             (0, 380, 300, 640), (980, 300, 1280, 640)]
    for zx0, zy0, zx1, zy1 in zones:
        for _ in range(18):
            x = rnd.randint(zx0, zx1)
            y = rnd.randint(zy0, zy1)
            r = rnd.randint(4, 9)
            a = rnd.randint(80, 150)
            _maple(ld, x, y, r, (190 + rnd.randint(0, 45), 28, 20, a),
                   angle=rnd.randint(-40, 40))
    # пара крупных листьев у краёв (лёгкий акцент на глубину)
    _maple(ld, 1226, 118, 34, (170, 22, 16, 55), angle=-25)
    _maple(ld, 86, 552, 32, (170, 22, 16, 50), angle=30)
    leaves = leaves.filter(ImageFilter.GaussianBlur(0.4))
    base = Image.alpha_composite(base, leaves)

    # --- угли / искры ---
    emb = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    ed = ImageDraw.Draw(emb)
    for _ in range(110):
        x, y = rnd.randint(0, W), rnd.randint(0, H)
        r = rnd.choice([1, 1, 2, 2, 3])
        ed.ellipse([x - r, y - r, x + r, y + r],
                   fill=(255, rnd.randint(90, 170), 40, rnd.randint(90, 220)))
    glow_layer = emb.filter(ImageFilter.GaussianBlur(3))
    base = Image.alpha_composite(base, glow_layer)
    base = Image.alpha_composite(base, emb)

    # --- нижнее затемнение под текст ---
    grad = Image.new("L", (1, H), 0)
    for y in range(H):
        t = max(0.0, (y - H * 0.34) / (H * 0.60))
        grad.putpixel((0, y), int(210 * min(1.0, t)))
    grad = grad.resize((W, H))
    dark = Image.new("RGBA", (W, H), (5, 1, 2, 255))
    dark.putalpha(grad)
    base = Image.alpha_composite(base, dark)

    # --- заголовок MIRDOTA: свечение + градиентная заливка ---
    f_title = _font(132, bold=True)
    title = "MIRDOTA"
    cx = W / 2

    mask = Image.new("L", (W, H), 0)
    md = ImageDraw.Draw(mask)
    tw = md.textlength(title, font=f_title)
    ty = 300
    md.text((cx - tw / 2, ty), title, font=f_title, fill=255)

    # свечение
    glow = mask.filter(ImageFilter.GaussianBlur(18))
    glow_col = Image.new("RGBA", (W, H), (255, 60, 20, 255))
    glow_col.putalpha(glow.point(lambda v: int(v * 0.85)))
    base = Image.alpha_composite(base, glow_col)
    glow2 = mask.filter(ImageFilter.GaussianBlur(6))
    glow_col2 = Image.new("RGBA", (W, H), (255, 120, 40, 255))
    glow_col2.putalpha(glow2.point(lambda v: int(v * 0.55)))
    base = Image.alpha_composite(base, glow_col2)

    # градиентная заливка букв (светло-жёлтый -> огненный красный)
    fill = Image.new("RGBA", (W, H))
    fp = fill.load()
    for y in range(ty, ty + 150):
        t = (y - ty) / 150
        col = (255, int(212 - 118 * t), int(125 - 95 * t), 255)
        for x in range(0, W, 2):
            fp[x, y] = col
            if x + 1 < W:
                fp[x + 1, y] = col
    base = Image.alpha_composite(base, Image.composite(
        fill, Image.new("RGBA", (W, H), (0, 0, 0, 0)), mask))

    draw = ImageDraw.Draw(base)

    # подпись под заголовком
    f_sub = _font(27, bold=True)
    sub = "СБОРКИ DOTA 2 · POS 1–5 · ИКОНКИ ПРЕДМЕТОВ"
    sw = draw.textlength(sub, font=f_sub)
    draw.text((cx - sw / 2, 470), sub, font=f_sub, fill=(235, 214, 208))

    draw.line([(cx - 250, 444), (cx + 250, 444)], fill=(255, 90, 45), width=2)

    base.convert("RGB").save(out_path, "PNG")
    return out_path


# ---------------------------------------------------------------------------
# итоговые сборки матча
# ---------------------------------------------------------------------------
def build_match_image(match: dict, out_path: str):
    """Итоговые сборки матча: 2 команды x 5 игроков, портрет + 6 слотов + нейтралка."""
    players = match.get("players") or []
    if not players:
        return None

    W = 666
    PAD = 12
    HEAD_H = 28
    ROW_H = 50
    PORT_W, PORT_H = 58, 33
    NAME_W = 132
    IT_W, IT_H = 40, 29
    IT_GAP = 4
    NEU_W, NEU_H = 34, 25
    ITEMS_X = PAD + PORT_W + 8 + NAME_W

    teams = [(True, "RADIANT", (120, 220, 150)),
             (False, "DIRE", (235, 110, 110))]
    H = PAD * 2 + len(teams) * HEAD_H
    for side, _, _ in teams:
        H += sum(1 for p in players if p["radiant"] == side) * ROW_H

    img = Image.new("RGB", (W, H), BG)
    draw = ImageDraw.Draw(img)

    f_head = _font(14, bold=True)
    f_kda = _font(13, bold=True)
    f_small = _font(10)

    y = PAD
    icons = {}
    for side, title, color in teams:
        team = [p for p in players if p["radiant"] == side]
        draw.rectangle([PAD, y, W - PAD, y + HEAD_H - 2], fill=(28, 34, 46))
        draw.text((PAD + 8, y + 5), title, font=f_head, fill=color)
        kills = match.get("totals", {}).get(side, {}).get("kills", 0)
        gold = match.get("totals", {}).get(side, {}).get("net", 0)
        info = f"{kills} kills · {gold // 1000}k gold"
        iw = draw.textlength(info, font=f_small)
        draw.text((W - PAD - 8 - iw, y + 9), info, font=f_small, fill=DIM)
        y += HEAD_H

        for i, p in enumerate(team):
            row_bg = (24, 29, 40) if i % 2 == 0 else (20, 24, 33)
            draw.rectangle([PAD, y, W - PAD, y + ROW_H - 2], fill=row_bg)

            icon = icons.get(p["hero"]) or get_hero_icon(p["hero"])
            icons[p["hero"]] = icon
            if icon is not None:
                ic = icon.resize((PORT_W, PORT_H))
                img.paste(ic, (PAD + 4, y + (ROW_H - PORT_H) // 2), ic)
            else:
                draw.rectangle([PAD + 4, y + (ROW_H - PORT_H) // 2,
                                PAD + 4 + PORT_W, y + (ROW_H + PORT_H) // 2],
                               fill=(34, 40, 52))

            nx = PAD + PORT_W + 8
            lines, fit_f = _fit_name(draw, p["hero"], NAME_W - 4,
                                     max_size=14, min_size=10, max_lines=2)
            line_h = int(getattr(fit_f, "size", 11) * 1.2)
            ny = y + (ROW_H - line_h * len(lines)) // 2
            for ln in lines:
                draw.text((nx, ny), ln, font=fit_f, fill=FG)
                ny += line_h

            for j, it in enumerate(p["items"][:6]):
                ix = ITEMS_X + j * (IT_W + IT_GAP)
                iy = y + (ROW_H - IT_H) // 2
                key = it["internal"]
                if key not in icons:
                    icons[key] = get_item_icon(key, display=it.get("name", ""))
                ic = icons[key]
                if ic is not None:
                    ic = ic.resize((IT_W, IT_H))
                    img.paste(ic, (ix, iy), ic)
                else:
                    draw.rounded_rectangle([ix, iy, ix + IT_W, iy + IT_H],
                                           radius=3, fill=(40, 46, 60))

            neutral = p["items"][6] if len(p["items"]) > 6 else None
            nx2 = ITEMS_X + 6 * (IT_W + IT_GAP) + 4
            iy = y + (ROW_H - NEU_H) // 2
            draw.rounded_rectangle([nx2 - 2, iy - 2, nx2 + NEU_W + 2, iy + NEU_H + 2],
                                   radius=4, outline=(70, 80, 100))
            if neutral:
                key = neutral["internal"]
                if key not in icons:
                    icons[key] = get_item_icon(key, display=neutral.get("name", ""))
                ic = icons[key]
                if ic is not None:
                    ic = ic.resize((NEU_W, NEU_H))
                    img.paste(ic, (nx2, iy), ic)

            kda = f"{p['k']}/{p['d']}/{p['a']}"
            kw = draw.textlength(kda, font=f_kda)
            draw.text((W - PAD - 10 - kw, y + 8), kda, font=f_kda, fill=FG)
            gpm = f"{p['gpm']} GPM"
            gw = draw.textlength(gpm, font=f_small)
            draw.text((W - PAD - 10 - gw, y + 27), gpm, font=f_small, fill=DIM)
            y += ROW_H

    img.save(out_path, "PNG")
    return out_path
