#!/usr/bin/env python3
"""ocr_proxy.py — OpenAI-совместимый прокси-адаптер OCR для marker.

Схема (USAGE §3.6, HANDOFF сл.шаг п.2): marker/surya клиент аттачится сюда
(SURYA_INFERENCE_URL=http://127.0.0.1:8511/v1) за алиасом surya, дальше
маршрутизация по промпту-контракту:

  layout / table_rec / high_accuracy_bbox (JSON со строкочным bbox)
      → surya-2 (:8510) без изменений (proxy-pass);
  block-OCR «OCR this block image to HTML.»
      → PaddleOCR-VL (:8512): промпт заменяется на <|ocr|>, temperature=0,
        response_format/logprobs отбрасываются, plain-text ответ оборачивается
        в <p>…</p> на строку (HTML-парсер surya/marker).

Эндпоинты: /health (оба бэкенда живы), /v1/models (алиас surya),
/v1/chat/completions, /stats (счётчики маршрутов).

Запуск: services.sh start --ocr-proxy (или вручную: OCR_PROXY_PORT=8511 \
SURYA_URL=… PADDLE_URL=… python scripts/ocr_proxy.py).
Офлайн-проверки: python scripts/ocr_proxy.py --test
"""

from __future__ import annotations

import html
import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx

SURYA_URL = os.environ.get("SURYA_URL", "http://127.0.0.1:8510").rstrip("/")
PADDLE_URL = os.environ.get("PADDLE_URL", "http://127.0.0.1:8512").rstrip("/")
PROXY_PORT = int(os.environ.get("OCR_PROXY_PORT", "8511"))
ALIAS = os.environ.get("SURYA_ALIAS", "datalab-to/surya-ocr-2")
PADDLE_MODEL = os.environ.get("PADDLE_ALIAS", "PaddleOCR-VL-1.6")
HOST = os.environ.get("OCR_PROXY_HOST", "127.0.0.1")
UPSTREAM_TIMEOUT = float(os.environ.get("OCR_PROXY_TIMEOUT", "600"))
PADDLE_PROMPT = "<|ocr|>"
# Движок для block-OCR (единственный реальный потребитель в balanced marker —
# table-fallback, которому нужен <table>-HTML; Paddle даёт plain-строки →
# clean_table_html отклоняет → таблица теряется, замерено на DMRB Vol.1
# стр. 162, 2026-10-16). Дефолт surya = чистый pass-through; Paddle включается
# явно: OCR_PROXY_BLOCK_ENGINE=paddle (эксперименты, §3.6 USAGE).
BLOCK_ENGINE = os.environ.get("OCR_PROXY_BLOCK_ENGINE", "surya").lower()

# Контракт surya (surya/inference/prompts.py) — промпт block-OCR.
BLOCK_PROMPT = "OCR this block image to HTML."

