#!/usr/bin/env python3
"""VLM-описания извлечённых фигур для поискового индекса.

После каждой ссылки на картинку в MD вставляет blockquote с плотным описанием
схемы (тип, узлы, связи, подписи) — делает графику находимой в текстовом RAG
(Cherry Studio не индексирует сами изображения). Идемпотентно: уже описанные
фигуры пропускаются. MD перезаписывается по ходу сборки (устойчивость
к прерыванию).

Бэкенды:
  локальный llama-server (services.sh; thinking-off для Qwen3.x);
  облачный OpenAI-совместимый API (env CAPTION_API/CAPTION_MODEL; ключ —
  LLM_API_KEY) — запускать с --parallel N>1.

Usage: caption_figures.py <file.md> [--limit N] [--parallel N] [--api URL] [--model ID]
                          [--lang ru|en] [--structured]
Env:  CAPTION_API, CAPTION_MODEL, LLM_API_KEY, CAPTION_PARALLEL,
      CAPTION_MIN_BYTES (деф. 20000 — отсекает декоративные),
      CAPTION_LANG (ru|en; деф. ru — язык описаний),
      CAPTION_PROMPT (свой промпт-шаблон; плейсхолдеры {ctx} — контекст,
      {lang} — язык слова «на русском/английском»; деф. — встроенный ниже),
      CAPTION_STRUCTURED (=1 — структурированные описания: prose + Mermaid
      + список связей для схем, pipe-MD реконструкция для таблиц; пилот
      этапа 0: 8/8 валидного Mermaid. Деф. 0 — prose-only.
      Валидация Mermaid — mmdc (npx -y @mermaid-js/mermaid-cli, нужен node;
      puppeteer --no-sandbox), ретрай x1 с текстом ошибки, фолбэк prose)
"""

import argparse
import base64
import concurrent.futures
import contextlib
import json
import mimetypes
import os
import pathlib
import re
import subprocess
import sys
import tempfile
import time
import urllib.request

# Маркер вставки — ПО ЯЗЫКУ описаний; described() знает ОБА маркера
# (идемпотентность на файлах, описанных в другом языке/до переименования)
MARK_RU = "> **VLM-описание:**"
MARK_EN = "> **VLM-description:**"
MARKS = (MARK_RU, MARK_EN)
# [^)]*? — допускаем пробелы в имени (вэб-имена вида «Схема ММ.png»)
IMG_RE = re.compile(r"!\[[^\]]*\]\(([^)]*?\.(?:png|jpe?g|gif|webp))\)", re.IGNORECASE)


def strip_descriptions(lines):
    """Вырезать blockquote-описания (произв. этого скрипта, ru+en, prose и
    structured: маркер + ```mermaid-фенс + «Связи:/Edges:» + буллеты + pipe-
    таблица + «> ⚠ …»), включая edge-случай «маркер на той же строке после
    картинки» (в ранних прогонах описание клеилось без \\n). Грамматика
    вставки — join("\\n\\n"), части разделены пустыми строками.
    -> (новые строки, сколько блоков снято).
    Прим.: строки-«хвосты» грамматики (свой «|»-таблица/буллет документа
    вплотную к блоку) могут быть захвачены — перед применением делай бэкап.
    """
    out, removed, i = [], 0, 0
    while i < len(lines):
        ln = lines[i]
        pos = [ln.find(m) for m in MARKS if m in ln]
        mpos = min(pos) if pos else None
        if mpos is None:
            out.append(ln)
            i += 1
            continue
        if mpos > 0:  # маркер не с начала строки
            if not IMG_RE.search(ln[:mpos]):  # не после картинки — чужой текст
                out.append(ln)
                i += 1
                continue
            prefix = ln[:mpos].rstrip()  # картинку сохранить, хвост строки drop
            out.append(prefix + ("\n" if ln.endswith("\n") else ""))
        removed += 1
        i = _consume_block(lines, i + 1, out)
    return out, removed


