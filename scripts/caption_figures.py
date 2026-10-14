#!/usr/bin/env python3
"""VLM-описания извлечённых фигур для поискового индекса.

После каждой ссылки на картинку в MD вставляет blockquote с плотным описанием
схемы (тип, узлы, связи, подписи) — делает графику находимой в текстовом RAG
(Cherry Studio не индексирует сами изображения). Идемпотентно: уже описанные
фигуры пропускаются. MD перезаписывается по ходу сборки (устойчивость
к прерыванию).

Бэкенды:
  локальный llama-server (services.sh; thinking-off для Qwen3.x);
  облачный OpenAI-совместимый API (env CAPTION_API/CAPTION_MODEL/CAPTION_API_KEY;
  для cherry-шлюза ключ берётся из CHERRY_API_KEY) — запускать с --parallel N>1.

Usage: caption_figures.py <file.md> [--limit N] [--parallel N] [--api URL] [--model ID]
Env:  CAPTION_API, CAPTION_MODEL, CAPTION_API_KEY, CAPTION_PARALLEL,
      CAPTION_MIN_BYTES (деф. 20000 — отсекает декоративные)
"""

import argparse
import base64
import concurrent.futures
import json
import mimetypes
import os
import pathlib
import re
import sys
import time
import urllib.request

MARK = "> **VLM-описание:**"
IMG_RE = re.compile(r"!\[[^\]]*\]\(([^)\s]+\.(?:png|jpe?g|gif|webp))\)", re.I)

PROMPT = (
    "Ты готовишь описание изображения для полнотекстового поиска базы знаний "
    "по управлению данными. Опиши: 1) тип (схема/диаграмма/таблица/инфографика); "
    "2) что изображено и его роль в документе; 3) ВСЕ подписи, названия узлов, "
    "связи/стрелки, числа — дословно, насколько возможно. Учти контекст документа "
    "ниже. Плотный текст по-русски одним абзацем, без воды.\n\nКонтекст: {ctx}"
)

LOCAL_HOSTS = ("127.0.0.1", "localhost", "0.0.0.0", "[::1]")


def api_key(api):
    # явный ключ либо (только для cherry-шлюза) ключ из окружения opencode-конфига
    k = os.environ.get("CAPTION_API_KEY")
    if k:
        return k
    if "cherryin.net" in api:
        return os.environ.get("CHERRY_API_KEY")
    return None


def model_alias(api):
    req = urllib.request.Request(f"{api}/models")
    if api_key(api):
        req.add_header("Authorization", f"Bearer {api_key(api)}")
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)["data"][0]["id"]


