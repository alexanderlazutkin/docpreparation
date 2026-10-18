#!/usr/bin/env python3
"""Ремонт провальных таблиц marker: 2-я попытка + внятный отчёт о потерях.

Маркер логирует `Table OCR failed for block /page/N/<Label>/K` (N — 0-based).
Основная обработка (balanced) могла потерять такие таблицы; fast-режим
(pdftext, текст-слой) часто читает их. Ремонт:

  1) --parse <marker.log>  — выбрать провальные страницы/блоки:
        stdout: TABLE_REPAIR_PAGES=<0-based csv> (пусто = провалов нет)
                + человеческие строки «стр. N (инд. M): Label/K»
  2) --splice --main <md> --page <0-based> --retry <retry.md> [--label L]
        — вставить содержимое retry в конец СЕКЦИИ страницы main (секции =
          куски между якорями страниц: сырые маркеры marker «{N}-----»
          (paginate_output, 0-based), очищенные «⌗p.N» (после clean_md,
          1-based) и легаси <span id="page-N-M">; страница без якоря
          относится к предыдущей секции). Вставка, а не замена: исходная
          секция может содержать частичный рендер и непрерывный контекст.
        — вердикт: восстановлено (строк таблицы X) / не восстановлено (что
          потеряно: блок, страница, метрики обеих попыток).

Ограничения: фрагментный режим (prepare_doc.sh) ремонтом не покрывается
(там QC мягкий) — пропуск с честной записью в лог. Overflow-потери ячеек
(частичные) маркер не локализует по страницам — не ремонтируются.
"""

import argparse
import re
import sys

ANCHOR = re.compile(r'<span id="page-(\d+)-\d+">')
FAILED = re.compile(r"Table OCR failed for block /page/(\d+)/([^/]+/\d+)")
# сырой маркер страницы marker ({N} + 48 дефисов; N 0-based)
PAGEMARK_RAW = re.compile(r"^\{(\d+)\}-{3,}[ \t]*$", re.MULTILINE)
# очищенный маркер после clean_md (⌗p.N; N 1-based PDF-страница)
PAGEMARK_CLEAN = re.compile(r"^⌗p\.(\d+)[ \t]*$", re.MULTILINE)


def table_lines(t):
    return sum(1 for ln in t.splitlines() if ln.lstrip().startswith("|"))


def _all_marks(md):
    """[(pos, page_idx_0based)] по всем типам якорей страниц, по порядку."""
    ms = [(m.start(), int(m.group(1))) for m in ANCHOR.finditer(md)]
    ms += [(m.start(), int(m.group(1))) for m in PAGEMARK_RAW.finditer(md)]
    ms += [(m.start(), int(m.group(1)) - 1) for m in PAGEMARK_CLEAN.finditer(md)]
    ms.sort()
    return ms


def sections(md):
    """[(first_page_idx|None, text)] — раскол по якорям страниц; None = преамбула."""
    out, last, pos = [], None, 0
    for m_start, page_idx in _all_marks(md):
        out.append((last, md[pos:m_start]))
        last, pos = page_idx, m_start
    out.append((last, md[pos:]))
    return out


def parse(logfile):
    seen = {}
    with open(logfile, encoding="utf-8", errors="ignore") as f:
        for ln in f:
            for m in FAILED.finditer(ln):
                seen.setdefault(int(m.group(1)), []).append(m.group(2))
    pages = sorted(seen)
    print(f"TABLE_REPAIR_PAGES={','.join(map(str, pages)) if pages else ''}")
    for p in pages:
        print(f"  стр. {p + 1} (инд. {p}): {', '.join(seen[p])}")
    return pages


