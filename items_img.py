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
PORTRAIT_CDN = ("https://cdn.cloudflare.steamstatic.com/apps/dota2/images/"
                "heroes/{n}_vert.jpg")

_HERE = Path(__file__).parent
ITEMS_DIR = _HERE / "icons" / "items"
HEROES_DIR = _HERE / "icons" / "heroes"
PORTRAITS_DIR = _HERE / "icons" / "portraits"
HERO_MAP_FILE = _HERE / "heroes_icons.json"
ITEMS_MAP_FILE = _HERE / "icons" / "items_map.json"

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


_item_map = None


def _item_name_map() -> dict:
    """{display name: internal name}: «Blink Dagger» -> blink.

    Нужен, потому что dotabuff отдаёт только название, а CDN Valve ждёт
    внутреннее имя. Словарь тянем из OpenDota один раз и кешируем на диск.
    """
    global _item_map
    if _item_map is not None:
        return _item_map
    try:
        _item_map = json.loads(ITEMS_MAP_FILE.read_text(encoding="utf-8"))
        if _item_map:
            return _item_map
    except Exception:  # noqa: BLE001
        _item_map = {}
    try:
        r = crequests.get("https://api.opendota.com/api/constants/items",
                          impersonate="chrome", timeout=25)
        if r.status_code == 200:
            raw = r.json()
            mapping = {}
            for internal, info in (raw or {}).items():
                if isinstance(info, dict) and info.get("dname"):
                    mapping.setdefault(info["dname"], internal)
            if mapping:
                _item_map = mapping
                try:
                    ITEMS_MAP_FILE.parent.mkdir(parents=True, exist_ok=True)
                    ITEMS_MAP_FILE.write_text(
                        json.dumps(mapping, ensure_ascii=False),
                        encoding="utf-8")
                except Exception:  # noqa: BLE001
                    pass
    except Exception:  # noqa: BLE001
        pass
    return _item_map or {}


# ---------------------------------------------------------------------------
# предметы
# ---------------------------------------------------------------------------
def get_item_icon(internal: str, display: str = ""):
    """Image (RGBA) иконки предмета или None. Кеш на диске."""
    if not internal and not display:
        return None
    # иногда d2pt/dotabuff отдают display name вместо внутреннего имени —
    # тогда подстраховываемся слагом от display и словарём предметов
    candidates = []
    if internal:
        candidates.append(internal)
    if display:
        alias = _item_name_map().get(display)
        if alias and alias not in candidates:
            candidates.append(alias)
        slug = _slug(display)
        if slug not in candidates:
            candidates.append(slug)
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
    "Outworld Devourer": "obsidian_destroyer",
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


def _hero_candidates(en_name: str) -> list:
    """Возможные внутренние имена Valve: маппинг d2pt, оверрайды, слаг."""
    mapping = _hero_name_map()
    out = []
    if en_name in mapping:
        out.append(mapping[en_name])
    if en_name in _HERO_OVERRIDES:
        out.append(_HERO_OVERRIDES[en_name])
    guess = _slug(en_name)
    if guess not in out:
        out.append(guess)
    return out


def get_hero_icon(en_name: str):
    """Image (RGBA) портрета героя или None. Кеш на диске."""
    HEROES_DIR.mkdir(parents=True, exist_ok=True)
    for internal in _hero_candidates(en_name):
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


def get_hero_portrait(en_name: str, internal: str = ""):
    """Вертикальный портрет героя (235x272) для постера. Кеш на диске."""
    PORTRAITS_DIR.mkdir(parents=True, exist_ok=True)
    candidates = []
    if internal:
        candidates.append(internal)
    candidates += [c for c in _hero_candidates(en_name) if c not in candidates]
    for name in candidates:
        f = PORTRAITS_DIR / f"{name}_vert.jpg"
        if f.exists():
            try:
                return Image.open(f).convert("RGBA")
            except Exception:  # noqa: BLE001
                continue
        try:
            data = _download(PORTRAIT_CDN.format(n=name))
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