def describe(api, alias, img_path, ctx):
    data = img_path.read_bytes()
    b64 = base64.b64encode(data).decode()
    mime = mimetypes.guess_type(str(img_path))[0] or "image/jpeg"
    headers = {"Content-Type": "application/json"}
    if api_key(api):
        headers["Authorization"] = f"Bearer {api_key(api)}"
    body = {
        "model": alias,
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:{mime};base64,{b64}"},
                    },
                    {"type": "text", "text": PROMPT.format(ctx=ctx[-500:] or "(нет)")},
                ],
            }
        ],
        "max_tokens": 600,
        "temperature": 0.1,
    }
    # thinking-модели (Qwen3.x): иначе reasoning попадает в content.
    # Облачные API строго валидируют поля — kwargs шлём только локальным llama.cpp
    local = any(h in api for h in LOCAL_HOSTS)
    if local or os.environ.get("CAPTION_FORCE_KWARGS") == "1":
        body["chat_template_kwargs"] = {"enable_thinking": False}
    req = urllib.request.Request(
        f"{api}/chat/completions", data=json.dumps(body).encode(), headers=headers
    )
    t0 = time.time()
    r = json.load(urllib.request.urlopen(req, timeout=600))
    m = r["choices"][0]["message"]
    txt = (m.get("content") or m.get("reasoning_content") or "").strip()
    if "</think>" in txt:  # страховка, если kwargs не подействовал
        txt = txt.split("</think>", 1)[1]
    txt = re.sub(r"\s*\n\s*", " ", txt).strip()
    return txt, time.time() - t0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("md", type=pathlib.Path)
    ap.add_argument(
        "--limit", type=int, default=0, help="описать не более N картинок (0 = все)"
    )
    ap.add_argument(
        "--parallel",
        type=int,
        default=int(os.environ.get("CAPTION_PARALLEL", "1")),
        help="параллельных запросов (облако: 6-8; локально: 1-2)",
    )
    ap.add_argument(
        "--api", default=os.environ.get("CAPTION_API", "http://127.0.0.1:8080/v1")
    )
    ap.add_argument(
        "--model",
        default=os.environ.get("CAPTION_MODEL", ""),
        help="имя модели; пусто = первая из /models (локальный llama-server)",
    )
    a = ap.parse_args()
    min_bytes = int(os.environ.get("CAPTION_MIN_BYTES", "20000"))

    lines = a.md.read_text(encoding="utf-8").splitlines(keepends=True)
    try:
        alias = a.model or model_alias(a.api)
    except Exception:
        print("captioning: LLM/VLM-сервис не отвечает на " + a.api, file=sys.stderr)
        sys.exit(2)

    def path_of(ref):
        p = (a.md.parent / ref).resolve()
        return p if (p.is_file() and p.stat().st_size >= min_bytes) else None

    def described(i):  # есть ли blockquote-описание у ссылок строки i
        j = i + 1
        while j < len(lines) and not lines[j].strip():
            j += 1
        return j < len(lines) and lines[j].startswith(MARK)

    # проход 1: очередь уникальных неописанных фигур (документный порядок)
    todo, seen, ctxs = [], set(), {}
    for i, ln in enumerate(lines):
        if described(i):
            continue
        for m in IMG_RE.finditer(ln):
            p = path_of(m.group(1))
            if p and p not in seen:
                seen.add(p)
                ctxs[p] = re.sub(
                    r"[*_#>`\[\]()|]", " ", "".join(lines[max(0, i - 6) : i])
                )
                todo.append(p)
    m_total = len(todo)
    if not todo:
        print(f"captioning: новых фигур нет ({a.md.name})")
        return
    if a.limit:
        todo = todo[: a.limit]
        m_total = len(todo)

    # проход 2: описания (параллельно при --parallel > 1), прогресс по готовности
    results, durs, done = {}, [], 0

    def work(p):
        return describe(a.api, alias, p, ctxs[p])

    def report(p, txt, dt):
        nonlocal done
        results[p] = txt
        durs.append(dt)
        done += 1
        avg = sum(durs) / len(durs)
        rem = max(0, len(todo) - done) * avg / max(1, a.parallel)
        eta = (
            f"; осталось ~{int(rem // 60)} мин {int(rem % 60)} с"
            if len(durs) > 1
            else ""
        )
        print(
            f"  [{done}/{m_total}] {p.name}: {len(txt)} симв. ({dt:.0f}с{eta})",
            flush=True,
        )

    if a.parallel > 1 and len(todo) > 1:
        with concurrent.futures.ThreadPoolExecutor(max_workers=a.parallel) as ex:
            futs = {ex.submit(work, p): p for p in todo}
            for f in concurrent.futures.as_completed(futs):
                p = futs[f]
                try:
                    txt, dt = f.result()
                except Exception as e:  # одна битая фигура не роняет пакет
                    print(f"  [!] {p.name}: {e}", flush=True)
                    continue
                report(p, txt, dt)
    else:
        for p in todo:
            txt, dt = work(p)
            report(p, txt, dt)

    # проход 3: сборка в документном порядке, запись по ходу
    out, tiny, cached = [], 0, 0
    for i, ln in enumerate(lines):
        out.append(ln)
        refs = [mm.group(1) for mm in IMG_RE.finditer(ln)]
        if not refs or described(i):
            continue
        ins = []
        for ref in refs:
            p = path_of(ref)
            if p is None:
                tiny += 1
                continue
            if p in results:
                if results[p]:
                    ins.append(f"{MARK} {results[p]}\n")
                continue
            if p in seen:
                cached += 1
        out.extend(ins)
        if ins:
            a.md.write_text("".join(out), encoding="utf-8")

    a.md.write_text("".join(out), encoding="utf-8")
    print(
        f"captioning: описано {len([1 for v in results.values() if v])}/{m_total}, "
        f"повторных ссылок {cached}, мелких/битых {tiny} -> {a.md.name}"
    )


if __name__ == "__main__":
    main()
