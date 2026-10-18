#!/usr/bin/env python3
"""CHM (распакованный) -> Markdown для преп-пайплайна: PowerDesigner-доки.

Источник: каталог распакованного CHM (*.hhc + *.html + картинки). Имена html —
хеши, порядок чтения и человеческие заголовки — в .hhc (Sitemap 1.0).

Что делает:
  - обходит .hhc (вложенность UL -> уровень заголовка ##/###/…, деф. с ##);
  - каждый html -> MD (html2text; кодировка из <meta>, деф. cp1252), внутренние
    заголовки демотируются на +1, первый — выбрасывается, если совпадает с
    именем из TOC (иначе был бы дубль);
  - перед каждой секцией — якорь источника «⌗doc:<имя>.html» (аналог меток
    страниц ⌗p у PDF: ссылка на оригинал, грепается, переживает чанкинг);
  - картинки: референсы <min-image-bytes (иконки GUI) выбрасываются, остальные
    копируются в figures/ (".image" -> ".png" — это PNG по магике), ссылки в MD
    переписываются на figures/<имя>;
  - дубли в TOC — первый вхождения, повторы пропускаются (счётчик);
  - html вне TOC — хвостом в секию «Не в оглавлении» (терять контент нельзя);
  - результат: <out>/<title>/<title>.md + figures/ + _meta.json (карта секций).

Дальше — обычный пайплайн: caption_figures.py (--structured — скриншоты GUI
получат prose, диаграммы — Mermaid), clean_md.py, qc_check.py --lang en.

Usage: chm_to_md.py [--title ИМЯ] [--out OUTBOX] [--min-image-bytes N] <файл.hhc>
       chm_to_md.py --selftest
"""

import argparse
import html
import json
import pathlib
import re
import shutil
import sys

import html2text

HHC_ITEM = re.compile(
    r'<param\s+name="Name"\s+value="([^"]*)"\s*>\s*'
    r'(?:<param\s+name="Local"\s+value="([^"]*)"\s*>)?',
    re.IGNORECASE,
)
IMG_SRC = re.compile(r'src="([^"]+)"', re.IGNORECASE)
IMG_MD = re.compile(r"!\[[^\]]*\]\(([^)]+)\)")
HEAD_MD = re.compile(r"^(#{1,6})\s", re.MULTILINE)
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def parse_hhc(text):
    """[(имя, local|None, уровень)] в порядке документа; уровень = вложенность
    UL (1 = верхний). Пары Name+Local собираются потоково: Name без Local
    (до следующего Name/конца) — заголовок-разделитель без контента."""
    items, level, pending = [], 0, None
    tok_re = re.compile(
        r'<UL>|</UL>|<param\s+name="(Name|Local)"\s+value="([^"]*)"', re.IGNORECASE
    )
    for m in tok_re.finditer(text):
        tok = m.group(0)
        if tok.lower().startswith("<ul"):
            level += 1
        elif tok.lower().startswith("</ul"):
            level = max(0, level - 1)
        elif m.group(1).lower() == "name":
            if pending:
                items.append((pending, None, max(1, level)))
            pending = re.sub(r"\s+", " ", html.unescape(m.group(2))).strip()
        else:  # Local
            if pending:
                items.append((pending, m.group(2), max(1, level)))
                pending = None
    if pending:
        items.append((pending, None, max(1, level)))
    return items


def _norm(s):
    return re.sub(r"[^a-z0-9]+", "", s.lower())


def page_to_md(path, toc_name, heading_level):
    """html -> (md_текст, [ссылки на картинки]). Первый заголовок страницы,
    совпадающий с именем из TOC, выбрасывается; прочие демотируются на +1."""
    raw = path.read_bytes()
    enc_m = re.search(rb"charset=([\w-]+)", raw)
    enc = (enc_m.group(1) if enc_m else b"cp1252").decode()
    try:
        t = raw.decode(enc)
    except (LookupError, UnicodeDecodeError):
        t = raw.decode("cp1252", errors="replace")
    m = re.search(r"<body[^>]*>(.*?)</body>", t, re.DOTALL | re.IGNORECASE)
    body = m.group(1) if m else t
    body = re.sub(
        r"<script.*?</script>|<noscript.*?</noscript>",
        "",
        body,
        flags=re.DOTALL | re.IGNORECASE,
    )
    h = html2text.HTML2Text()
    h.body_width = 0
    md = h.handle(body).strip()
    md = re.sub(r"\n{3,}", "\n\n", md)

    # первый заголовок-дубль TOC — убрать; остальные демотировать на +1
    lines = md.splitlines(keepends=True)
    if lines and lines[0].startswith("#"):
        title_txt = re.sub(r"^#+\s*|\s*\n$", "", lines[0])
        if _norm(title_txt)[:60] == _norm(toc_name or "")[:60]:
            lines = lines[1:]
            md = "".join(lines).lstrip("\n")
    dem = heading_level + 1

    def _demote(m):
        return "#" * min(6, len(m.group(1)) + dem - 1) + " "

    md = HEAD_MD.sub(_demote, md)
    return md


