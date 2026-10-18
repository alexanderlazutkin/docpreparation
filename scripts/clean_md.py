#!/usr/bin/env python3
"""Очистка MD перед QC: удаляет строки с точечными лидерами оглавления
(«1.1. Процесс ......... 2», в т.ч. внутри таблиц «| 3.3 Название ..... | 54 |»
— лидеры короче 8 точек, DAMA-DMBOK) — TOC-мусор не должен попадать в индекс БЗ.
Схлопывает серии пустых строк.

Также переписывает сырые маркеры страниц marker («{N}-----…», N = 0-based
page_id, появляются при paginate_output=true, см. prepare_doc.sh) в якоря
страниц PDF «⌗p.N+1» (1-based, как в просмотрщике) — чтобы из БЗ можно было
сослаться на исходную страницу файла. Отключается в prepare_doc.sh флагом
--no-page-anchors (тогда маркеров в MD нет вовсе).

Usage: clean_md.py <file.md> | clean_md.py --selftest
"""

import pathlib
import re
import sys

# лидер: 4+ точки перед «|» (ячейка TOC-таблицы) или перед номером страницы
LEADER = re.compile(r"\.{4,}\s*(\||\d)|\.{8,}")
# сырой маркер страницы marker: {N} + разделитель (деф. 48 дефисов)
PAGEMARK = re.compile(r"^\{(\d+)\}-{3,}[ \t]*$")


def rewrite_pagemarks(lines):
    """«{N}----…» -> «⌗p.N+1» (1-based). Возвращает (строки, число меток)."""
    out, n = [], 0
    for ln in lines:
        m = PAGEMARK.match(ln.rstrip("\n"))
        if m:
            out.append(f"⌗p.{int(m.group(1)) + 1}\n")
            n += 1
        else:
            out.append(ln)
    return out, n


def clean(text):
    """(текст) -> (очищенный текст, удалено лидеров, размечено страниц)."""
    lines, marks = rewrite_pagemarks(text.splitlines(keepends=True))
    out, removed = [], 0
    for ln in lines:
        if LEADER.search(ln.rstrip("\n")):
            removed += 1
            continue
        out.append(ln)
    t = re.sub(r"\n{4,}", "\n\n\n", "".join(out))
    return t, removed, marks


def run(path):
    p = pathlib.Path(path)
    t, removed, marks = clean(p.read_text(encoding="utf-8"))
    p.write_text(t, encoding="utf-8")
    print(
        f"cleanup: удалено строк с лидерами {removed}, меток страниц ⌗p {marks} -> {p.name}"
    )


def selftest():
    sep = "-" * 48
    checks = []

    def check(name, cond):
        checks.append((name, bool(cond)))

    # 1) базовое: лидеры удаляются
    t, r, _ = clean("1.1. Процесс .......... 2\nобычный текст\n")
    check("лидер удалён", r == 1 and "Процесс" not in t and "обычный текст" in t)

    # 2) строки без лидеров не трогаются
    t, r, _ = clean("Точка. Три точки... конец\n")
    check("короткие точки не лидер", r == 0 and "Точка." in t)

    # 3) серии пустых схлопываются
    t, _, _ = clean("a\n\n\n\n\n\nb\n")
    check("пустые схлопнуты", t == "a\n\n\nb\n")

    # 4) сырой маркер страницы -> ⌗p.N+1
    t, _, m = clean(f"текст0\n\n{{0}}{sep}\n\nтекст1\n\n{{41}}{sep}\n\nтекст2\n")
    check("метки страниц 2 шт", m == 2)
    check(
        "0-based -> 1-based",
        "⌗p.1\n" in t and "⌗p.42\n" in t and "{0}" not in t and "{41}" not in t,
    )

    # 5) содержимое страниц сохраняется, порядок меток правильный
    t, _, _ = clean(f"a\n\n{{0}}{sep}\n\nb\n\n{{1}}{sep}\n\nc\n")
    check(
        "порядок сохранён",
        t.index("⌗p.1") < t.index("b") < t.index("⌗p.2") < t.index("c"),
    )

    # 6) без маркеров — без меток, текст не меняется
    t, r, m = clean("просто текст\n")
    check("без маркеров ничего", m == 0 and r == 0 and t == "просто текст\n")

    # 7) лидер внутри таблицы (короткий, 4+ точки) + маркер рядом
    t, r, m = clean(f"| 3.3 Название .... | 54 |\n\n{{7}}{sep}\n\nдальше\n")
    check(
        "таблица-лидер + метка",
        r == 1 and m == 1 and "⌗p.8" in t and "54" not in t.split("⌗")[0],
    )

    # 8) маркер в последней строке без \n
    t, _, m = clean(f"\n{{2}}{sep}")
    check("маркер без хвостового \\n", m == 1 and t.endswith("⌗p.3\n"))

    # 9) {N} с коротким разделителем (<3 дефисов) — не маркер
    t, _, m = clean("{5}--\nтекст\n")
    check("короткий разделитель не метка", m == 0 and "{5}--" in t)

    # 10) blank-collapse не съедает blanks вокруг меток
    t, _, _ = clean(f"a\n\n\n\n{{0}}{sep}\n\n\n\nb\n")
    check("бланки вокруг метки", "\n\n\n⌗p.1\n" in t and "\n\n\nb" in t)

    bad = [n for n, ok in checks if not ok]
    for n, ok in checks:
        print(f"  {'ok' if ok else 'FAIL'}  {n}")
    print(f"selftest clean_md: {len(checks) - len(bad)}/{len(checks)}")
    return 1 if bad else 0


def main():
    if len(sys.argv) == 2 and sys.argv[1] == "--selftest":
        sys.exit(selftest())
    if len(sys.argv) != 2:
        sys.exit("usage: clean_md.py <file.md> | clean_md.py --selftest")
    run(sys.argv[1])


if __name__ == "__main__":
    main()