def _consume_block(lines, i, out):
    """Съесть грамматику блока описания с позиции i; пустые строки перед
    не-грамматикой вернуть в out (разделитель документа). -> новый i."""
    BULLETS = ("-", "*", "•")
    in_list = False
    pending_blanks = []
    while i < len(lines):
        cur = lines[i]
        if not cur.strip():
            pending_blanks.append(cur)
            i += 1
            continue
        s = cur.strip()
        cont = (
            cur.startswith((">", "```mermaid", "|"))  # blockquote/фенс/таблица
            or (in_list and (s.startswith(BULLETS) or re.match(r"^\d+[.)]", s)))
            or s.startswith(("Связи:", "Edges:"))
            or (s.endswith(":") and s.rstrip(":").strip() in ("Связи", "Edges"))
        )
        if not cont:
            break
        pending_blanks = []
        if cur.startswith("```mermaid"):
            i += 1
            while i < len(lines) and lines[i].strip() != "```":
                i += 1
            if i < len(lines):
                i += 1  # закрывающий фенс
            continue
        in_list = in_list or s.rstrip(":").strip() in ("Связи", "Edges")
        i += 1
    out.extend(pending_blanks)  # хвостовые пустые — сохранить (разделитель)
    return i


def selftest():
    """Тесты strip_descriptions на синтетике (без сети/сервисов)."""
    checks = []

    def check(name, cond):
        checks.append((name, bool(cond)))

    # 1) prose-only ru: маркер + текст, дальше контент документа
    lines = [
        "![a](x.png)\n",
        "> **VLM-описание:** Схема базы.\n",
        "\n",
        "Текст главы.\n",
    ]
    out, n = strip_descriptions(lines)
    check("prose ru снят", n == 1 and "".join(out) == "![a](x.png)\n\nТекст главы.\n")

    # 2) en-маркер
    lines = ["![a](x.png)\n", "> **VLM-description:** ER diagram.\n", "## Heading\n"]
    out, n = strip_descriptions(lines)
    check("prose en снят", n == 1 and "".join(out) == "![a](x.png)\n## Heading\n")

    # 3) structured полный: prose + mermaid + Edges + таблица + ⚠
    block = (
        "> **VLM-description:** Diagram of A and B.\n"
        "\n"
        "```mermaid\n"
        "erDiagram\n"
        "    A ||--o{ B : has\n"
        "```\n"
        "\n"
        "Edges:\n"
        "\n"
        "- A to B\n"
        "\n"
        "| h1 | h2 |\n"
        "|---|---|\n"
        "| x | y |\n"
        "\n"
        "> ⚠ Uncertainties: cardinality of A-B\n"
    )
    lines = ["![a](x.png)\n", *block.splitlines(keepends=True), "## Heading\n"]
    out, n = strip_descriptions(lines)
    check(
        "structured снят целиком",
        n == 1 and "".join(out) == "![a](x.png)\n## Heading\n",
    )
    # 3-Б) то же, но с пустой строкой после блока (как в реальных файлах)
    lines = ["![a](x.png)\n", *block.splitlines(keepends=True), "\n", "## Heading\n"]
    out, n = strip_descriptions(lines)
    check(
        "structured снят, разделитель цел",
        n == 1 and "".join(out) == "![a](x.png)\n\n## Heading\n",
    )

    # 4) две картинки-описания подряд — снимаются оба
    lines = [
        "![a](x.png)\n",
        "> **VLM-описание:** Первая.\n",
        "\n",
        "![b](y.png)\n",
        "> **VLM-описание:** Вторая.\n",
        "\n",
        "Хвост.\n",
    ]
    out, n = strip_descriptions(lines)
    check("оба блока сняты", n == 2 and "VLM" not in "".join(out))

    # 5) контент документа после блока НЕ тронут (prose-абзац)
    lines = [
        "![a](x.png)\n",
        "> **VLM-описание:** Текст.\n",
        "\n",
        "Обычный абзац книги.\n",
    ]
    out, _ = strip_descriptions(lines)
    check("абзац после блока цел", "Обычный абзац книги.\n" in "".join(out))

    # 6) «>»-цитата ДОКУМЕНТА (не наш маркер) — не трогается
    lines = ["> Обычная цитата документа.\n", "Текст.\n"]
    out, n = strip_descriptions(lines)
    check("чужая цитата цела", n == 0 and "".join(out) == "".join(lines))

    # 7) идемпотентность: повторный вызов ничего не меняет
    once, n1 = strip_descriptions(
        ["![a](x.png)\n"] + block.splitlines(keepends=True) + ["Хвост.\n"]
    )
    twice, n2 = strip_descriptions(once)
    check("идемпотентно", n1 == 1 and n2 == 0 and once == twice)

    # 8) mermaid-фенс НЕ нашего блока (код в документе) — не трогается
    lines = ["```mermaid\n", "graph TD\n", "```\n", "Текст.\n"]
    out, n = strip_descriptions(lines)
    check("чужой фенс цел", n == 0 and out == lines)

    # 9) «Связи:» с буллетами, затем чужой абзац — граница корректна
    lines = [
        "> **VLM-описание:** Схема.\n",
        "\n",
        "Связи:\n",
        "\n",
        "- A -> B\n",
        "- B -> C\n",
        "\n",
        "Дальше глава.\n",
    ]
    out, n = strip_descriptions(lines)
    check("связи сняты, абзац цел", n == 1 and "".join(out) == "\nДальше глава.\n")

    # 10) edge: маркер на ТОЙ ЖЕ строке после картинки (ранние прогоны)
    lines = ["![a](x.png)> **VLM-описание:** Схема.\n", "\n", "Хвост.\n"]
    out, n = strip_descriptions(lines)
    check(
        "маркер в строке с картинкой снят",
        n == 1 and "".join(out) == "![a](x.png)\n\nХвост.\n",
    )

    # 11) маркер-текст БЕЗ картинки перед ним (упоминание в prose) — не трогать
    lines = ["Абзац, где цитируется «> **VLM-описание:** …» как пример.\n"]
    out, n = strip_descriptions(lines)
    check("упоминание в prose цела", n == 0 and out == lines)

    bad = [nm for nm, ok in checks if not ok]
    for nm, ok in checks:
        print(f"  {'ok' if ok else 'FAIL'}  {nm}")
    print(f"selftest caption_figures (strip): {len(checks) - len(bad)}/{len(checks)}")
    return 1 if bad else 0


