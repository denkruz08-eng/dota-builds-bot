# -*- coding: utf-8 -*-
"""Проверка доступности источников данных с текущего сервера.

Запускается в GitHub Actions перед ботом, чтобы в логе было видно,
какие сайты доступны, а какие блокируют IP дата-центра.
"""
import sys
import io
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")


def check(name, fn):
    try:
        result = fn()
        print(f"[OK]   {name}: {result}")
        return True
    except Exception as e:  # noqa: BLE001
        print(f"[FAIL] {name}: {type(e).__name__}: {str(e)[:160]}")
        return False


def main() -> int:
    import dotabuff
    import d2pt
    import opendota

    print("=== Проверка источников данных ===")
    results = []
    results.append(check("dotabuff /heroes",
                         lambda: f"{len(dotabuff.get_heroes())} героев"))
    results.append(check("dotabuff карточка (Tinker)",
                         lambda: dotabuff.get_hero_overview("tinker")["en"]))
    results.append(check("dotabuff гайды (Tinker)",
                         lambda: f"{len(dotabuff.get_guides('tinker'))} гайдов"))
    results.append(check("dota2protracker (Tinker)",
                         lambda: f"позиции {sorted(d2pt.get_builds('Tinker')['positions'])}"))
    results.append(check("OpenDota API",
                         lambda: opendota.get_hero_overview("tinker")["en"]))
    print("=== Итог: работает", sum(results), "из", len(results), "источников ===")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:  # noqa: BLE001
        traceback.print_exc()
        sys.exit(0)