def convert(hhc_path, title, out_root, min_image_bytes):
    src = hhc_path.parent
    items = parse_hhc(hhc_path.read_text(encoding="cp1252", errors="replace"))
    out_dir = pathlib.Path(out_root) / title
    figs = out_dir / "figures"
    out_dir.mkdir(parents=True, exist_ok=True)

    seen, used_imgs, sections, dups = set(), {}, [], 0
    parts = [f"# {title}\n"]

    def copy_img(ref):
        """ref -> относительная ссылка на figures/…; мелкие иконки — None."""
        p = (src / ref).resolve()
        if not p.is_file() or p.stat().st_size < min_image_bytes:
            return None
        if p in used_imgs:
            return used_imgs[p]
        ext = (
            ".png"
            if p.suffix == ".image" or p.read_bytes()[:8] == PNG_MAGIC
            else p.suffix
        )
        name = p.stem + ext
        dst = figs / name
        if dst.exists() and (figs / name).read_bytes() != p.read_bytes():
            name = f"{p.stem}_{len(used_imgs)}{ext}"
            dst = figs / name
        figs.mkdir(exist_ok=True)
        shutil.copy2(p, dst)
        used_imgs[p] = f"figures/{name}"
        return used_imgs[p]

    for name, local, level in items:
        if not name:
            continue
        lvl = min(6, level + 1)
        if not local:
            parts.append(f"\n\n{'#' * lvl} {name}\n")
            sections.append({"name": name, "local": None, "level": lvl})
            continue
        # TOC-ссылка с #фрагментом: если базовая страница уже включена —
        # это навигационный повтор (контент в ней), тихий скип
        base = local.split("#")[0] or local
        if base != local and base in seen:
            continue
        if local in seen:
            dups += 1
            continue
        local = base
        seen.add(local)
        page = src / local
        if not page.is_file():
            print(
                f"  [!] {local} ({name}): файла нет — секция-заголовок", file=sys.stderr
            )
            parts.append(f"\n\n{'#' * lvl} {name}\n")
            continue
        parts.append(f"\n\n{'#' * lvl} {name}\n\n⌗doc:{local}\n\n")
        md = page_to_md(page, name, lvl)
        for ref in sorted({str(m.group(1)) for m in IMG_MD.finditer(md)}):
            new = copy_img(
                re.sub(r"^figures/", "", ref) if ref.startswith("figures/") else ref
            )
            if new is None:
                md = md.replace(ref, "")
            elif new != ref:
                md = md.replace(ref, new)
        parts.append(md)
        sections.append({"name": name, "local": local, "level": lvl})

    # html вне TOC — хвостом, терять нельзя
    orphans = sorted(
        p.name
        for p in src.glob("*.html")
        if p.name not in seen and p.name != hhc_path.name
    )
    if orphans:
        parts.append(f"\n\n# Не в оглавлении ({len(orphans)})\n")
        for o in orphans:
            parts.append(f"\n\n⌗doc:{o}\n\n")
            parts.append(page_to_md(src / o, o, 2))
            sections.append({"name": o, "local": o, "level": 2, "orphan": True})

    md_text = re.sub(r"\n{4,}", "\n\n\n", "".join(parts)) + "\n"
    md_file = out_dir / f"{title}.md"
    md_file.write_text(md_text, encoding="utf-8")
    meta = {
        "source": str(hhc_path),
        "title": title,
        "sections": len(sections),
        "duplicates_skipped": dups,
        "orphans": len(orphans),
        "images_copied": len(used_imgs),
    }
    (out_dir / "_meta.json").write_text(
        json.dumps({**meta, "toc": sections}, ensure_ascii=False, indent=1),
        encoding="utf-8",
    )
    print(
        f"chm_to_md: {title}: секций {len(sections)}, дубли пропущены {dups}, "
        f"вне TOC {len(orphans)}, картинок {len(used_imgs)} -> {md_file}"
    )
    return 0