# язык код -> фраза для промпта (только ru/en, другие не поддерживаются)
LANG_WORDS = {"ru": "русском", "en": "английском"}

# Встроенные промпты ПО ЯЗЫКУ (инструкции на языке ответа — иначе VLM
# скатывается в язык инструкций: замер на Ponniah: рус. промпт с «на
# английском» давал 14% русских описаний)
PROMPTS = {
    "ru": (
        "Ты готовишь описание изображения для полнотекстового поиска базы знаний "
        "по управлению данными. Опиши: 1) тип (схема/диаграмма/таблица/инфографика); "
        "2) что изображено и его роль в документе; 3) ВСЕ подписи, названия узлов, "
        "связи/стрелки, числа — дословно, насколько возможно. Учти контекст документа "
        "ниже. Плотный текст на русском одним абзацем, без воды.\n\nКонтекст: {ctx}"
    ),
    "en": (
        "You are preparing an image description for the full-text search index of "
        "a data management knowledge base. Describe: 1) the type (diagram/chart/"
        "table/infographic); 2) what is shown and its role in the document; "
        "3) ALL captions, node names, links/arrows, numbers — verbatim where "
        "possible. Consider the document context below. Dense text in English, "
        "one paragraph, no filler.\n\nContext: {ctx}"
    ),
}

LOCAL_HOSTS = ("127.0.0.1", "localhost", "0.0.0.0", "[::1]")

