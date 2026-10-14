#!/usr/bin/env python3
"""QC: доля кириллицы, артефакты оглавления, переносы, пустые таблицы."""

import re, sys, pathlib

p = pathlib.Path(sys.argv[1])
t = p.read_text(encoding="utf-8")
cyr = len(re.findall(r"[А-Яа-яЁё]", t)) / max(1, len(t))
issues = []
if cyr < 0.55:
    issues.append(f"КИРИЛЛИЦА {cyr:.1%} < 55% — возможна потеря текста")
if len(re.findall(r"\.{8,}", t)) > 5:
    issues.append("оглавление/точечные лидеры не вычищены")
if len(re.findall(r"[а-яё]-\n[а-яё]", t)) > 10:
    issues.append("переносы не склеены")
if t.count("|--") < 2 and t.count("<table") < 2:
    issues.append("таблицы не распознаны (а ожидались)")
print(f"{p.name}: cyr={cyr:.1%}, {len(t)} chars")
print("QC PASS" if not issues else "QC FAIL:\n- " + "\n- ".join(issues))
sys.exit(1 if issues else 0)
