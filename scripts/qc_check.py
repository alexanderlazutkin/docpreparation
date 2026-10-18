#!/usr/bin/env python3
"""QC: доля букв целевого языка среди всех букв (не символов — иммунитет к
пробельным паддингам таблиц), артефакты оглавления, переносы, пустые таблицы.

Язык: --lang ru|en (деф. ru). ru -> гейт кириллицы >= 55%, en -> гейт
латиницы >= 55%. prepare_doc.sh передаёт язык документа (флаг --lang или
распознанный: имя файла -> метаданные PDF -> текст)."""

import re
import sys
import pathlib

args = sys.argv[1:]
lang = "ru"
pos = []
i = 0
while i < len(args):
    a = args[i]
    if a.startswith("--lang="):
        lang = a.split("=", 1)[1]
    elif a == "--lang":
        i += 1
        if i >= len(args):
            sys.exit("--lang: ожидается ru|en (--lang ru | --lang=ru)")
        lang = args[i]
    else:
        pos.append(a)
    i += 1
if lang not in ("ru", "en"):
    sys.exit(f"--lang: ожидается ru|en, получено «{lang}»")
if len(pos) != 1:
    sys.exit("usage: qc_check.py [--lang ru|en] <file.md>")

p = pathlib.Path(pos[0])
t = p.read_text(encoding="utf-8")
cyr = len(re.findall(r"[А-Яа-яЁё]", t))
lat = len(re.findall(r"[A-Za-z]", t))
ratio = cyr / max(1, cyr + lat)  # доля кириллицы среди букв
issues = []
if lang == "ru" and ratio < 0.55:
    issues.append(f"КИРИЛЛИЦА {ratio:.1%} (среди букв) < 55% — возможна потеря текста")
if lang == "en" and (1 - ratio) < 0.55:
    issues.append(
        f"ЛАТИНИЦА {1 - ratio:.1%} (среди букв) < 55% — возможна потеря текста"
    )
if len(re.findall(r"\.{8,}", t)) > 5:
    issues.append("оглавление/точечные лидеры не вычищены")
# переносы: только целевого алфавита (лат. идентификаторы в ру-документе
# и наоборот не должны давать ложный FAIL)
_hyph = re.findall(r"[а-яё]-\n[а-яё]" if lang == "ru" else r"[a-z]-\n[a-z]", t)
if len(_hyph) > 10:
    issues.append("переносы не склеены")
if t.count("|--") < 2 and t.count("<table") < 2:
    issues.append("таблицы не распознаны (а ожидались)")
print(
    f"{p.name}: lang={lang}, cyr={ratio:.1%}/lat={1 - ratio:.1%} "
    f"(среди {cyr + lat} букв), {len(t)} chars"
)
print("QC PASS" if not issues else "QC FAIL:\n- " + "\n- ".join(issues))
sys.exit(1 if issues else 0)
