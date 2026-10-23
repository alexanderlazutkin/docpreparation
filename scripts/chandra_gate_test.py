#!/usr/bin/env python3
"""chandra_gate_test.py — гейт-тесты Chandra OCR 2 (modelset 2) на живых страницах.

Гейты приёмки (результаты 2026-10-23, USAGE §3.7): кириллица >=90%,
таблицы не теряются, скорость <=2x surya (10,5 с/стр). Рендерит страницы
PDF -> PNG, шлёт официальный OCR_LAYOUT_PROMPT (github.com/datalab-to/chandra,
chandra/prompts.py) с thinking off (шаблон Qwen3.5: без enable_thinking=false
весь ответ уходит в reasoning_content), сохраняет HTML + текст + тайминги.

Usage:
  uv run python scripts/chandra_gate_test.py --pages "pdf.pdf:10-15" [...] [--workers 4]
      --workers N — параллельные запросы (throughput-тест; деф. 1 = латентность)
  Требует поднятого chandra: ./services.sh start --modelset 2 --no-llm
"""

from __future__ import annotations

import argparse
import base64
import html as html_mod
import json
import re
import subprocess
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

# Официальный OCR_LAYOUT_PROMPT chandra-ocr-2 (datalab-to/chandra, prompts.py)
PROMPT = """OCR this image to HTML, arranged as layout blocks.  Each layout block should be a div with the data-bbox attribute representing the bounding box of the block in x0 y0 x1 y1 format.  Bboxes are normalized 0-1000. The data-label attribute is the label for the block.
Use the following labels:
- Caption
- Footnote
- Equation-Block
- List-Group
- Page-Header
- Page-Footer
- Image
- Section-Header
- Table
- Text
- Complex-Block
- Code-Block
- Form
- Table-Of-Contents
- Figure
- Chemical-Block
- Diagram
- Bibliography
- Blank-Page
Only use these tags ['math', 'table', 'h1', 'h2', 'h3', 'h4', 'h5', 'input', 'span', 'tbody', 'small', 'caption', 'strong', 'thead', 'code', 'chem'], and these attributes ['class', 'colspan', 'rowspan', 'display', 'checked', 'type', 'border', 'value', 'style', 'href', 'align', 'data-bbox', 'data-label'].
Guidelines:
* Inline math: Surround math with <math>...</math> tags. Math expressions should be rendered in KaTeX-compatible LaTeX. Use display for block math.
* Tables: Use colspan and rowspan attributes to match table structure.
* Formatting: Maintain consistent formatting with the image, including spacing, indentation, subscripts/superscripts, and special characters.
* Images: Include a description of any images in the alt attribute of an <img> tag. Do not fill out the src property. Describe in detail inside the div tag. Also convert charts to high fidelity data, and convert diagrams to mermaid.
* Forms: Mark checkboxes and radio buttons properly.
* Text: join lines together properly into paragraphs using <p>...</p> tags.  Use <br> tags for line breaks within paragraphs, but only when absolutely necessary to maintain meaning.
* Chemistry: Use <chem>...</chem> tags for chemical formulas with reactive SMILES.
* Lists: Preserve indents and proper list markers.
* Use the simplest possible HTML structure that accurately represents the content of the block.
* Make sure the text is accurate and easy for a human to read and interpret.  Reading order should be correct and natural."""

URL = "http://127.0.0.1:8513/v1/chat/completions"


def parse_pages(spec: str) -> tuple[str, int, int]:
    """'pdf.pdf:S[-E]' -> (pdf, S, E); E по умолчанию = S."""
    pdf, _, rng = spec.rpartition(":")
    if not pdf or not rng:
        raise ValueError(spec)
    if "-" in rng:
        s, e = rng.split("-", 1)
    else:
        s = e = rng
    return pdf, int(s), int(e or s)


def render(pdf: str, page: int, outdir: Path) -> Path:
    prefix = outdir / Path(pdf).stem[:24].replace(" ", "_")
    subprocess.run(
        [
            "pdftoppm",
            "-png",
            "-r",
            "100",
            "-f",
            str(page),
            "-l",
            str(page),
            pdf,
            str(prefix),
        ],
        check=True,
    )
    cands = list(outdir.glob(f"{prefix.name}-*{page:04d}.png")) + list(
        outdir.glob(f"{prefix.name}-{page}.png")
    )
    if not cands:
        raise RuntimeError(f"pdftoppm не создал PNG для стр. {page}")
    return cands[0]


def html_to_text(h: str) -> str:
    return html_mod.unescape(re.sub(r"<[^>]+>", "\n", h))


def ocr(png: Path) -> dict:
    img = base64.b64encode(png.read_bytes()).decode()
    body = {
        "model": "chandra-ocr-2",
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/png;base64,{img}"},
                    },
                    {"type": "text", "text": PROMPT},
                ],
            }
        ],
        "max_tokens": 6000,
        "temperature": 0,
        "chat_template_kwargs": {"enable_thinking": False},
    }
    req = urllib.request.Request(
        URL,
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
    )
    t0 = time.time()
    r = json.loads(urllib.request.urlopen(req, timeout=1200).read())
    dt = time.time() - t0
    msg = r["choices"][0]["message"]
    return {
        "page": png.stem,
        "sec": round(dt, 1),
        "tok": r["usage"]["completion_tokens"],
        "finish": r["choices"][0]["finish_reason"],
        "content": msg["content"],
        "reasoning": msg.get("reasoning_content") or "",
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pages", nargs="+", required=True, help="PDF:S[-E] ...")
    ap.add_argument("--workers", type=int, default=1, help="параллелизм (деф. 1)")
    ap.add_argument("--outdir", default="/tmp/chandra_gate", help="каталог результатов")
    args = ap.parse_args()

    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    jobs = []
    for spec in args.pages:
        pdf, s, e = parse_pages(spec)
        for page in range(s, e + 1):
            jobs.append(render(pdf, page, outdir))

    t0 = time.time()
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        results = list(ex.map(ocr, jobs))
    total = time.time() - t0

    for r in results:
        stem = outdir / r["page"]
        stem.with_suffix(".html").write_text(r["content"])
        stem.with_suffix(".txt").write_text(html_to_text(r["content"]))
        if r["reasoning"]:
            stem.with_suffix(".reasoning").write_text(r["reasoning"])
        warn = (
            " [WARN: reasoning не пуст — thinking не отключён]"
            if r["reasoning"]
            else ""
        )
        trunc = " [ОБРЫВ max_tokens]" if r["finish"] == "length" else ""
        print(f"{r['page']}: {r['sec']}s {r['tok']}tok fin={r['finish']}{trunc}{warn}")

    sec_pages = [r["sec"] for r in results]
    print(
        f"\nитог: {len(results)} стр, {total:.1f}s всего, "
        f"throughput {total / len(results):.1f} с/стр "
        f"(латентность: медиана {sorted(sec_pages)[len(sec_pages) // 2]:.1f}s); "
        f"эталон surya 10,5 с/стр (гейт: <=2x)"
    )


if __name__ == "__main__":
    main()