STATS = {
    "surya_layout": 0, "surya_table_rec": 0, "surya_fullpage": 0,
    "surya_block": 0, "surya_other": 0, "paddle_block": 0, "errors": 0,
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


def to_html(text: str | None) -> str:
    """Plain-text ответ Paddle → HTML-блоки: строка → <p>…</p> (экранирование)."""
    lines = [ln.strip() for ln in (text or "").splitlines()]
    return "".join(f"<p>{html.escape(ln)}</p>" for ln in lines if ln)


def build_paddle_request(req: dict) -> dict:
    """Подмена промпта на <|ocr|>, t=0, без response_format/logprobs."""
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
                    parts.append({"type": "text", "text": PADDLE_PROMPT})
                else:
                    parts.append(part)
            msg = dict(msg, content=parts)
        out_msgs.append(msg)
    out = dict(req)
    out["messages"] = out_msgs
    out["model"] = PADDLE_MODEL
    out["temperature"] = 0
    for key in ("response_format", "logprobs", "top_logprobs"):
        out.pop(key, None)
    return out


def wrap_paddle_response(resp: dict) -> dict:
    """content → <p>-HTML; usage/finish_reason сохраняются."""
    try:
        content = resp["choices"][0]["message"]["content"] or ""
        resp["choices"][0]["message"]["content"] = to_html(content)
    except (KeyError, IndexError, TypeError):
        pass
    resp["model"] = ALIAS
    return resp


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
                paddle = backend_ok(client, PADDLE_URL)
            if surya and paddle:
                self._send(200, {"status": "ok", "surya": True, "paddle": True})
            else:
                self._send(
                    503, {"status": "unavailable", "surya": surya, "paddle": paddle}
                )
        elif path == "/v1/models":
            self._send(
                200, {"object": "list", "data": [{"id": ALIAS, "object": "model"}]}
            )
        elif path == "/stats":
            with _stats_lock:
                self._send(200, dict(STATS))
        else:
            self._error(404, f"not found: {path}")

    # ------------------------------------------------------------ POST ---
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
        try:
            with httpx.Client(timeout=UPSTREAM_TIMEOUT) as client:
                if route == "surya":
                    upstream = client.post(
                        f"{SURYA_URL}/v1/chat/completions",
                        content=json.dumps(req),
                        headers={"Content-Type": "application/json"},
                    )
                else:
                    upstream = client.post(
                        f"{PADDLE_URL}/v1/chat/completions",
                        json=build_paddle_request(req),
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
        if route == "surya":
            log(f"surya  -> 200 ({ms} ms)")
            self._send(200, upstream.content)
        else:
            resp = wrap_paddle_response(upstream.json())
            n_tok = (resp.get("usage") or {}).get("completion_tokens", "?")
            log(f"paddle -> 200 ({ms} ms, {n_tok} tok)")
            self._send(200, resp)


def main() -> None:
    if "--test" in sys.argv:
        sys.exit(run_selftest())
    global BLOCK_ENGINE
    if BLOCK_ENGINE not in ("surya", "paddle"):
        sys.exit(
            f"OCR_PROXY_BLOCK_ENGINE: ожидается surya|paddle, получено '{BLOCK_ENGINE}'"
        )
    server = ThreadingHTTPServer((HOST, PROXY_PORT), ProxyHandler)
    server.daemon_threads = True
    log(
        f"ocr-proxy :{PROXY_PORT} (alias '{ALIAS}'): surya={SURYA_URL}, "
        f"paddle={PADDLE_URL}, block-OCR -> {BLOCK_ENGINE}"
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
    BLOCK_ENGINE = "paddle"
    check(
        "block prompt -> paddle при OCR_PROXY_BLOCK_ENGINE=paddle",
        route_prompt(BLOCK_PROMPT) == "paddle",
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

    # 3. to_html
    check("to_html строка", to_html("строка 1") == "<p>строка 1</p>")
    check(
        "to_html мультистрока",
        to_html("a\n\n  b  \nc") == "<p>a</p><p>b</p><p>c</p>",
    )
    check("to_html экранирование", to_html("a<b>&c") == "<p>a&lt;b&gt;&amp;c</p>")
    check("to_html пусто", to_html("") == "" and to_html(None) == "")

    # 4. build_paddle_request
    req = {
        "model": ALIAS,
        "messages": msgs,
        "temperature": 0.3,
        "top_p": 0.1,
        "max_tokens": 500,
        "logprobs": True,
        "response_format": {"type": "json_schema", "json_schema": {"schema": {}}},
    }
    preq = build_paddle_request(req)
    ptxt = extract_prompt(preq["messages"])
    check("paddle промпт заменён на <|ocr|>", ptxt == PADDLE_PROMPT)
    check("paddle t=0", preq["temperature"] == 0)
    check("paddle response_format сброшен", "response_format" not in preq)
    check(
        "paddle logprobs сброшен", "logprobs" not in preq and "top_logprobs" not in preq
    )
    check("paddle max_tokens сохранён", preq["max_tokens"] == 500)
    check(
        "paddle image-часть сохранена",
        preq["messages"][0]["content"][0] == img_part,
    )
    check("исходный запрос не изменён", req["temperature"] == 0.3)

    # 5. wrap_paddle_response
    resp = {
        "id": "x",
        "model": PADDLE_MODEL,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": "Тест\nВторая"},
                "finish_reason": "stop",
            }
        ],
        "usage": {"completion_tokens": 5},
    }
    w = wrap_paddle_response(resp)
    check(
        "wrap content -> <p>…</p>",
        w["choices"][0]["message"]["content"] == "<p>Тест</p><p>Вторая</p>",
    )
    check("wrap model -> алиас", w["model"] == ALIAS)
    check("wrap usage сохранён", w["usage"]["completion_tokens"] == 5)

    # 6.Сквозной HTTP-тест с мок-бэкендами (route + подмена + ответ)
    def make_png_b64() -> str:
        img = Image.new("RGB", (64, 32), "white")
        d = ImageDraw.Draw(img)
        d.text((4, 8), "тест", fill="black")
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return base64.b64encode(buf.getvalue()).decode()

    from http.server import BaseHTTPRequestHandler as H

    class Mock(H):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            txt = extract_prompt(body["messages"])
            content = "МОК-LAYOUT" if txt != PADDLE_PROMPT else f"ПОДДЕРЖКА {txt}"
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

    global SURYA_URL, PADDLE_URL, PROXY_PORT
    surya_port, paddle_port = free_port(), free_port()
    mock_surya = ThreadingHTTPServer(("127.0.0.1", surya_port), Mock)
    mock_paddle = ThreadingHTTPServer(("127.0.0.1", paddle_port), Mock)
    import threading as _t

    for srv in (mock_surya, mock_paddle):
        _t.Thread(target=srv.serve_forever, daemon=True).start()

    SURYA_URL = f"http://127.0.0.1:{surya_port}"
    PADDLE_URL = f"http://127.0.0.1:{paddle_port}"
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

    BLOCK_ENGINE = "paddle"
    code, content = chat(BLOCK_PROMPT)
    check(
        "e2e: block (paddle) -> <p>-обёртка",
        code == 200 and content == "<p>ПОДДЕРЖКА &lt;|ocr|&gt;</p>",
    )

    with _stats_lock:
        check("e2e: счётчики маршрутов (по типам)",
              STATS["surya_layout"] >= 1 and STATS["paddle_block"] >= 1
              and STATS["surya_block"] >= 1 and STATS["errors"] == 0)

    r = httpx.get(f"http://127.0.0.1:{proxy_port}/v1/models", timeout=5)
    check(
        "e2e: /v1/models отдаёт алиас",
        r.status_code == 200 and r.json()["data"][0]["id"] == ALIAS,
    )

    for srv in (mock_surya, mock_paddle, proxy):
        srv.shutdown()

    print(f"\n{'FAIL: ' + str(failures) if failures else 'Все проверки пройдены'}")
    return 1 if failures else 0


if __name__ == "__main__":
    main()
