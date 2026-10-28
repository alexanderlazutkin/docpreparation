#!/usr/bin/env python3
"""ocr_proxy.py — OpenAI-совместимый прокси-адаптер OCR для marker.

Схема (USAGE §3.6, HANDOFF сл.шаг п.2): marker/surya клиент аттачится сюда
(SURYA_INFERENCE_URL=http://127.0.0.1:8511/v1) за алиасом surya, дальше
маршрутизация по промпту-контракту:

  layout / table_rec / high_accuracy_bbox (JSON со строкочным bbox)
      → surya-2 (:8510) без изменений (proxy-pass);
  block-OCR «OCR this block image to HTML.»
      → OCR_PROXY_BLOCK_ENGINE:
        surya (деф.) — pass-through;
        chandra (:8513) — промпт заменяется на официальный OCR_PROMPT
          chandra-ocr-2 (datalab-to/chandra, prompts.py), t=0,
          thinking выключен (шаблон Qwen3.5: без этого весь ответ уходит
          в reasoning_content), ответ — уже HTML, проходит как есть.
          Повтор-гвард: деградация повторами на ER-диаграммах (замерено
          DMRB Vol.1 p.164: одна mermaid-строка ×10+, обрыв max_tokens) —
          ретрай ×1 с запретом mermaid.

Эндпоинты: /health (нужные бэкенды живы), /v1/models (алиас surya),
/v1/chat/completions, /stats (счётчики маршрутов + block_engine).

Запуск: services.sh start --modelset 1 (прокси по умолчанию; раньше
требовался флаг --ocr-proxy, теперь no-op) или
services.sh start --modelset 2 (block-OCR -> chandra) —
или вручную: OCR_PROXY_PORT=8511 SURYA_URL=… CHANDRA_URL=…
python scripts/ocr_proxy.py.
Офлайн-проверки: python scripts/ocr_proxy.py --test
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx

SURYA_URL = os.environ.get("SURYA_URL", "http://127.0.0.1:8510").rstrip("/")
CHANDRA_URL = os.environ.get("CHANDRA_URL", "http://127.0.0.1:8513").rstrip("/")
PROXY_PORT = int(os.environ.get("OCR_PROXY_PORT", "8511"))
ALIAS = os.environ.get("SURYA_ALIAS", "datalab-to/surya-ocr-2")
CHANDRA_MODEL = os.environ.get("CHANDRA_ALIAS", "chandra-ocr-2")
HOST = os.environ.get("OCR_PROXY_HOST", "127.0.0.1")
UPSTREAM_TIMEOUT = float(os.environ.get("OCR_PROXY_TIMEOUT", "900"))
# Движок для block-OCR (единственный реальный потребитель в balanced marker —
# table-fallback, которому нужен <table>-HTML). Дефолт surya = чистый
# pass-through; Chandra — services.sh --modelset 2 (layout-HTML: таблицы/
# схемы/alt-описания).
BLOCK_ENGINE = os.environ.get("OCR_PROXY_BLOCK_ENGINE", "surya").lower()

# Контракт surya (surya/inference/prompts.py) — промпт block-OCR.
BLOCK_PROMPT = "OCR this block image to HTML."

# Официальный OCR_PROMPT chandra-ocr-2 (github.com/datalab-to/chandra,
# chandra/prompts.py): «OCR this image to HTML.» + общие правила вывода.
# Layout-вариант (data-bbox/data-label) тут не нужен — marker шлёт уже
# вырезанные блоки, нужны только HTML и таблицы.
CHANDRA_PROMPT = """OCR this image to HTML.
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

# Суффикс ретрая повтор-гварда: деградация mermaid на сложных диаграммах.
CHANDRA_NO_MERMAID_SUFFIX = (
    "\nDo not use mermaid: describe diagrams and charts in plain text instead."
)
REPEAT_LINE_MIN = 5  # строка (>=20 симв.) встретилась столько раз → деградация
REPEAT_LINE_LEN = 20

STATS = {
    "surya_layout": 0,
    "surya_table_rec": 0,
    "surya_fullpage": 0,
    "surya_block": 0,
    "surya_other": 0,
    "chandra_block": 0,
    "chandra_retries": 0,
    "errors": 0,
}
_stats_lock = threading.Lock()


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def route_prompt(prompt: str | None) -> str:
    """block-OCR → BLOCK_ENGINE (деф. surya); остальное → surya (safe default)."""
    return BLOCK_ENGINE if prompt == BLOCK_PROMPT else "surya"


