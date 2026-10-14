#!/usr/bin/env python3
"""VLM-описания извлечённых фигур для поискового индекса.

После каждой ссылки на картинку в MD вставляет blockquote с плотным описанием
схемы (тип, узлы, связи, подписи) — делает графику находимой в текстовом RAG
(Cherry Studio не индексирует сами изображения). Идемпотентно: уже описанные
фигуры пропускаются. MD перезаписывается после каждой фигуры (устойчивость
к прерыванию). Требует LLM-сервис с mmproj (services.sh start).

Usage: caption_figures.py <file.md> [--limit N] [--api http://127.0.0.1:8080/v1]
Env:  CAPTION_API, CAPTION_MIN_BYTES (деф. 20000 — отсекает декоративные)
"""

import argparse
import base64
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


def model_alias(api):
    with urllib.request.urlopen(f"{api}/models", timeout=10) as r:
        return json.load(r)["data"][0]["id"]


def describe(api, alias, img_path, ctx):
    data = img_path.read_bytes()
    b64 = base64.b64encode(data).decode()
    mime = mimetypes.guess_type(str(img_path))[0] or "image/jpeg"
    headers = {"Content-Type": "application/json"}
    # облачный провайдер (OpenAI-совместимый): ключ из окружения
    if os.environ.get("CAPTION_API_KEY"):
        headers["Authorization"] = f"Bearer {os.environ['CAPTION_API_KEY']}"
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
                    {
                        "type": "text",
                        "text": PROMPT.format(ctx=ctx[-500:] or "(нет)"),
                    },
                ],
            }
        ],
        "max_tokens": 600,
        "temperature": 0.1,
    }
    # thinking-модели (Qwen3.x): иначе reasoning попадает в content.
    # Облачные API строго валидируют поля — kwargs шлём только локальным llama.cpp
    local = any(h in api for h in ("127.0.0.1", "localhost", "0.0.0.0", "[::1]"))
    if local or os.environ.get("CAPTION_FORCE_KWARGS") == "1":
        body["chat_template_kwargs"] = {"enable_thinking": False}
    req = urllib.request.Request(
        f"{api}/chat/completions",
        data=json.dumps(body).encode(),
        headers=headers,
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
        print("captioning: LLM-сервис не отвечает на " + a.api, file=sys.stderr)
        sys.exit(2)

    def path_of(ref):
        p = (a.md.parent / ref).resolve()
        return p if (p.is_file() and p.stat().st_size >= min_bytes) else None

    def described(i):  # есть ли blockquote-описание у ссылок строки i
        j = i + 1
        while j < len(lines) and not lines[j].strip():
            j += 1
        return j < len(lines) and lines[j].startswith(MARK)

    # проход 1: очередь уникальных неописанных фигур (для M и resume)
    todo, seen = [], set()
    for i, ln in enumerate(lines):
        if described(i):
            continue
        for m in IMG_RE.finditer(ln):
            p = path_of(m.group(1))
            if p and p not in seen:
                seen.add(p)
                todo.append(p)
    m_total = len(todo)
    if not todo:
        print(f"captioning: новых фигур нет ({a.md.name})")
        return

    # проход 2: вставка описаний, MD пишем после каждой фигуры
    out, done, cached, tiny = [], 0, 0, 0
    described_now, durations = set(), []
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
            if p not in todo or p in described_now:
                cached += 1
                continue
            if a.limit and done >= a.limit:
                continue
            ctx = re.sub(r"[*_#>`\[\]()|]", " ", "".join(lines[max(0, i - 6) : i]))
            txt, dt = describe(a.api, alias, p, ctx)
            if txt:
                ins.append(f"{MARK} {txt}\n")
                described_now.add(p)
                done += 1
                durations.append(dt)
                avg = sum(durations) / len(durations)
                rem = max(0, m_total - done) * avg
                eta = (
                    f"; осталось ~{int(rem // 60)} мин {int(rem % 60)} с"
                    if len(durations) > 1
                    else ""
                )
                print(
                    f"  [{done}/{m_total}] {p.name}: {len(txt)} симв. ({dt:.0f}с{eta})",
                    flush=True,
                )
        out.extend(ins)
        if ins:  # инкрементальная запись: прерывание не теряет работу
            a.md.write_text("".join(out), encoding="utf-8")

    a.md.write_text("".join(out), encoding="utf-8")
    print(
        f"captioning: описано {done}/{m_total}, повторных ссылок {cached}, "
        f"мелких/битых пропущено {tiny} -> {a.md.name}"
    )


if __name__ == "__main__":
    main()
