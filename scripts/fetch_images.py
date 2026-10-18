#!/usr/bin/env python3
"""Скачать картинки вэб-страницы в MD-экспорте (Obsidian Web Clipper).

MD содержит абсолютные http(s)-ссылки на картинки; скрипт скачивает их
в каталог MD-файла и переписывает ссылки на локальные имена (файл готов
к caption_figures.py и Obsidian). data:URI и SVG не трогаются (VLM не читает
SVG); битые/недоступные — остаются URL (caption их пропустит как битые).

Usage: fetch_images.py <file.md>
"""

import os
import re
import sys
import urllib.parse
import urllib.request

UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/128 Safari/537.36"
MAX_BYTES = 20 * 1024 * 1024
IMG_RE = re.compile(r"!\[[^\]]*\]\((https?://[^)\s]+)\)")
EXT_BY_TYPE = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "image/bmp": ".bmp",
}
RASTER_EXT = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp")


def normalize_url(url):
    """Кириллицу в host (IDNA) и path (percent-encode) — urllib умеет только ASCII."""
    p = urllib.parse.urlsplit(url)
    host = p.hostname or ""
    try:
        host.encode("ascii")
    except UnicodeEncodeError:
        host = host.encode("idna").decode("ascii")
    return urllib.parse.urlunsplit(
        (
            p.scheme,
            f"{host}:{p.port}" if p.port else host,
            urllib.parse.quote(urllib.parse.unquote(p.path), safe="/%:@"),
            urllib.parse.quote(p.query, safe="=&?/:+%,"),
            p.fragment,
        )
    )


def fetch(url):
    req = urllib.request.Request(normalize_url(url), headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read(MAX_BYTES), r.headers.get("Content-Type", "").split(";")[
            0
        ].strip().lower()


def local_name(url, ctype, taken):
    # декодируем percent-encoding: «Схема ММ.png» вместо «_D0_A1_D1_85_…»
    name = urllib.parse.unquote(os.path.basename(urllib.parse.urlparse(url).path))
    name = re.sub(r"[^\w.\- ]", "_", name)[:120].strip()
    root, ext = os.path.splitext(name)
    if ext.lower() not in RASTER_EXT:
        ext = EXT_BY_TYPE.get(ctype, ".png")
    name = f"{root or 'img'}{ext}"
    cand, i = name, 1
    while cand in taken:
        cand = f"{root or 'img'}_{i}{ext}"
        i += 1
    taken.add(cand)
    return cand


def main():
    path = sys.argv[1]
    base = os.path.dirname(path) or "."
    with open(path, encoding="utf-8-sig") as f:
        md = f.read()

    urls = list(dict.fromkeys(m.group(1) for m in IMG_RE.finditer(md)))
    taken = set()
    saved = skipped = 0
    for url in urls:
        if os.path.splitext(urllib.parse.urlparse(url).path)[1].lower() == ".svg":
            skipped += 1  # VLM не читает SVG — оставляем URL
            continue
        try:
            data, ctype = fetch(url)
        except Exception:
            skipped += 1
            print(f"  [!] не скачано: {url[:90]}")
            continue
        if ctype not in EXT_BY_TYPE:
            skipped += 1  # не картинка (страница-ошибка и т.п.)
            continue
        name = local_name(url, ctype, taken)
        with open(os.path.join(base, name), "wb") as f:
            f.write(data)
        md = md.replace(url, name)
        saved += 1
        print(f"  [+] {name} ({len(data) // 1024} КБ)")

    if saved:
        with open(path, "w", encoding="utf-8") as f:
            f.write(md)
    print(f"fetch_images: скачано {saved}, пропущено {skipped} из {len(urls)} ссылок")


if __name__ == "__main__":
    main()