# ---------------------------------------------------------------------------
# карточка-постер героя для шаринга (1080 × 1350 — формат сторис)
# ---------------------------------------------------------------------------
POSTER_W, POSTER_H = 1080, 1350
POSTER_BG = (10, 13, 21)
GOLD = (216, 180, 90)
GOLD_DIM = (154, 128, 64)


def _cover(im, size):
    """Масштабирует картинку «по размеру» и обрезает по центру."""
    w, h = size
    scale = max(w / im.width, h / im.height)
    nw = max(1, int(im.width * scale + 0.5))
    nh = max(1, int(im.height * scale + 0.5))
    im = im.resize((nw, nh), Image.LANCZOS)
    x, y = (nw - w) // 2, (nh - h) // 2
    return im.crop((x, y, x + w, y + h))


def _wrap(draw, text, font, max_w, max_lines=2):
    """Разбивает текст на строки; «…» только если слова реально не влезли."""
    words = str(text or "").split()
    lines, cur = [], ""
    for word in words:
        trial = (cur + " " + word).strip()
        if not cur or draw.textlength(trial, font=font) <= max_w:
            cur = trial
            continue
        lines.append(cur)
        cur = word
        if len(lines) >= max_lines:
            break
    truncated = False
    if cur:
        if len(lines) < max_lines:
            lines.append(cur)
            cur = ""
        else:
            truncated = True
    if truncated and lines:
        last = lines[-1]
        while last and draw.textlength(last + "…", font=font) > max_w:
            last = last[:-1]
        lines[-1] = last.rstrip() + "…"
    return lines


def _fit_font(draw, text, max_w, max_size, min_size, bold=True):
    for size in range(max_size, min_size - 1, -1):
        f = _font(size, bold=bold)
        if draw.textlength(str(text), font=f) <= max_w:
            return f
    return _font(min_size, bold=bold)