# Структурированные промпты (CAPTION_STRUCTURED=1) — по языку ответа, с ветвлением
# по типу фигуры В ОДНОМ ВЫЗОВЕ и few-shot erDiagram БЕЗ атрибутных блоков
# (пилот этапа 0: блоки атрибутов — единственный источник ошибок
# парсинга; для RAG достаточно связей). Формат ответа — XML-теги для парсинга.
STRUCT_PROMPTS = {
    "ru": """Ты готовишь структурированное описание изображения для полнотекстового поиска базы знаний по управлению данными. Контекст документа: {ctx}

Определи тип изображения: DIAGRAM (схема/ER-модель/блок-схема), TABLE (таблица) или OTHER (фото/график/инфографика). Ответ дай СТРОГО в XML-тегах, ничего кроме них — ответ читает парсер:

<type>DIAGRAM|TABLE|OTHER</type>
<prose>1–2 плотных предложения на русском: что изображено и его роль, с ключевыми сущностями/строками — для полнотекстового поиска.</prose>
<mermaid>код Mermaid, только если DIAGRAM, иначе пусто</mermaid>
<edges>- СУЩНОСТЬ_A -> СУЩНОСТЬ_B (кардинальность, имя связи) — по одной на связь; только если DIAGRAM</edges>
<table_md>таблица pipe-MD, только если TABLE, иначе пусто</table_md>
<uncertainties>что не удалось определить надёжно (например «направление связи B–C неясно»); пусто, если неопределённостей нет</uncertainties>

ЖЁСТКИЕ правила:
- DIAGRAM → заполнить <prose>, <mermaid> И <edges>. Список связей обязателен (для ЛЮБОГО типа диаграммы): поиск ищет по фразам, не по синтаксису.
- Выбери тип Mermaid ПО СОДЕРЖАНИЮ фигуры (пример — по одной строке-эталону):
  * erDiagram — ER-модели (сущности, связи с кардинальностями) — ТОЛЬКО строки связей, БЕЗ блоков атрибутов (секций вида ENTITY { ... } быть не должно):
    erDiagram
        PARTY ||--o{ PARTY_RELATIONSHIP : "of"
  * flowchart TB (или LR) — блок-схемы, архитектуры, процессы, оргструктуры (универсальный фолбэк, если тип не очевиден):
    A["Узел один"] -->|"метка стрелки"| B["Узел два"]
  * sequenceDiagram — диаграммы последовательности (акторы, сообщения по порядку):
    Менеджер->>Система: "запрос"
  * stateDiagram-v2 — состояния и переходы:
    state "В обработке" as s1
    s1 --> s2 : "событие"
  * classDiagram — UML-классы (наследования, ассоциации): Родитель <|-- Наследник
  * mindmap — радиальные схемы, карты мыслей (метки БЕЗ кавычек, вложенность отступами)
  Идентификаторы узлов/акторов — только A-Z a-z 0-9 и подчёркивание; кириллица, скобки и спецсимволы — только внутри меток в кавычках (кроме mindmap). Не более 40 строк.
- НЕ выдумывай узлы, связи и числа: пропуск детали допустим, выдумка — ошибка. Если узлов 50+, пропусти Mermaid и дай prose плюс список ключевых сущностей внутри <edges> с префиксом «СУЩНОСТИ:».
- Каждая связь — РОВНО один раз (не дублируй одну и ту же связь в обоих направлениях).
- Не рисуй «приблизительные» пунктирные связи; неопределённость фиксируй в <uncertainties>.
- TABLE → pipe-MD в <table_md>: числа, единицы, примечания дословно; различай пустую ячейку / 0 / тире; если объединённые ячейки (rowspan/colspan) нельзя выразить в MD — дай маленький фрагмент HTML <table>. Последней строкой добавь: *VLM-реконструкция, сверить с PDF.*
- Вывод — ТОЛЬКО теги.""",
    "en": """You are preparing a structured description of an image for the full-text search index of a data management knowledge base. Document context: {ctx}

Classify the image: DIAGRAM (scheme/ER model/flowchart), TABLE, or OTHER (photo/chart/infographic). Answer with these XML tags ONLY, nothing else — a parser reads your output:

<type>DIAGRAM|TABLE|OTHER</type>
<prose>1-2 dense sentences in English: what the visual shows and its role, naming key entities/rows — for plain-text search.</prose>
<mermaid>Mermaid code only if DIAGRAM, else empty</mermaid>
<edges>- ENTITY_A -> ENTITY_B (cardinality, relationship label) — one per edge; only if DIAGRAM</edges>
<table_md>pipe-MD table only if TABLE, else empty</table_md>
<uncertainties>anything you could not determine reliably (e.g. "direction of B-C unclear"); empty if none</uncertainties>

STRICT rules:
- DIAGRAM → provide ALL of <prose>, <mermaid> AND <edges>. The edges list is REQUIRED for ANY diagram type: search matches phrases, not diagram syntax.
- Pick the Mermaid type BY THE CONTENT of the figure (one reference line each):
  * erDiagram — ER models (entities, relationships with cardinalities) — RELATIONSHIP LINES ONLY, NEVER attribute blocks (no `ENTITY { ... }` sections):
    erDiagram
        PARTY ||--o{ PARTY_RELATIONSHIP : "of"
  * flowchart TB (or LR) — block schemes, architectures, processes, org charts (universal fallback when the type is unclear):
    A["Node one"] -->|"arrow label"| B["Node two"]
  * sequenceDiagram — sequence/interaction diagrams (actors, messages in order):
    Manager->>System: "request"
  * stateDiagram-v2 — state machines and transitions:
    state "In progress" as s1
    s1 --> s2 : "event"
  * classDiagram — UML classes (inheritance, associations): Parent <|-- Child
  * mindmap — radial schemes, mind maps (labels WITHOUT quotes, nesting by indentation)
  Node/actor ids: only A-Z a-z 0-9 underscore; Cyrillic, brackets and special characters only inside quoted labels (mindmap excepted). UNDER 40 lines.
- NEVER invent nodes, edges, or numbers: a missing detail is acceptable, a made-up one is a failure. If 50+ nodes, skip Mermaid and give prose plus an "ENTITIES:" list inside <edges>.
- Each edge appears EXACTLY once (do not duplicate the same edge in both directions).
- No "approximate" dashed edges; uncertainty goes to <uncertainties>.
- TABLE → pipe-MD in <table_md>: numbers, units, footnotes verbatim; distinguish empty cell / 0 / dash; if merged cells (rowspan/colspan) make MD impossible use a small HTML <table> fragment. Append final line: *VLM reconstruction — verify against the PDF.*
- Output ONLY the tags.""",
}