def classify_prompt(prompt: str | None) -> str:
    """Тип запроса для /stats (факт-скорость: layout = готовые страницы)."""
    if prompt == BLOCK_PROMPT:
        return "block"
    if prompt is None:
        return "other"
    if prompt.startswith("Output the layout"):
        return "layout"
    if prompt.startswith("Output the table rows"):
        return "table_rec"
    if prompt.startswith("OCR this image to HTML"):
        return "fullpage"
    return "other"


def extract_prompt(messages: list) -> str | None:
    """Текст последнего user-сообщения (контент — список частей или строка)."""
    for msg in reversed(messages or []):
        if msg.get("role") != "user":
            continue
        content = msg.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and part.get("type") == "text":
                    return part.get("text")
        return None
    return None


def build_chandra_request(req: dict, prompt: str = CHANDRA_PROMPT) -> dict:
    """Промпт → официальный OCR_PROMPT chandra, t=0, thinking off,
    без response_format/logprobs (маркеры thinking-шаблона Qwen3.5)."""
    out_msgs = []
    for msg in req.get("messages") or []:
        content = msg.get("content")
        if isinstance(content, list):
            parts = []
            for part in content:
                if (
                    isinstance(part, dict)
                    and part.get("type") == "text"
                    and part.get("text") == BLOCK_PROMPT
                ):
                    parts.append({"type": "text", "text": prompt})
                else:
                    parts.append(part)
            msg = dict(msg, content=parts)
        out_msgs.append(msg)
    out = dict(req)
    out["messages"] = out_msgs
    out["model"] = CHANDRA_MODEL
    out["temperature"] = 0
    out["chat_template_kwargs"] = {"enable_thinking": False}
    for key in ("response_format", "logprobs", "top_logprobs"):
        out.pop(key, None)
    return out


def wrap_chandra_response(resp: dict) -> dict:
    """Ответ chandra — уже HTML (контракт marker block-OCR), проходит как есть."""
    resp["model"] = ALIAS
    return resp


def chandra_degenerate(resp: dict) -> bool:
    """Деградация повторами: обрыв по длине или строка ×REPEAT_LINE_MIN.

    Замерено на DMRB Vol.1 p.164 (ER-диаграмма → mermaid): одна строка
    «…||--o{ ORGANIZATION : "priced by"» ×10+, обрыв max_tokens за 157–205с.
    """
    try:
        choice = resp["choices"][0]
    except (KeyError, IndexError, TypeError):
        return False
    if choice.get("finish_reason") == "length":
        return True
    content = (choice.get("message") or {}).get("content") or ""
    counts: dict[str, int] = {}
    for ln in content.splitlines():
        ln = ln.strip()
        if len(ln) >= REPEAT_LINE_LEN:
            counts[ln] = counts.get(ln, 0) + 1
    return any(n >= REPEAT_LINE_MIN for n in counts.values())


def backend_ok(client: httpx.Client, base_url: str) -> bool:
    try:
        return client.get(f"{base_url}/health", timeout=3).status_code == 200
    except httpx.HTTPError:
        return False


class ProxyHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "docprep-ocr-proxy/0.1"

    def log_message(self, fmt, *args):  # свои строки в лог вместо stderr-шума
        pass

    def _send(self, code: int, payload: dict | bytes, content_type="application/json"):
        body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _error(self, code: int, msg: str):
        self._send(
            code, {"error": {"message": msg, "type": "proxy_error", "code": code}}
        )

    # ------------------------------------------------------------- GET ---
    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/health":
            with httpx.Client() as client:
                surya = backend_ok(client, SURYA_URL)
                chandra = (
                    backend_ok(client, CHANDRA_URL)
                    if BLOCK_ENGINE == "chandra"
                    else None
                )
            # chandra проверяется, только если это активный движок block-OCR
            required = [surya] + [b for b in (chandra,) if b is not None]
            if all(required):
                self._send(
                    200,
                    {
                        "status": "ok",
                        "surya": surya,
                        **({"chandra": chandra} if chandra is not None else {}),
                    },
                )
            else:
                self._send(
                    503,
                    {
                        "status": "unavailable",
                        "surya": surya,
                        **({"chandra": chandra} if chandra is not None else {}),
                    },
                )
        elif path == "/v1/models":
            self._send(
                200, {"object": "list", "data": [{"id": ALIAS, "object": "model"}]}
            )
        elif path == "/stats":
            with _stats_lock:
                self._send(200, dict(STATS, block_engine=BLOCK_ENGINE))
        else:
            self._error(404, f"not found: {path}")

    # ------------------------------------------------------------ POST ---
    def _chandra_call(self, req: dict) -> dict | None:
        """Block-OCR → chandra (:8513) + повтор-гвард (ретрай ×1 без mermaid)."""
        try:
            with httpx.Client(timeout=UPSTREAM_TIMEOUT) as client:
                upstream = client.post(
                    f"{CHANDRA_URL}/v1/chat/completions",
                    json=build_chandra_request(req),
                )
        except httpx.HTTPError as e:
            with _stats_lock:
                STATS["errors"] += 1
            self._error(502, f"chandra upstream unreachable: {e}")
            return None
        if upstream.status_code != 200:
            with _stats_lock:
                STATS["errors"] += 1
            log(f"chandra-> {upstream.status_code}: {upstream.text[:200]}")
            self._send(
                upstream.status_code, upstream.text, content_type="application/json"
            )
            return None

        resp = wrap_chandra_response(upstream.json())
        if chandra_degenerate(resp):
            with _stats_lock:
                STATS["chandra_retries"] += 1
            log("chandra-> деградация повторов, ретрай без mermaid")
            try:
                with httpx.Client(timeout=UPSTREAM_TIMEOUT) as client:
                    retry = client.post(
                        f"{CHANDRA_URL}/v1/chat/completions",
                        json=build_chandra_request(
                            req, CHANDRA_PROMPT + CHANDRA_NO_MERMAID_SUFFIX
                        ),
                    )
                if retry.status_code == 200:
                    resp = wrap_chandra_response(retry.json())
                    n_tok = (resp.get("usage") or {}).get("completion_tokens", "?")
                else:
                    log(f"chandra-> ретрай {retry.status_code}, отдаю первый ответ")
            except httpx.HTTPError as e:
                log(f"chandra-> ретрай недоступен ({e}), отдаю первый ответ")
        n_tok = (resp.get("usage") or {}).get("completion_tokens", "?")
        log(f"chandra-> 200 ({n_tok} tok)")
        return resp

    def do_POST(self):
        path = self.path.split("?")[0]
        if path != "/v1/chat/completions":
            self._error(404, f"not found: {path}")
            return
        try:
            length = int(self.headers.get("Content-Length", 0))
            req = json.loads(self.rfile.read(length) or b"{}")
        except (ValueError, json.JSONDecodeError) as e:
            self._error(400, f"bad request: {e}")
            return

        prompt = extract_prompt(req.get("messages"))
        route = route_prompt(prompt)
        kind = classify_prompt(prompt)
        t0 = time.monotonic()
        if route == "chandra":
            resp = self._chandra_call(req)
            if resp is not None:
                with _stats_lock:
                    STATS["chandra_block"] += 1
                self._send(200, resp)
            return
        try:
            with httpx.Client(timeout=UPSTREAM_TIMEOUT) as client:
                upstream = client.post(
                    f"{SURYA_URL}/v1/chat/completions",
                    content=json.dumps(req),
                    headers={"Content-Type": "application/json"},
                )
        except httpx.HTTPError as e:
            with _stats_lock:
                STATS["errors"] += 1
            self._error(502, f"{route} upstream unreachable: {e}")
            return

        ms = int((time.monotonic() - t0) * 1000)
        if upstream.status_code != 200:
            with _stats_lock:
                STATS["errors"] += 1
            log(
                f"{route:6s} -> {upstream.status_code} ({ms} ms) upstream: {upstream.text[:200]}"
            )
            self._send(
                upstream.status_code, upstream.text, content_type="application/json"
            )
            return

        with _stats_lock:
            STATS[f"{route}_{kind}"] += 1
        log(f"surya  -> 200 ({ms} ms)")
        self._send(200, upstream.content)


