#!/usr/bin/env python3
"""Вендор-патч marker-pdf: raw-HTML хвост в MD на больших документах.

Root cause (расследование 2026-10-27, сессия rawhtml_ab):
1. marker 2.0.0 пропускает VLM-вывод (surya-2) в HTML без эскейпа
   (fast-path `if self.html: return self.html` в schema/text/span.py:86) —
   в full_html попадают сырые инвалидные charref'ы вида `&#`
   (пример: сноска «<sup>&#</sup>x27; Finkelstein 1992…», стр. 110).
2. bs4 >=4.13 (здесь 4.15.0) парсит html.parser с convert_charrefs=False
   (bs4/builder/_htmlparser.py:413).
3. CPython html.parser: инвалидный `&#` в stop-позиции (сразу после тега)
   заставляет goahead() выйти из цикла («wait for more data»). Первое
   вхождение ломает feed() (безвредно — close() доюирает), ВТОРОЕ ломает
   close(): при end=1 остаток документа сбрасывается одним текстовым узлом
   (html/parser.py, финальный flush rawdata[i:n]) → markdownify выводит его
   как есть → хвост документа = сырой HTML, QC слеп.

   Поэтому баг выглядит «кумулятивным» и проявляется только когда в документе
   >= 2 tag-смежных инвалидных `&#` (в книге Chmura/Heumann — стр. 25 и 110,
   слом с стр. 111; окно 101–131 чистое — там только одно вхождение).

Fix: эскейпим инвалидные `&#` → `&amp;#` в full_html (extract_html, level==0)
перед любой дальнейшей парсинг-конвертацией. Семантика не меняется: это и
был литеральный текст. Валидные charref'ы (`&#x27;`, `&#65;`) не трогаем —
регекс зеркалит charref из html.parser.

Патч идемпотентен и живёт в .venv до переустановки marker (uv sync при
смене lock). prepare_doc.sh вызывает патчер перед каждым запуском marker;
QC-гейт (qc_check.py, «сырой HTML») ловит регрессию, если патч забыт.

Использование:
  python scripts/patch_marker.py            # применить (идемпотентно)
  python scripts/patch_marker.py --check    # exit 0 = применён, 1 = нет
  python scripts/patch_marker.py --selftest # репро бага до/после патча
"""

import importlib.util
import sys
import subprocess

PATCH_MARK = "# docpreparation vendor patch (raw-HTML tail bug)"

FUNC_BLOCK = """
# docpreparation vendor patch (raw-HTML tail bug): bare `&#` from VLM output
# makes html.parser bail out of goahead() (bs4>=4.13: convert_charrefs=False);
# a 2nd occurrence during close() flushes the rest of the document as a single
# text node. Escape invalid charrefs to literal text; valid ones untouched.
_INVALID_CHARREF_RE = re.compile(r"&#(?!(?:[0-9]+|[xX][0-9a-fA-F]+)[^0-9a-fA-F])")


def sanitize_charrefs(html_text: str) -> str:
    return _INVALID_CHARREF_RE.sub("&amp;#", html_text)

"""

ANCHOR_DEF = "    def extract_html(self, document, document_output, level=0):"
ANCHOR_RET = '''""")

        return output, images'''
PATCHED_RET = '''""")
            output = sanitize_charrefs(output)  # docpreparation vendor patch

        return output, images'''


def marker_html_path():
    spec = importlib.util.find_spec("marker.renderers.html")
    if spec is None or not spec.origin:
        sys.exit(
            "FAIL: marker не найден в текущем окружении (запускай из .venv / uv run)"
        )
    return spec.origin


def read(p):
    with open(p, encoding="utf-8") as f:
        return f.read()


def is_patched(src):
    return PATCH_MARK in src


def apply():
    p = marker_html_path()
    src = read(p)
    if is_patched(src):
        print(f"[--] патч уже применён: {p}")
        return
    if ANCHOR_DEF not in src or ANCHOR_RET not in src:
        sys.exit(
            "FAIL: якоря патча не найдены — marker-pdf изменился "
            "(версия?), патч нужно переписать. Файл: " + p
        )
    backup = p + ".vendor-orig"
    try:
        read(backup)
    except FileNotFoundError:
        with open(backup, "w", encoding="utf-8") as f:
            f.write(src)
    src = src.replace(ANCHOR_DEF, FUNC_BLOCK + ANCHOR_DEF, 1)
    src = src.replace(ANCHOR_RET, PATCHED_RET, 1)
    with open(p, "w", encoding="utf-8") as f:
        f.write(src)
    print(f"[OK] патч применён: {p} (бэкап: {backup})")


def check():
    p = marker_html_path()
    ok = is_patched(read(p))
    print(("[OK] патч на месте: " if ok else "[--] патч ОТСУТСТВУЕТ: ") + p)
    sys.exit(0 if ok else 1)


def selftest():
    """Репро бага (до патча — на сыром HTML, после — через sanitize)."""
    from bs4 import BeautifulSoup

    poison = (
        "<html><body>"
        "<div class='page' data-page-id='25'><p><sup>&#</sup>quot;Layout of the HSTN.</p></div>"
        "<div class='page' data-page-id='111'><p><sup>&#</sup>x27; Finkelstein 1992: 35-38.</p></div>"
        "<div class='page' data-page-id='112'><table border='1'><tr><td>TAIL</td></tr></table></div>"
        "</body></html>"
    )
    leaks = [
        s
        for s in BeautifulSoup(poison, "html.parser").strings
        if "data-page-id" in str(s)
    ]
    assert leaks, "репро сломался: сырой HTML не течёт (парсер уже починили?)"
    print(f"[OK] репро: без патча хвост течёт ({len(leaks[0])} байт текстовым узлом)")

    from marker.renderers.html import sanitize_charrefs

    cases = [
        ("<sup>&#</sup>", "<sup>&amp;#</sup>"),
        ("&#x27;", "&#x27;"),  # валидный hex charref — не трогаем
        ("&#65;", "&#65;"),  # валидный dec charref — не трогаем
        ("&#65<", "&#65<"),  # валидный без ';' (терминатор <) — не трогаем
        ("&#xZZ", "&amp;#xZZ"),  # x без hex — инвалидный
        ("&# Finkelstein", "&amp;# Finkelstein"),
        ("a & b", "a & b"),  # голый амперсанд — не трогаем
        ("&amp;#x27;", "&amp;#x27;"),  # уже эскейпнут — не трогаем
    ]
    for src, want in cases:
        got = sanitize_charrefs(src)
        assert got == want, f"sanitize({src!r}) = {got!r}, ожидалось {want!r}"
    print("[OK] sanitize_charrefs: 8/8 кейсов")

    clean = [
        s
        for s in BeautifulSoup(sanitize_charrefs(poison), "html.parser").strings
        if "data-page-id" in str(s)
    ]
    assert not clean, "после sanitize хвост всё ещё течёт"
    print("[OK] после патча: дерево парсится целиком, утечки нет")
    print("SELFTEST PASS")


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "apply"
    if cmd == "apply":
        apply()
    elif cmd == "--check":
        check()
    elif cmd == "--selftest":
        apply()
        selftest()
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main()