# Итоги structured-прогона для сводки (ключи статистики)
OUTCOMES = ("diagram_ok", "diagram_retry", "diagram_fallback", "table", "other", "err")


def api_key():
    # ключ облачного шлюза из .env (LLM_API_KEY; бывш. CAPTION_API_KEY/CHERRY_API_KEY)
    return os.environ.get("LLM_API_KEY")


def model_alias(api):
    req = urllib.request.Request(f"{api}/models")
    if api_key():
        req.add_header("Authorization", f"Bearer {api_key()}")
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)["data"][0]["id"]


def prompt_template(lang):
    """Шаблон промпта: env CAPTION_PROMPT либо встроенный ДЛЯ ЯЗЫКА ответа
    (инструкции на языке ответа — иначе VLM частично отвечает на языке
    инструкций). Плейсхолдеры {ctx}/{lang} — подстановка через replace,
    чтобы фигурные скобки в пользовательском тексте не ломали шаблон.
    В env перевод строки записывается как \\n (однострочный .env)."""
    tpl = os.environ.get("CAPTION_PROMPT", "").strip()
    if not tpl:
        return PROMPTS[lang], f"встроенный ({lang})"
    if "{ctx}" not in tpl:
        raise SystemExit(
            "CAPTION_PROMPT: нет плейсхолдера {ctx} — в него подставляется "
            "контекст документа; добавьте и перезапустите"
        )
    if "{lang}" not in tpl:
        tpl += "\n\nОтвечай на {lang} языке."
    # предупреждение: алфавит шаблона не совпадает с целевым языком
    cyr = len(re.findall(r"[а-яёА-ЯЁ]", tpl))
    lat = len(re.findall(r"[A-Za-z]", tpl))
    if (lang == "en" and cyr > lat) or (lang == "ru" and lat > cyr):
        print(
            f"[!] CAPTION_PROMPT написан преимущественно на другом языке "
            f"(целевой {lang}) — VLM может отвечать на языке шаблона; "
            f"рекомендуется пустой CAPTION_PROMPT (встроенные промпты по языку)"
        )
    return tpl.replace("\\n", "\n"), "CAPTION_PROMPT (.env)"