def splice(main_path, page, retry_path, label):
    with open(main_path, encoding="utf-8") as f:
        md = f.read()
    with open(retry_path, encoding="utf-8") as f:
        retry = f.read().strip()
    retry = re.sub(r"\n{3,}", "\n\n", retry)
    secs = sections(md)
    # содержащая секция: последняя с first_page <= page (преамбула = -1)
    target = max(
        (i for i, (fp, _) in enumerate(secs) if (fp if fp is not None else -1) <= page),
        default=-1,
    )
    if target < 0:
        print("TABLE_REPAIR_REPLACED=0")
        print(f"  стр. {page + 1}: секция не найдена — вставка пропущена")
        return 1
    tl = table_lines(retry)
    if tl < 2:
        print("TABLE_REPAIR_REPLACED=0")
        print(
            f"  стр. {page + 1}: НЕ восстановлено — блок {label or '?'}: "
            f"fast-попытка тоже без таблицы (строк таблицы {tl}); потеря "
            f"остаётся (текст-слой этой страницы не содержит таблицу)"
        )
        return 1
    fp, txt = secs[target]
    sec_tl = table_lines(txt)
    glued = (
        md[: md.index(txt)]
        + txt.rstrip("\n")
        + "\n\n"
        + retry
        + "\n\n"
        + md[md.index(txt) + len(txt) :]
    )
    # страховка от двойной вставки при повторном прогоне ремонта
    if retry[:200] in md:
        print("TABLE_REPAIR_REPLACED=0")
        print(f"  стр. {page + 1}: уже восстановлено ранее — пропуск")
        return 0
    with open(main_path, "w", encoding="utf-8") as f:
        f.write(glued)
    print("TABLE_REPAIR_REPLACED=1")
    where = (
        "конец секции страницы"
        if fp == page
        else (
            f"конец секции, начинающейся на стр. {(fp if fp is not None else 0) + 1} "
            "(у страницы нет своего якоря)"
        )
    )
    note = (
        ""
        if sec_tl == 0
        else (
            f"; ВНИМАНИЕ: в секции уже было {sec_tl} строк таблицы — возможен дубль,"
            f" сверь спот-чеком"
        )
    )
    print(
        f"  стр. {page + 1}: восстановлено, вставлено в {where} "
        f"(строк таблицы в попытке: {tl}, символов: {len(retry)}{note})"
    )
    return 0


def selftest():
    sep = "-" * 48
    checks = []

    def check(name, cond):
        checks.append((name, bool(cond)))

    # --- sections(): сырые маркеры marker ---
    md = f"преамбула\n\n{{0}}{sep}\n\nтекст0\n\n{{1}}{sep}\n\nтекст1\n"
    secs = sections(md)
    check("raw: 3 секции", len(secs) == 3)
    check("raw: преамбула None", secs[0][0] is None and "преамбула" in secs[0][1])
    check("raw: стр.0", secs[1][0] == 0 and "текст0" in secs[1][1])
    check("raw: стр.1", secs[2][0] == 1 and "текст1" in secs[2][1])

    # --- sections(): очищенные ⌗p.N (1-based -> 0-based) ---
    md = "x\n\n⌗p.11\n\nтекст\n"
    secs = sections(md)
    check("clean: 10-based из ⌗p.11", secs[1][0] == 10 and "текст" in secs[1][1])

    # --- sections(): легаси span-якоря по-прежнему работают ---
    md = 'a\n<span id="page-3-1">\n| tbl |\n'
    secs = sections(md)
    check("span: стр.3", secs[1][0] == 3 and "| tbl |" in secs[1][1])

    # --- splice(): вставка в конец СЕКЦИИ нужной страницы (сырые маркеры) ---
    import pathlib
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        td = pathlib.Path(td)
        main = td / "main.md"
        retry = td / "retry.md"
        main.write_text(
            f"начало\n\n{{0}}{sep}\n\nстр0 без таблицы\n\n{{1}}{sep}\n\nстр1\n\n{{2}}{sep}\n\nконец\n",
            encoding="utf-8",
        )
        retry.write_text("| h1 | h2 |\n|---|---|\n| a | b |\n", encoding="utf-8")
        rc = splice(str(main), 1, str(retry), "Table/5")
        t = main.read_text(encoding="utf-8")
        check("splice: rc=0", rc == 0)
        check(
            "splice: перед маркером стр.2",
            t.index("стр1") < t.index("| a | b |") < t.index("⌗")
            if "⌗" in t
            else t.index("стр1") < t.index("| a | b |") < t.index(f"{{2}}{sep}"),
        )
        check(
            "splice: конец документа не затронут вставкой до него",
            t.rstrip().endswith("конец"),
        )

        # повторный splice того же контента — защита от дубля
        rc2 = splice(str(main), 1, str(retry), "Table/5")
        t2 = main.read_text(encoding="utf-8")
        check("splice: повтор = пропуск", rc2 == 0 and t2.count("| a | b |") == 1)

    bad = [n for n, ok in checks if not ok]
    for n, ok in checks:
        print(f"  {'ok' if ok else 'FAIL'}  {n}")
    print(f"selftest table_repair: {len(checks) - len(bad)}/{len(checks)}")
    return 1 if bad else 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--parse", metavar="marker.log")
    ap.add_argument("--splice", action="store_true")
    ap.add_argument("--main")
    ap.add_argument("--page", type=int, help="0-based индекс страницы")
    ap.add_argument("--retry")
    ap.add_argument("--label", default="")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        sys.exit(selftest())
    if a.parse:
        sys.exit(0 if parse(a.parse) or True else 1)
    if a.splice:
        sys.exit(splice(a.main, a.page, a.retry, a.label))
    ap.error("нужен --parse или --splice")


if __name__ == "__main__":
    main()
