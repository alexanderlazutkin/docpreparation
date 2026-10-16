#!/usr/bin/env python3
"""A/B block-OCR: surya-2 напрямую (:8510) vs PaddleOCR-VL через адаптер (:8511).

Шлёт BLOCK_PROMPT («OCR this block image to HTML.») на оба эндпоинта с одним
и тем же изображением (150 dpi рендер страницы), сохраняет ответы и печатает
сводку: время, символы, доля кириллицы. Использовано в приёмке бедки
cell--ll3um (адаптер-прокси PaddleOCR-VL → marker), USAGE §3.6.

Usage: python scripts/ab_block_ocr.py <image.png> [...]   (запускать при
поднятых сервисах: services.sh start --ocr-proxy --no-llm)
"""

from __future__ import annotations

import base64
import json
import re
import sys
import time
import urllib.request

SURYA = "http://127.0.0.1:8510/v1/chat/completions"
PROXY = "http://127.0.0.1:8511/v1/chat/completions"
BLOCK_PROMPT = "OCR this block image to HTML."


def cyr_share(text: str) -> float:
    letters = re.findall(r"[а-яёА-ЯЁa-zA-Z]", text)
    if not letters:
        return 0.0
    return sum(1 for c in letters if re.match(r"[а-яёА-ЯЁ]", c)) / len(letters)


def chat(url: str, img_b64: str, timeout: int = 600) -> tuple[str, float]:
    body = json.dumps(
        {
            "model": "datalab-to/surya-ocr-2",
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:image/png;base64,{img_b64}"},
                        },
                        {"type": "text", "text": BLOCK_PROMPT},
                    ],
                }
            ],
            "temperature": 0.0,
            "top_p": 0.1,
            "max_tokens": 4096,
        }
    ).encode()
    req = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/json"}
    )
    t0 = time.monotonic()
    with urllib.request.urlopen(req, timeout=timeout) as r:
        out = json.load(r)
    return out["choices"][0]["message"]["content"] or "", time.monotonic() - t0


def main() -> None:
    print(f"{'страница':10s} {'движок':8s} {'сек':>6s} {'симв':>6s} {'кир%':>6s}")
    for path in sys.argv[1:]:
        name = path.rsplit("/", 1)[-1].replace(".png", "")
        img = base64.b64encode(open(path, "rb").read()).decode()
        for label, url, outdir in (
            ("surya", SURYA, "surya"),
            ("paddle", PROXY, "paddle"),
        ):
            content, dt = chat(url, img)
            open(f"outbox/ocrtests/adapter_ab/{outdir}/{name}.html", "w").write(content)
            plain = re.sub(r"<[^>]+>", "", content)
            print(
                f"{name:10s} {label:8s} {dt:6.1f} {len(plain):6d} {cyr_share(plain) * 100:5.1f}%"
            )


if __name__ == "__main__":
    main()