def render_prompt(tpl, lang, ctx):
    return tpl.replace("{lang}", LANG_WORDS.get(lang, "русском")).replace(
        "{ctx}", ctx[-500:] or "(нет)"
    )


def describe(api, alias, img_path, ctx, tpl, lang, structured=False):
    data = img_path.read_bytes()
    b64 = base64.b64encode(data).decode()
    mime = mimetypes.guess_type(str(img_path))[0] or "image/jpeg"
    headers = {"Content-Type": "application/json"}
    if api_key():
        headers["Authorization"] = f"Bearer {api_key()}"
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
                    {"type": "text", "text": render_prompt(tpl, lang, ctx)},
                ],
            }
        ],
        "max_tokens": 3000 if structured else 600,
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
    # structured: переводы строк ВАЖНЫ (Mermaid/таблицы) — не схлопываем
    if not structured:
        txt = re.sub(r"\s*\n\s*", " ", txt).strip()
    return txt, time.time() - t0


def parse_structured(txt):
    """Ответ VLM по XML-тегам -> dict секций (пустые — '')."""
    out = {}
    for tag in ("type", "prose", "mermaid", "edges", "table_md", "uncertainties"):
        m = re.search(rf"<{tag}>(.*?)</{tag}>", txt, re.DOTALL)
        out[tag] = m.group(1).strip() if m else ""
    out["mermaid"] = re.sub(r"^```[a-zA-Z]*\s*\n|\n```\s*$", "", out["mermaid"]).strip()
    # prose/uncertainties — однострочные blockquote (переносы схлопываем)
    for k in ("prose", "uncertainties"):
        out[k] = re.sub(r"\s*\n\s*", " ", out[k]).strip()
    if not out["type"]:  # нет тегов — честный prose-фолбэк
        out["type"] = "OTHER"
        out["prose"] = out["prose"] or re.sub(r"\s+", " ", txt).strip()
    return out


def validate_mermaid(code):
    """(True, '') если mmdc отрендерил; иначе (False, хвост ошибки для ретрая)."""
    with tempfile.TemporaryDirectory(prefix="mmdc_") as d:
        mmd, svg, cfg = (pathlib.Path(d) / n for n in ("x.mmd", "x.svg", "pptr.json"))
        mmd.write_text(code + "\n", encoding="utf-8")
        # Ubuntu 24.x: unprivileged userns выключен — Chromium без --no-sandbox
        # не стартует (пилот этапа 0)
        cfg.write_text(
            json.dumps({"args": ["--no-sandbox", "--disable-setuid-sandbox"]})
        )
        try:
            p = subprocess.run(
                [
                    "npx",
                    "-y",
                    "@mermaid-js/mermaid-cli",
                    "-i",
                    str(mmd),
                    "-o",
                    str(svg),
                    "-p",
                    str(cfg),
                ],
                capture_output=True,
                text=True,
                timeout=300,
                check=False,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired) as e:
            return False, f"mmdc failed to run: {e}"
        if p.returncode == 0 and svg.exists():
            return True, ""
        # полезная диагностика (Parse error ...) — в первых строках stderr;
        # в хвосте только стектрейс puppeteer, модели он бесполезен
        head = "\n".join((p.stderr or "").splitlines()[:4]).strip()
        return False, head or (p.stdout or "")[-600:]


def assemble_structured(sec, lang):
    """Секции ответа -> многострочная вставка (prose blockquote + ```mermaid + «Связи:»):
    prose blockquote (маркер — первая строка), затем mermaid, список связей,
    таблица, неопределённости. -> (текст_вставки, outcome)."""
    edges_hdr = "Связи:" if lang == "ru" else "Edges:"
    unc_hdr = "Неопределённости" if lang == "ru" else "Uncertainties"
    parts = [sec["prose"]] if sec["prose"] else []
    outcome = "other"
    if sec["type"] == "DIAGRAM":
        if sec["mermaid"]:
            parts += ["```mermaid", sec["mermaid"], "```"]
            outcome = "diagram_ok"
        else:
            outcome = "diagram_fallback"  # §5.3: фолбэк prose-only
        if sec["edges"]:
            parts += [edges_hdr] + [
                ln for ln in sec["edges"].splitlines() if ln.strip()
            ]
    elif sec["type"] == "TABLE" and sec["table_md"]:
        parts.append(sec["table_md"])
        outcome = "table"
    if sec["uncertainties"]:
        parts.append(f"> ⚠ {unc_hdr}: {sec['uncertainties']}")
    return "\n\n".join(parts), outcome


