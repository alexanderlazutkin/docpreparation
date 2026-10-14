#!/usr/bin/env python3
"""Очистка MD перед QC: удаляет строки с точечными лидерами оглавления
(«1.1. Процесс ......... 2») — TOC-мусор не должен попадать в индекс БЗ.
Схлопывает серии пустых строк. Usage: clean_md.py <file.md>
"""

import re
import sys
import pathlib

p = pathlib.Path(sys.argv[1])
lines = p.read_text(encoding="utf-8").splitlines(keepends=True)
out, removed = [], 0
for ln in lines:
    if re.search(r"\.{8,}", ln):
        removed += 1
        continue
    out.append(ln)
t = re.sub(r"\n{4,}", "\n\n\n", "".join(out))
p.write_text(t, encoding="utf-8")
print(f"cleanup: удалено строк с лидерами {removed} -> {p.name}")