def main() -> None:
    if "--test" in sys.argv:
        sys.exit(run_selftest())
    global BLOCK_ENGINE
    if BLOCK_ENGINE not in ("surya", "chandra"):
        sys.exit(
            f"OCR_PROXY_BLOCK_ENGINE: ожидается surya|chandra, "
            f"получено '{BLOCK_ENGINE}'"
        )
    server = ThreadingHTTPServer((HOST, PROXY_PORT), ProxyHandler)
    server.daemon_threads = True
    log(
        f"ocr-proxy :{PROXY_PORT} (alias '{ALIAS}'): surya={SURYA_URL}, "
        f"chandra={CHANDRA_URL}, block-OCR -> {BLOCK_ENGINE}"
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


# ----------------------------------------------------------- selftest ---
def run_selftest() -> int:
    import base64
    import io

    from PIL import Image, ImageDraw

    failures = []

    def check(name, cond):
        print(f"  {'ok' if cond else 'FAIL'}  {name}")
        if not cond:
            failures.append(name)

    # 1. Маршрутизация по промпту (деф. BLOCK_ENGINE=surya — pass-through)
    check(
        "block prompt -> block-engine (деф. surya)",
        route_prompt(BLOCK_PROMPT) == "surya",
    )
    global BLOCK_ENGINE
    BLOCK_ENGINE = "chandra"
    check(
        "block prompt -> chandra при OCR_PROXY_BLOCK_ENGINE=chandra",
        route_prompt(BLOCK_PROMPT) == "chandra",
    )
    BLOCK_ENGINE = "surya"
    check(
        "layout prompt -> surya",
        route_prompt("Output the layout of this image as JSON.") == "surya",
    )
    check(
        "table_rec prompt -> surya",
        route_prompt("Output the table rows then columns as JSON.") == "surya",
    )
    check(
        "high_accuracy prompt -> surya",
        route_prompt("OCR this image to HTML.") == "surya",
    )
    check(
        "unknown prompt -> surya (safe default)",
        route_prompt("что-то новое") == "surya",
    )
    check("empty -> surya", route_prompt(None) == "surya")

    # 2. extract_prompt: список частей и строковый контент
    img_part = {"type": "image_url", "image_url": {"url": "data:image/png;base64,xxx"}}
    msgs = [
        {"role": "user", "content": [img_part, {"type": "text", "text": BLOCK_PROMPT}]}
    ]
    check("extract из частей", extract_prompt(msgs) == BLOCK_PROMPT)
    check(
        "extract из строки", extract_prompt([{"role": "user", "content": "hi"}]) == "hi"
    )
    check("extract пусто", extract_prompt([]) is None)

    # 3. build_chandra_request / wrap / повтор-гвард
    req = {
        "model": ALIAS,
        "messages": msgs,
        "temperature": 0.3,
        "top_p": 0.1,
        "max_tokens": 500,
        "logprobs": True,
        "response_format": {"type": "json_schema", "json_schema": {"schema": {}}},
    }
    creq = build_chandra_request(req)
    ctxt = extract_prompt(creq["messages"])
    check("chandra промпт заменён на OCR_PROMPT", ctxt == CHANDRA_PROMPT)
    check(
        "chandra thinking off",
        creq.get("chat_template_kwargs") == {"enable_thinking": False},
    )
    check("chandra t=0", creq["temperature"] == 0)
    check(
        "chandra response_format/logprobs сброшены",
        "response_format" not in creq and "logprobs" not in creq,
    )
    check(
        "chandra image-часть сохранена",
        creq["messages"][0]["content"][0] == img_part,
    )
    check(
        "chandra суффикс ретрая",
        extract_prompt(
            build_chandra_request(req, CHANDRA_PROMPT + CHANDRA_NO_MERMAID_SUFFIX)[
                "messages"
            ]
        )
        == CHANDRA_PROMPT + CHANDRA_NO_MERMAID_SUFFIX,
    )
    cresp = {
        "model": CHANDRA_MODEL,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": "<p>HTML</p>"},
                "finish_reason": "stop",
            }
        ],
        "usage": {"completion_tokens": 4},
    }
    cw = wrap_chandra_response(cresp)
    check(
        "chandra wrap: content проходит как есть",
        cw["choices"][0]["message"]["content"] == "<p>HTML</p>",
    )
    check("chandra wrap model -> алиас", cw["model"] == ALIAS)
    ok_resp = {
        "choices": [{"finish_reason": "stop", "message": {"content": "<p>a</p>" * 3}}]
    }
    check("повтор-гвард: чистый ответ не деградация", not chandra_degenerate(ok_resp))
    len_resp = {
        "choices": [{"finish_reason": "length", "message": {"content": "<p>a</p>"}}]
    }
    check("повтор-гвард: обрыв по длине", chandra_degenerate(len_resp))
    rep_line = "PRICE_COMPONENT ||--o{ ORGANIZATION : priced by"
    rep_resp = {
        "choices": [
            {"finish_reason": "stop", "message": {"content": "\n".join([rep_line] * 7)}}
        ]
    }
    check("повтор-гвард: строка ×7 → деградация", chandra_degenerate(rep_resp))
    short_resp = {
        "choices": [
            {"finish_reason": "stop", "message": {"content": "\n".join(["x" * 5] * 7)}}
        ]
    }
    check(
        "повтор-гвард: короткие строки не триггерят", not chandra_degenerate(short_resp)
    )
    check("повтор-гвард: пустой ответ не падает", not chandra_degenerate({}))

    # 6.Сквозной HTTP-тест с мок-бэкендами (route + подмена + ответ)
    def make_png_b64() -> str:
        img = Image.new("RGB", (64, 32), "white")
        d = ImageDraw.Draw(img)
        d.text((4, 8), "тест", fill="black")
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return base64.b64encode(buf.getvalue()).decode()

    from http.server import BaseHTTPRequestHandler as H

    rep_line = "PRICE_COMPONENT ||--o{ ORGANIZATION : priced by"

    class Mock(H):
        def do_GET(self):
            raw = json.dumps({"status": "ok"}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            txt = extract_prompt(body["messages"])
            if txt == CHANDRA_PROMPT:
                # деградация повторами (mermaid) → прокси сделает ретрай
                content = "\n".join([rep_line] * 7)
            elif txt.startswith(CHANDRA_PROMPT):
                # ретрай с суффиксом no-mermaid → чистый ответ
                content = "<p>CHANDRA-ЧИСТО</p>"
            else:
                content = "МОК-LAYOUT"
            payload = {
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": content},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"completion_tokens": 3},
                "model": "mock",
            }
            raw = json.dumps(payload).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

    import socket

    def free_port() -> int:
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            return s.getsockname()[1]

    global SURYA_URL, CHANDRA_URL, PROXY_PORT
    surya_port, chandra_port = free_port(), free_port()
    mock_surya = ThreadingHTTPServer(("127.0.0.1", surya_port), Mock)
    mock_chandra = ThreadingHTTPServer(("127.0.0.1", chandra_port), Mock)
    import threading as _t

    for srv in (mock_surya, mock_chandra):
        _t.Thread(target=srv.serve_forever, daemon=True).start()

    SURYA_URL = f"http://127.0.0.1:{surya_port}"
    CHANDRA_URL = f"http://127.0.0.1:{chandra_port}"
    proxy_port = free_port()
    PROXY_PORT = proxy_port
    proxy = ThreadingHTTPServer(("127.0.0.1", proxy_port), ProxyHandler)
    _t.Thread(target=proxy.serve_forever, daemon=True).start()
    time.sleep(0.2)

    img_b64 = make_png_b64()

    def chat(prompt: str) -> tuple[int, str]:
        payload = {
            "model": ALIAS,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:image/png;base64,{img_b64}"},
                        },
                        {"type": "text", "text": prompt},
                    ],
                }
            ],
            "temperature": 0.0,
            "top_p": 0.1,
            "max_tokens": 128,
        }
        r = httpx.post(
            f"http://127.0.0.1:{proxy_port}/v1/chat/completions",
            json=payload,
            timeout=30,
        )
        try:
            content = r.json()["choices"][0]["message"]["content"]
        except Exception:
            content = ""
        return r.status_code, content

    code, content = chat("Output the layout of this image as JSON.")
    check("e2e: layout проходит насквозь", code == 200 and content == "МОК-LAYOUT")

    code, content = chat(BLOCK_PROMPT)
    check(
        "e2e: block (деф.) -> surya насквозь", code == 200 and content == "МОК-LAYOUT"
    )

    BLOCK_ENGINE = "chandra"
    code, content = chat(BLOCK_PROMPT)
    check(
        "e2e: block (chandra) -> повтор-гвард ретрай -> чистый HTML",
        code == 200 and content == "<p>CHANDRA-ЧИСТО</p>",
    )
    code, content = chat("Output the layout of this image as JSON.")
    check(
        "e2e: layout идёт мимо chandra на surya",
        code == 200 and content == "МОК-LAYOUT",
    )

    r = httpx.get(f"http://127.0.0.1:{proxy_port}/health", timeout=5)
    h = r.json()
    check(
        "e2e: /health в режиме chandra (surya+chandra)",
        r.status_code == 200 and h.get("chandra") is True,
    )

    with _stats_lock:
        check(
            "e2e: счётчики маршрутов (по типам)",
            STATS["surya_layout"] >= 1
            and STATS["surya_block"] >= 1
            and STATS["chandra_block"] >= 1
            and STATS["chandra_retries"] >= 1
            and STATS["errors"] == 0,
        )

    r = httpx.get(f"http://127.0.0.1:{proxy_port}/v1/models", timeout=5)
    check(
        "e2e: /v1/models отдаёт алиас",
        r.status_code == 200 and r.json()["data"][0]["id"] == ALIAS,
    )
    r = httpx.get(f"http://127.0.0.1:{proxy_port}/stats", timeout=5)
    check(
        "e2e: /stats содержит block_engine",
        r.status_code == 200 and r.json().get("block_engine") == "chandra",
    )

    for srv in (mock_surya, mock_chandra, proxy):
        srv.shutdown()

    print(f"\n{'FAIL: ' + str(failures) if failures else 'Все проверки пройдены'}")
    return 1 if failures else 0


if __name__ == "__main__":
    main()