def _mini_strip(canvas, draw, items, x, y, w, h=36, count=6):
    """Ряд мелких иконок предметов по ширине колонки.

    Возвращает список (x, width) для подписей под иконками и высоту ряда.
    """
    items = [it for it in (items or []) if it.get("item")][:count]
    if not items:
        return [], 0
    iw = int(h * 1.38)
    n = len(items)
    gap = int((w - n * iw) / (n - 1)) if n > 1 else 0
    gap = max(6, min(gap, 18))
    total = n * iw + (n - 1) * gap
    while total > w and iw > 24:
        iw -= 1
        total = n * iw + (n - 1) * gap
    x0 = x + max(0, (w - total) // 2)
    cells = []
    for i, it in enumerate(items):
        ix = x0 + i * (iw + gap)
        icon = get_item_icon("", display=it.get("item", ""))
        if icon is not None:
            ic = icon.resize((iw, h))
            canvas.paste(ic, (ix, y), ic)
        else:
            draw.rounded_rectangle([ix, y, ix + iw, y + h], radius=6,
                                   fill=(26, 31, 45))
        draw.rounded_rectangle([ix - 1, y - 1, ix + iw, y + h], radius=6,
                               outline=(72, 78, 94), width=2)
        cells.append((ix, iw))
    return cells, h


def _draw_full_build(canvas, draw, build: dict, x: int, y: int, w: int):
    """Правая колонка под портретом: старт, кор с таймингами, скиллы, таланты."""
    label = build.get("label") or ""
    draw.text((x, y), "ПОЛНАЯ СБОРКА", font=_font(19, bold=True), fill=GOLD)
    if label:
        f = _font(15)
        draw.text((x + w - draw.textlength(label, font=f), y + 4),
                  label, font=f, fill=(150, 158, 174))
    y += 28

    def head(text: str):
        nonlocal y
        draw.text((x, y), text, font=_font(12, bold=True), fill=GOLD_DIM)
        y += 17

    starting = [it for it in (build.get("starting") or []) if it.get("item")]
    if starting:
        head("СТАРТ")
        cells, h = _mini_strip(canvas, draw, starting, x, y, w, h=36)
        y += h + 3
        counts, order = {}, []
        for it in starting:
            name = it.get("item", "")
            if name not in counts:
                counts[name] = 1
                order.append(name)
            else:
                counts[name] += 1
        names = " · ".join(f"{name} ×{counts[name]}" if counts[name] > 1
                           else name for name in order)
        f = _font(11)
        for line in _wrap(draw, names, f, w, max_lines=2):
            draw.text((x, y), line, font=f, fill=(148, 155, 170))
            y += 13
        y += 5

    core = [it for it in (build.get("core") or []) if it.get("item")]
    if core:
        head("КОР · ТАЙМИНГИ")
        cells, h = _mini_strip(canvas, draw, core, x, y, w, h=36)
        f = _font(12, bold=True)
        for (ix, iw), it in zip(cells, core):
            minute = it.get("minute")
            if not minute:
                continue
            t = f"{int(minute)}м"
            draw.text((ix + (iw - draw.textlength(t, font=f)) / 2,
                       y + h + 2), t, font=f, fill=GOLD)
        y += h + 18

    abilities = [a for a in (build.get("abilities") or []) if a]
    if abilities:
        head("ПРОКАЧКА")
        f = _font(12)
        text = " → ".join(abilities[:10])
        if len(abilities) > 10:
            text += " → …"
        for line in _wrap(draw, text, f, w, max_lines=3):
            draw.text((x, y), line, font=f, fill=(206, 212, 224))
            y += 15
        y += 5

    talents = build.get("talents") or []
    if talents:
        head("ТАЛАНТЫ")
        f = _font(12)
        for t in talents[:4]:
            line = f"{t.get('level')} · {t.get('name') or ''}"
            while line and draw.textlength(line, font=f) > w:
                line = line[:-1]
            draw.text((x, y), line, font=f, fill=(206, 212, 224))
            y += 15


def _draw_player_build(canvas, draw, sections: list, x: int, y: int, w: int):
    """Правая колонка: компактная сборка игрока по разделам, одними иконками.

    Иконки предметов в Dota имеют пропорции 88×64 — вписываем их в ячейку
    с сохранением пропорций, а не растягиваем в квадрат.
    """
    draw.text((x, y), "СБОРКА ИГРОКА", font=_font(19, bold=True), fill=GOLD)
    y += 26
    cell_w, cell_h, gap = 38, 28, 4
    cols = max(1, (w + gap) // (cell_w + gap))
    for title, names in sections:
        draw.text((x, y), title, font=_font(11, bold=True), fill=GOLD_DIM)
        y += 13
        for i, name in enumerate(names[:cols]):
            ix = x + i * (cell_w + gap)
            ico = get_item_icon(name)
            if ico is not None:
                scale = min((cell_w - 2) / ico.width, (cell_h - 2) / ico.height)
                iw, ih = max(1, int(ico.width * scale)), max(1, int(ico.height * scale))
                ico = ico.resize((iw, ih), Image.LANCZOS)
                tile = Image.new("RGBA", (cell_w, cell_h), (0, 0, 0, 0))
                tile.alpha_composite(
                    ico, ((cell_w - iw) // 2, (cell_h - ih) // 2))
                canvas.paste(tile, (ix, y), tile)
            draw.rounded_rectangle([ix - 1, y - 1, ix + cell_w, y + cell_h],
                                   radius=5, outline=(70, 76, 92), width=1)
        y += cell_h + 6
    return y


def build_meta_poster(meta: dict, out_path: str, subtitle: str = "") -> str:
    """Постер меты: топ-10 по винрейту и популярности с портретами героев."""
    W, H = POSTER_W, POSTER_H
    m = 56

    # фон: вертикальный градиент + тёплое пятно сверху справа
    base = Image.new("RGB", (W, H), POSTER_BG)
    gd = ImageDraw.Draw(base)
    top_c, bot_c = (17, 22, 36), (7, 9, 15)
    for y in range(H):
        t = y / (H - 1)
        gd.line([(0, y), (W, y)],
                fill=tuple(int(top_c[i] + (bot_c[i] - top_c[i]) * t)
                           for i in range(3)))
    glow = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    ImageDraw.Draw(glow).ellipse([W - 640, -340, W + 240, 440],
                                 fill=(170, 122, 42, 48))
    canvas = Image.alpha_composite(base.convert("RGBA"),
                                   glow.filter(ImageFilter.GaussianBlur(150)))
    draw = ImageDraw.Draw(canvas)

    # шапка
    draw.rectangle([0, 0, W, 5], fill=GOLD)
    draw.text((m, 54), "DOTA 2 · МЕТА ПАТЧА", font=_font(23, bold=True),
              fill=GOLD)
    if subtitle:
        f_sub = _font(23, bold=True)
        tw = draw.textlength(subtitle, font=f_sub)
        draw.text((W - m - tw, 54), subtitle, font=f_sub, fill=(190, 196, 210))
    draw.line([m, 102, W - m, 102], fill=(70, 74, 88), width=2)

    # ранг и выборка
    name = meta.get("bracket_name") or "Все ранги"
    mmr = meta.get("mmr") or ""
    matches = meta.get("matches") or 0
    f_name = _fit_font(draw, name, W - 2 * m - 280, 66, 34)
    draw.text((m, 140), name, font=f_name, fill=(238, 240, 246))

    line2 = f"{_compact(matches)} матчей"
    if mmr:
        line2 += f" · ≈{mmr} MMR"
    line2 += " · публичные матчи · OpenDota"
    draw.text((m, 224), line2, font=_font(21), fill=(150, 158, 174))

    heroes_n = meta.get("heroes") or 0
    if heroes_n:
        extra = f"{heroes_n} героев в выборке"
        f_x = _font(19)
        draw.text((W - m - draw.textlength(extra, font=f_x), 178), extra,
                  font=f_x, fill=(125, 132, 148))

    draw.line([m, 274, W - m, 274], fill=(52, 57, 72), width=2)

    col_w = (W - 2 * m - 30) // 2
    left_x, right_x = m, m + col_w + 30
    top = 302
    row_h = 92

    def section(x: int, title: str, note: str, rows: list, mode: str):
        draw.text((x, top), title, font=_font(22, bold=True), fill=GOLD)
        f_n = _font(17)
        draw.text((x + col_w - draw.textlength(note, font=f_n), top + 5),
                  note, font=f_n, fill=(125, 132, 148))
        y = top + 38
        for i, r in enumerate(rows, 1):
            if i % 2 == 1:
                draw.rounded_rectangle([x - 8, y - 2, x + col_w + 8,
                                        y + row_h - 10],
                                       radius=10, fill=(20, 24, 36))
            f_rank = _font(24 if i < 10 else 21, bold=True)
            rank = str(i)
            draw.text((x + 34 - draw.textlength(rank, font=f_rank), y + 24),
                      rank, font=f_rank, fill=GOLD_DIM)

            pw, ph = 60, 74
            px, py = x + 44, y + 2
            portrait = get_hero_portrait(r.get("hero") or "")
            if portrait:
                ic = _cover(portrait.convert("RGB"), (pw, ph)).convert("RGBA")
                mask = Image.new("L", (pw, ph), 0)
                ImageDraw.Draw(mask).rounded_rectangle(
                    [0, 0, pw - 1, ph - 1], radius=8, fill=255)
                ic.putalpha(mask)
                canvas.alpha_composite(ic, (px, py))
            else:
                draw.rounded_rectangle([px, py, px + pw, py + ph], radius=8,
                                       fill=(26, 31, 45))
                letter = (r.get("hero") or "?")[:1].upper()
                f_l = _font(30, bold=True)
                draw.text((px + pw / 2 - draw.textlength(letter, font=f_l) / 2,
                           py + ph / 2 - 18), letter, font=f_l, fill=GOLD)
            draw.rounded_rectangle([px, py, px + pw, py + ph], radius=8,
                                   outline=(66, 72, 88), width=2)

            tx = px + pw + 16
            hero = r.get("hero") or ""
            f_hero = _font(26, bold=True)
            avail = col_w - (tx - x) - 8
            while hero and draw.textlength(hero, font=f_hero) > avail:
                hero = hero[:-1]
            if hero != (r.get("hero") or ""):
                hero = hero.rstrip() + "…"
            draw.text((tx, y + 12), hero, font=f_hero, fill=(232, 235, 242))

            if mode == "win":
                stat = f"{r['winrate']:.1f}% · {r['pickrate']:.1f}% пиков"
            else:
                stat = f"{r['pickrate']:.1f}% пиков · {r['winrate']:.1f}%"
            color = (232, 200, 120) if r.get("winrate", 0) >= 52.0 \
                else (150, 158, 174)
            draw.text((tx, y + 48), stat, font=_font(18), fill=color)
            y += row_h

    section(left_x, "ТОП ПО ВИНРЕЙТУ", "винрейт · пики",
            meta.get("by_winrate") or [], "win")
    section(right_x, "ТОП ПО ПОПУЛЯРНОСТИ", "пики · винрейт",
            meta.get("by_pick") or [], "pick")

    # подвал
    draw.line([m, 1282, W - m, 1282], fill=(60, 64, 78), width=2)
    draw.text((m, 1302), "@Mirsborka_bot", font=_font(30, bold=True), fill=GOLD)
    f_tag = _font(21)
    tag = "мета · сборки · разбор матчей"
    tw = draw.textlength(tag, font=f_tag)
    draw.text((W - m - tw, 1308), tag, font=f_tag, fill=(150, 158, 174))

    canvas.convert("RGB").save(out_path, "PNG", optimize=True)
    return out_path


def _compact(v) -> str:
    try:
        v = int(v)
    except (TypeError, ValueError):
        return str(v)
    if v >= 1_000_000:
        return f"{v / 1_000_000:.1f}M"
    if v >= 1_000:
        return f"{v / 1_000:.0f}k"
    return str(v)


def build_hero_poster(data: dict, out_path: str, subtitle: str = "") -> str:
    """Постер 1080×1350: герой, винрейт, матчапы, сборки и полный билд."""
    en = data.get("en") or (data.get("slug") or "").replace("-", " ").title()
    W, H = POSTER_W, POSTER_H
    m = 72

    portrait = get_hero_portrait(en)
    if portrait:
        base = _cover(portrait.convert("RGB"), (W, H))
        base = base.filter(ImageFilter.GaussianBlur(34))
    else:
        base = Image.new("RGB", (W, H), POSTER_BG)
    img = Image.alpha_composite(base.convert("RGBA"),
                                Image.new("RGBA", (W, H), (8, 10, 16, 208)))

    # затемнение снизу, чтобы текст читался на любом арте
    grad = Image.new("L", (1, H), 0)
    for y in range(H):
        t = max(0.0, (y - H * 0.42) / (H * 0.58))
        grad.putpixel((0, y), int(215 * (t ** 1.5)))
    dark = Image.new("RGBA", (W, H), (5, 7, 12, 255))
    dark.putalpha(grad.resize((W, H)))
    img = Image.alpha_composite(img, dark)
    draw = ImageDraw.Draw(img)

    # тонкая золотая линия сверху + шапка
    draw.rectangle([0, 0, W, 5], fill=GOLD)
    draw.text((m, 54), "DOTA 2 · BUILDS & META", font=_font(23, bold=True),
              fill=GOLD)
    if subtitle:
        f_sub = _font(23, bold=True)
        tw = draw.textlength(subtitle, font=f_sub)
        draw.text((W - m - tw, 54), subtitle, font=f_sub, fill=(190, 196, 210))
    draw.line([m, 102, W - m, 102], fill=(70, 74, 88), width=2)

    # портрет справа
    pw, ph = 400, 462
    px, py = W - m - pw, 136
    if portrait:
        card = portrait.convert("RGB").resize((pw, ph), Image.LANCZOS)
        card = card.convert("RGBA")
        mask = Image.new("L", (pw, ph), 0)
        ImageDraw.Draw(mask).rounded_rectangle([0, 0, pw - 1, ph - 1],
                                              radius=26, fill=255)
        card.putalpha(mask)
        glow = Image.new("RGBA", (pw + 48, ph + 48), (0, 0, 0, 0))
        ImageDraw.Draw(glow).rounded_rectangle([24, 24, pw + 23, ph + 23],
                                               radius=28, fill=(0, 0, 0, 170))
        img.alpha_composite(glow.filter(ImageFilter.GaussianBlur(16)),
                            (px - 24, py - 16))
        img.alpha_composite(card, (px, py))
        draw.rounded_rectangle([px, py, px + pw, py + ph], radius=26,
                               outline=GOLD, width=3)

    left_w = (px - 28 - m) if portrait else (W - 2 * m)

    # имя
    name = data.get("en") or "Dota 2"
    f_name = _fit_font(draw, name, left_w, 88, 46)
    name_lines = _wrap(draw, name, f_name, left_w, max_lines=2)
    y = 150
    lh = int(getattr(f_name, "size", 64) * 1.12)
    for line in name_lines:
        draw.text((m, y), line, font=f_name, fill=(238, 240, 246))
        y += lh

    # роли — подбираем размер шрифта, чтобы влезли в две строки без обрезки
    attrs = data.get("attrs") or []
    if attrs:
        roles = " · ".join(str(a) for a in attrs)
        f_roles = _font(24)
        for size in range(26, 17, -1):
            f_roles = _font(size)
            lines = _wrap(draw, roles, f_roles, left_w, max_lines=2)
            if lines and not lines[-1].endswith("…"):
                break
        for line in lines:
            y += 4
            draw.text((m, y), line, font=f_roles, fill=(168, 176, 192))
            y += int(size * 1.28)

    # винрейт
    winrate = data.get("winrate")
    wy = max(410, y + 26)
    if winrate:
        draw.text((m, wy), "ВИНРЕЙТ", font=_font(21, bold=True), fill=GOLD_DIM)
        f_wr = _font(92, bold=True)
        draw.text((m, wy + 30), str(winrate), font=f_wr, fill=(246, 248, 252))
        draw.text((m, wy + 132), "публичные матчи за неделю", font=_font(21),
                  fill=(150, 158, 174))

    # матчапы
    y2 = wy + 175

    def matchup(title, rows, sign):
        nonlocal y2
        if not rows:
            return
        draw.text((m, y2), title, font=_font(21, bold=True), fill=GOLD_DIM)
        y2 += 34
        for r in rows[:2]:
            hero = r.get("hero") or ""
            adv = str(r.get("adv") or "").replace("+", "").strip()
            line = f"{hero}  {sign}{adv}"
            f = _font(31, bold=True)
            while line and draw.textlength(line, font=f) > left_w:
                line = line[:-1]
            draw.text((m, y2), line, font=f, fill=(226, 230, 238))
            y2 += 40

    matchup("СИЛЬНЕЕ ВСЕГО ПРОТИВ", data.get("best_vs"), "+")
    y2 += 26
    matchup("СЛАБЕЕ ВСЕГО ПРОТИВ", data.get("worst_vs"), "−")

    # топ-3 сборки по позициям (D2PT) с винрейтом
    builds = data.get("top_builds") or []
    if builds:
        y2 += 8
        src = "D2PT"
        draw.text((m, y2), "ТОП СБОРКИ", font=_font(21, bold=True),
                  fill=GOLD_DIM)
        f_lbl = _font(17)
        lbl = f"винрейт · {src}"
        draw.text((m + left_w - draw.textlength(lbl, font=f_lbl), y2 + 4),
                  lbl, font=f_lbl, fill=(130, 136, 152))
        y2 += 30
        for b in builds[:3]:
            title = b.get("short") or b.get("title") or f"Pos {b.get('pos')}"
            try:
                wr = f"{float(b.get('winrate') or 0):.1f}%"
            except (TypeError, ValueError):
                wr = ""
            f_b = _font(27, bold=True)
            avail = left_w - (draw.textlength(wr, font=f_b) + 18 if wr else 0)
            while title and draw.textlength(title, font=f_b) > avail:
                title = title[:-1]
            draw.text((m, y2), title, font=f_b, fill=(226, 230, 238))
            if wr:
                f_w = _font(27, bold=True)
                draw.text((m + left_w - draw.textlength(wr, font=f_w), y2),
                          wr, font=f_w, fill=GOLD)
            y2 += 36

    # правая колонка под портретом: сборка игрока (если есть), иначе D2PT
    full = data.get("top_build") or {}
    player_build = data.get("player_build") or []
    if portrait and player_build:
        _draw_player_build(img, draw, player_build, px, py + ph + 22, pw)
    elif portrait and any(full.get(k) for k in
                          ("starting", "core", "abilities", "talents")):
        _draw_full_build(img, draw, full, px, py + ph + 22, pw)

    # предметы: сборка топ-1 по винрейту (D2PT), иначе — топ предметов недели
    build = data.get("top_build") or {}
    six = [x for x in (build.get("sixslot") or []) if x.get("item")][:6]
    if not six:
        six = [x for x in (build.get("core") or []) if x.get("item")][:6]

    if six:
        iy = max(958, y2 + 22)
        heading = "ФИНАЛ"
        if build.get("label"):
            heading += f" · {build['label']}"
        draw.text((m, iy), heading, font=_font(24, bold=True), fill=GOLD)

        iw, ih, gap = 134, 97, 18
        ty = iy + 42
        for i, it in enumerate(six):
            ix = m + i * (iw + gap)
            icon = get_item_icon("", display=it.get("item", ""))
            if icon is not None:
                ic = icon.resize((iw, ih))
                img.paste(ic, (ix, ty), ic)
            draw.rounded_rectangle([ix - 1, ty - 1, ix + iw, ty + ih],
                                   radius=8, outline=(72, 78, 94), width=2)
            f_in = _font(15, bold=True)
            ny = ty + ih + 7
            for line in _wrap(draw, it.get("item", ""), f_in, iw + 10,
                              max_lines=2):
                draw.text((ix, ny), line, font=f_in, fill=(206, 212, 224))
                ny += 18
    else:
        items = (data.get("items") or [])[:3]
        if items:
            iy = max(958, y2 + 22)
            draw.text((m, iy), "ТОП ПРЕДМЕТЫ", font=_font(24, bold=True),
                      fill=GOLD)
            iw, ih = 150, 109
            step = 200
            for i, it in enumerate(items):
                ix = m + i * step
                iyy = iy + 44
                icon = get_item_icon("", display=it.get("name", ""))
                if icon is not None:
                    ic = icon.resize((iw, ih))
                    img.paste(ic, (ix, iyy), ic)
                draw.rounded_rectangle([ix - 1, iyy - 1, ix + iw, iyy + ih],
                                       radius=8, outline=(72, 78, 94), width=2)
                f_in = _font(20, bold=True)
                ty = iyy + ih + 10
                for line in _wrap(draw, it.get("name", ""), f_in, iw + 24,
                                  max_lines=2):
                    draw.text((ix, ty), line, font=f_in, fill=(206, 212, 224))
                    ty += 25
                wr = it.get("winrate")
                if wr:
                    draw.text((ix, ty + 2), str(wr), font=_font(18),
                              fill=(140, 148, 164))

    # подвал
    draw.line([m, 1218, W - m, 1218], fill=(60, 64, 78), width=2)
    draw.text((m, 1244), "@Mirsborka_bot", font=_font(30, bold=True), fill=GOLD)
    f_tag = _font(21)
    tag = "сборки · мета · разбор матчей"
    tw = draw.textlength(tag, font=f_tag)
    draw.text((W - m - tw, 1250), tag, font=f_tag, fill=(150, 158, 174))

    img.convert("RGB").save(out_path, "PNG", optimize=True)
    return out_path
