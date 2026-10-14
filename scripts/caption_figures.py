#!/usr/bin/env python3
"""VLM-описания извлечённых фигур для поискового индекса.

После каждой ссылки на картинку в MD вставляет blockquote с плотным описанием
схемы (тип, узлы, связи, подписи) — делает графику находимой в текстовом RAG
(Cherry Studio не индексирует сами изображения). Идемпотентно: уже описанные
фигуры пропускаются. Требует LLM-сервис с mmproj (services.sh start).

Usage: caption_figures.py <file.md> [--limit N] [--api http://127.0.0.1:8080/v1]
Env:  CAPTION_API, CAPTION_MIN_BYTES (деф. 20000 — отсекает декоративные)
"""

import argparse, base64, json, mimetypes, os, pathlib, re, sys, time, urllib.request

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
    req = urllib.request.Request(
        f"{api}/chat/completions",
        data=json.dumps(
            {
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
                # thinking-модели (Qwen3.x): иначе reasoning попадает в content
                "chat_template_kwargs": {"enable_thinking": False},
            }
        ).encode(),
        headers={"Content-Type": "application/json"},
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
    a = ap.parse_args()
    min_bytes = int(os.environ.get("CAPTION_MIN_BYTES", "20000"))

    md = a.md.read_text(encoding="utf-8")
    lines = md.splitlines(keepends=True)
    try:
        alias = model_alias(a.api)
    except Exception:
        print("captioning: LLM-сервис не отвечает на " + a.api, file=sys.stderr)
        sys.exit(2)

    done = cached = skipped = tiny = 0
    described = {}
    out = []
    for i, ln in enumerate(lines):
        out.append(ln)
        refs = [m.group(1) for m in IMG_RE.finditer(ln)]
        if not refs:
            continue
        # уже описано? (следующая непустая строка — наш blockquote)
        j = len(out)
        nxt = next((s for s in out[j:] if s.strip()), "") if j < len(out) else ""
        ins = []
        for ref in refs:
            p = (a.md.parent / ref).resolve()
            if not p.is_file():
                continue
            if p.stat().st_size < min_bytes:
                tiny += 1
                continue
            if p in described:
                cached += 1
                continue
            if a.limit and done >= a.limit:
                continue
            ctx = re.sub(r"[*_#>`\[\]()|]", " ", "".join(lines[max(0, i - 6) : i]))
            txt, dt = describe(a.api, alias, p, ctx)
            if txt:
                ins.append(f"{MARK} {txt}\n")
                described[p] = True
                done += 1
                print(f"  + {p.name}: {len(txt)} симв. ({dt:.0f}с)", flush=True)
        out.extend(ins)

    a.md.write_text("".join(out), encoding="utf-8")
    print(
        f"captioning: описано {done}, повторных ссылок {cached}, "
        f"мелких пропущено {tiny} → {a.md.name}"
    )
    sys.exit(0)


if __name__ == "__main__":
    main()