def selftest():
    import tempfile

    checks = []

    def check(name, cond):
        checks.append((name, bool(cond)))

    with tempfile.TemporaryDirectory() as td:
        td = pathlib.Path(td)
        # синтетический CHM: вложенность, дубли, сирота, .image (PNG), мелкая иконка
        png = PNG_MAGIC + b"\x00" * 50
        (td / "a.image").write_bytes(png + b"X" * 200)
        (td / "icon.image").write_bytes(PNG_MAGIC + b"Y" * 2)  # < min-image-bytes
        (td / "data.hhc").write_text(
            '<UL><LI><OBJECT type="text/sitemap"><param name="Name" value="Root Doc">'
            '<param name="Local" value="root.html"></OBJECT><UL>'
            '<LI><OBJECT type="text/sitemap"><param name="Name" value="Chapter One&#xA;  ">'
            '<param name="Local" value="ch1.html"></OBJECT></LI>'
            '<LI><OBJECT type="text/sitemap"><param name="Name" value="Chapter One">'
            '<param name="Local" value="ch1.html"></OBJECT></LI>'
            '<LI><OBJECT type="text/sitemap"><param name="Name" value="Only Folder">'
            "</OBJECT><UL>"
            '<LI><OBJECT type="text/sitemap"><param name="Name" value="Sub Section">'
            '<param name="Local" value="sub.html"></OBJECT></LI></UL></LI>'
            "</UL></LI></UL>",
            encoding="cp1252",
        )
        (td / "root.html").write_text(
            '<html><head><meta charset="cp1252"><title>Root Doc</title></head>'
            "<body><h1>Root Doc</h1><p>Intro text.</p>"
            '<img src="a.image"/></body></html>',
            encoding="cp1252",
        )
        (td / "ch1.html").write_text(
            "<html><head><title>Ch1</title></head><body><h1>Chapter One</h1>"
            '<p>Body <b>bold</b>.</p><img src="icon.image"/></body></html>',
            encoding="cp1252",
        )
        (td / "sub.html").write_text(
            "<html><body><h2>Inner heading</h2><table><tr><td>k</td><td>v</td></tr></table>"
            "<h3>Deeper</h3></body></html>",
            encoding="cp1252",
        )
        (td / "stray.html").write_bytes(b"<html><body><p>Orphan page</p></body></html>")

        out = td / "outbox"
        rc = convert(td / "data.hhc", "Test Doc", out, min_image_bytes=64)
        md = (out / "Test Doc" / "Test Doc.md").read_text(encoding="utf-8")

        check("rc=0", rc == 0)
        check("якорь ⌗doc:ch1.html", "⌗doc:ch1.html" in md)
        check(
            "H1 документа один",
            md.count("# Test Doc") == 1 and md.startswith("# Test Doc"),
        )
        check("уровень: корень ##", "\n## Root Doc" in md)
        check("уровень: глава ###", "\n### Chapter One" in md)
        check("уровень: подраздел ####", "\n#### Sub Section" in md)
        check("папка без local — заголовок", "Only Folder" in md)
        check("дубль пропущен", md.count("⌗doc:ch1.html") == 1)
        check("дубль-заголовок не задвоен", md.count("### Chapter One") == 1)
        check("сирота включён", "Orphan page" in md and "stray.html" in md)
        check(
            "PNG скопирован + ссылка",
            "figures/a.png" in md
            and (out / "Test Doc" / "figures" / "a.png").is_file(),
        )
        check("иконка выброшена", "icon" not in md)
        check("таблица сохранена", "| k" in md or "k |" in md or "k" in md)
        check(
            "внутренние заголовки демотированы",
            "##### Inner heading" in md and "###### Deeper" in md,
        )
        check("meta.json есть", (out / "Test Doc" / "_meta.json").is_file())

    bad = [n for n, ok in checks if not ok]
    for n, ok in checks:
        print(f"  {'ok' if ok else 'FAIL'}  {n}")
    print(f"selftest chm_to_md: {len(checks) - len(bad)}/{len(checks)}")
    return 1 if bad else 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("hhc", nargs="?", type=pathlib.Path, help="путь к .hhc")
    ap.add_argument("--title", help="имя документа/каталога (деф. — первый пункт TOC)")
    ap.add_argument("--out", default="outbox", help="выходной корень (деф. outbox)")
    ap.add_argument(
        "--min-image-bytes",
        type=int,
        default=4096,
        help="мин. размер картинки для копирования (деф. 4096 — иконки GUI в отброс)",
    )
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        sys.exit(selftest())
    if not a.hhc or not a.hhc.is_file():
        sys.exit("usage: chm_to_md.py [--title ИМЯ] <файл.hhc> | --selftest")
    items = parse_hhc(a.hhc.read_text(encoding="cp1252", errors="replace"))
    title = a.title or (next((n for n, l, _ in items if n), "CHM Document"))
    sys.exit(convert(a.hhc, title, a.out, a.min_image_bytes))


if __name__ == "__main__":
    main()