def describe_structured(api, alias, img_path, ctx, lang):
    """Structured-конвейер: вызов -> парсинг -> mmdc -> ретрай x1 с текстом
    ошибки -> фолбэк prose. -> (вставка, outcome, сек)."""
    t0 = time.time()
    tpl = STRUCT_PROMPTS[lang]
    txt, _ = describe(api, alias, img_path, ctx, tpl, lang, structured=True)
    sec = parse_structured(txt)
    retried = False
    if sec["type"] == "DIAGRAM" and sec["mermaid"]:
        ok, err = validate_mermaid(sec["mermaid"])
        if not ok:
            retried = True
            fix = (
                "\n\nТвой прошлый Mermaid не срендерился с ошибкой:\n---\n"
                f"{err}\n---\nИсправь код Mermaid, сохранив содержание. "
                "Ответь снова всеми тегами."
                if lang == "ru"
                else "\n\nYour previous Mermaid failed to render with this "
                f"error:\n---\n{err}\n---\nFix the Mermaid code, keep the same "
                "content. Reply again with all tags."
            )
            # ретрай: полный промпт идёт как tpl, контекст пуст (плейсхолдеров нет)
            retry_tpl = (
                tpl.replace("{ctx}", "(контекст тот же)")
                + "\n\nКонтекст: "
                + (ctx[-500:] or "(нет)")
                + fix
                if lang == "ru"
                else tpl.replace("{ctx}", "(same context)")
                + "\n\nContext: "
                + (ctx[-500:] or "(none)")
                + fix
            )
            txt2, _ = describe(
                api, alias, img_path, "", retry_tpl, lang, structured=True
            )
            sec2 = parse_structured(txt2)
            if sec2["mermaid"]:
                ok2, _ = validate_mermaid(sec2["mermaid"])
                if ok2:
                    sec2["edges"] = sec2["edges"] or sec["edges"]
                    sec2["uncertainties"] = (
                        sec2["uncertainties"] or sec["uncertainties"]
                    )
                    sec, ok = sec2, True
        if not ok:
            sec["mermaid"] = ""  # фолбэк: prose + связи, без диаграммы
    ins, outcome = assemble_structured(sec, lang)
    if retried and outcome == "diagram_ok":
        outcome = "diagram_retry"
    return ins, outcome, time.time() - t0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("md", type=pathlib.Path, nargs="?")
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
    _env_lang = os.environ.get("CAPTION_LANG", "ru")
    ap.add_argument(
        "--lang",
        choices=sorted(LANG_WORDS),
        default=_env_lang if _env_lang in LANG_WORDS else "ru",
        help="язык описаний (деф. ru; env CAPTION_LANG; prepare_doc передаёт "
        "распознанный язык документа)",
    )
    ap.add_argument(
        "--structured",
        action="store_true",
        default=os.environ.get("CAPTION_STRUCTURED", "0") == "1",
        help="структурированные описания: схемы -> prose + Mermaid "
        "+ список связей (валидация mmdc, ретрай x1, фолбэк prose), таблицы -> "
        "pipe-MD реконструкция (деф. выкл; env CAPTION_STRUCTURED=1)",
    )
    ap.add_argument(
        "--redescribe",
        action="store_true",
        help="снять старые blockquote-описания (ru/en, prose и structured) "
        "и описать фигуры заново; бэкап <md>.bak_caption рядом",
    )
    ap.add_argument("--selftest", action="store_true", help="тесты strip_descriptions")
    a = ap.parse_args()
    if a.selftest:
        sys.exit(selftest())
    min_bytes = int(os.environ.get("CAPTION_MIN_BYTES", "20000"))
    mark = MARK_RU if a.lang == "ru" else MARK_EN
    tpl, tpl_src = prompt_template(a.lang)
    print(
        f"captioning: язык описаний — {a.lang} ({LANG_WORDS[a.lang]}), "
        f"маркер «{mark.strip()}», промпт: {tpl_src}"
        + (", РЕЖИМ: структурированный (Mermaid/таблицы)" if a.structured else "")
    )

    lines = a.md.read_text(encoding="utf-8").splitlines(keepends=True)
    if a.redescribe:
        lines, n_removed = strip_descriptions(lines)
        if n_removed:
            backup = a.md.with_suffix(a.md.suffix + ".bak_caption")
            backup.write_bytes(a.md.read_bytes())
            print(
                f"redescribe: снято старых описаний {n_removed}, "
                f"бэкап {backup.name} (снимок до перезаписи)"
            )
        else:
            print("redescribe: старых описаний нет — будет дописано недостающее")
    try:
        alias = a.model or model_alias(a.api)
    except Exception:  # noqa: BLE001 — сервис недоступен = выход с кодом 2
        print("captioning: LLM/VLM-сервис не отвечает на " + a.api, file=sys.stderr)
        sys.exit(2)

    def path_of(ref):
        p = (a.md.parent / ref).resolve()
        return p if (p.is_file() and p.stat().st_size >= min_bytes) else None

    def described(i):  # есть ли blockquote-описание у ссылок строки i
        j = i + 1
        while j < len(lines) and not lines[j].strip():
            j += 1
        return j < len(lines) and any(lines[j].startswith(m) for m in MARKS)

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

    # прогрев mmdc/npx ДО параллельных воркеров: холодный старт npx при
    # конкурентных вызовах гоняется за кэш npm -> первый mmdc падает
    # (наблюдался дважды на пилоте/этапе 1)
    if a.structured and any(
        p.suffix.lower() in (".png", ".jpg", ".jpeg", ".gif", ".webp") for p in todo
    ):
        with contextlib.suppress(Exception):  # прогрев не должен ронять пакет
            validate_mermaid('flowchart TB\n    W["warm up"] --> S["start"]')

    # проход 2: описания (параллельно при --parallel > 1), прогресс по готовности
    results, outcomes, durs, done = {}, {}, [], 0

    def work(p):
        if a.structured:
            return describe_structured(a.api, alias, p, ctxs[p], a.lang)
        txt, dt = describe(a.api, alias, p, ctxs[p], tpl, a.lang)
        return txt, None, dt

    def report(p, txt, outcome, dt):
        nonlocal done
        if outcome is not None:
            outcomes[p] = outcome
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
        extra = f", {outcomes[p]}" if outcomes.get(p) else ""
        print(
            f"  [{done}/{m_total}] {p.name}: {len(txt)} симв.{extra} ({dt:.0f}с{eta})",
            flush=True,
        )

    if a.parallel > 1 and len(todo) > 1:
        with concurrent.futures.ThreadPoolExecutor(max_workers=a.parallel) as ex:
            futs = {ex.submit(work, p): p for p in todo}
            for f in concurrent.futures.as_completed(futs):
                p = futs[f]
                try:
                    txt, outcome, dt = f.result()
                except Exception as e:  # noqa: BLE001 — одна битая фигура не роняет пакет
                    print(f"  [!] {p.name}: {e}", flush=True)
                    continue
                report(p, txt, outcome, dt)
    else:
        for p in todo:
            txt, outcome, dt = work(p)
            report(p, txt, outcome, dt)

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
                    ins.append(f"{mark} {results[p]}\n")
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
    if a.structured:  # статистика режима
        cnt = {o: 0 for o in OUTCOMES}
        for o in outcomes.values():
            cnt[o] = cnt.get(o, 0) + 1
        cnt["err"] = m_total - len(outcomes)
        dia = cnt["diagram_ok"] + cnt["diagram_retry"]
        print(
            f"captioning: структура: mermaid валидно {dia} "
            f"(ретрай {cnt['diagram_retry']}, фолбэк prose {cnt['diagram_fallback']}), "
            f"таблиц {cnt['table']}, прочих {cnt['other']}, ошибок {cnt['err']}"
        )


if __name__ == "__main__":
    main()
