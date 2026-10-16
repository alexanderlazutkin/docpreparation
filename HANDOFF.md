# HANDOFF: docpreparation — преп-пайплайн PDF → Markdown для БЗ Cherry Studio

> Обновлён 2026-10-16. Осн. документы: `README.md` (план/стек),
> `USAGE.md` (возможности/примеры/замеры/реестр требований — главный справочник).

## Контекст
Локальный RAG Cherry Studio 2.1.4 на русскоязычных PDF (DAMA-DMBOK, рекомендации ФР):
таблицы, схемы, сканы, битые шрифты. Цель — преп-пайплайн: PDF → готовый `.md`
(текст + таблицы + VLM-описания графики рядом) с QC-гейтом, целиком на локальном
железе (Strix Halo, Radeon 8060S, ROCm не используется — inference через llama.cpp/Vulkan).

## Что сделано (завершено)
- **uv-проект** marker-pdf (глобальный Python не тронут), git-репозиторий, рабочие
  каталоги `inbox/ outbox/ logs/` вне git.
- **services.sh**: surya VLM (:8510, alias `datalab-to/surya-ocr-2`, p=8) +
  LLM/VLM Qwen3.6-35B-A3B (:8080, p=2, mmproj-F32). Флаги `--llm-parallel`,
  `--llm-model`, `--no-llm`; check --deep.
- **Пайплайн `prepare_doc.sh`/`prepare_all.sh`/`launch_batch.sh`**: стадии
  [1/6] marker → [2/6] captioning → [3/6] cleanup TOC → [4/6] QC → [5/6] force_ocr
  (только `--retry-ocr`) → [6/6] готово. Resume по `.qc_pass`, `--pages "S[-E]"`,
  `--fragments K`, ETA по `logs/timings.jsonl`, per-file логи + `batch_latest.*`.
  Агент запускает batch ТОЛЬКО через `launch_batch.sh` (видимое окно терминала,
  авто-закрытие 5 мин).
- **Captioning**: локально 35B (5,4 с/фигура при `CAPTION_PARALLEL=2`) — быстрее
  облака (Cherry free: ~19 с, plus: ~16 с); облако подключается env
  `CAPTION_API/MODEL/PARALLEL` + ключ `CHERRY_API_KEY` (см. USAGE §2.4, §3.1).
- **QC**: кириллица ≥55% среди букв, лидеры, переносы, таблицы. Полный документ
  FAIL → без ретрая (только `--retry-ocr`); фрагменты — мягкий режим.
- **Замеры производительности** (USAGE §3.5): дефолт surya p=8 оптимален
  (10,5 с/стр); p=16 −57%; параллельные фрагменты выигрыш только при p<8.
- **Приёмка**: «АРХИТЕКТУРА И МОДЕЛИРОВАНИЕ ДАННЫХ» (39 стр.) — полный PASS
  (marker 440с + 3 описания + cleanup 31 лидер + QC PASS). Спот-чек таблиц
  (текстовый + через Qwen3VL-8B/Qwen3.6-35B визуально) — без потерь.
- **Модели перенесены**: `/media/llm/Models/datalab-to` → `/media/llm/Models/OCR`
  (рядом с GLM-OCR, PaddleOCR-VL-1.6); пути обновлены везде, старт/стоп проверен.
- **Тест GLM-OCR / PaddleOCR-VL-1.6 как drop-in surya (USAGE §3.6)**: обе модели
  несовместимы с промптами-контрактами surya (bbox массивом вместо строки →
  блоки skip, потеря ~70% текста, у GLM пустые страницы) И медленнее surya-2
  (GLM 20,5 с/стр, Paddle 14,0 против 10,5 у surya; p=4×2 фрагмента не помогает).
  Вывод: **surya-2 остаётся OCR-движком**. Проверка «не учли ли параметры»
  (§3.6 аддендум): параметры ни при чём — llama-server навязывает json_schema
  (Paddle отвечает пустым `[ ]` → пустые страницы), у GLM-OCR слабая кириллица
  даже нативно → **GLM-OCR отклонён, веса удалены (2,5 ГБ)**. **PaddleOCR-VL
  нативно (`<|ocr|>`) читает чисто и полно (5,5 с/стр full-page)** — кандидат
  на «отдельный OCR-сервис с адаптером формата», если понадобится быстрый путь.
  Новое: `services.sh --surya-model/--surya-mmproj/--surya-parallel N` (подмена
  OCR-VLM за алиасом). Тестовые выходы: `outbox/ocrtests/` (вне git).
  Грабли: `restart --no-llm` НЕ перезапускает surya (stop с --no-llm её не
  трогает) — для смены --surya-* флагов делать явный `stop` → `start`.
- **Адаптер-прокси PaddleOCR-VL → marker (2026-10-16, бедка `cell--ll3um`) —
  реализован и принят**: `scripts/ocr_proxy.py` (OpenAI-прокси :8511, алиас
  surya; роутинг: layout/table_rec/full-page → surya :8510, block-OCR →
  Paddle :8512 с `<|ocr|>` и `<p>`-обёрткой; selftest `--test` — 27 проверок),
  `services.sh start --ocr-proxy` (+Paddle-сервис, check/stop/restart),
  `prepare_doc.sh` аттачится к прокси автоматически. Приёмка: полный PDF
  39 стр. — QC PASS (cyr 97,1%), дифф с эталоном косметический, но block-OCR
  в balanced не вызывается (таблицы берутся из full-page/pdftext) → прокси как
  pass-through. A/B block-OCR (`scripts/ab_block_ocr.py`,
  `outbox/ocrtests/adapter_ab/`): Paddle 3–5× быстрее surya при равной
  кириллице, на тяжёлой таблице устойчивее (у surya repetition-loop).
  Итог: `--ocr-proxy` — опциональный режим (не дефолт), выигрыш когда таблицы
  массово падают в OCR-fallback. **Рекомендация (2026-10-16): стартовать всегда
  с `--ocr-proxy`** — роутинг автоматический по содержимому, флаг безопасен на
  любом PDF (на чистых Paddle простаивает); `--no-ocr-proxy` не вводился:
  флаг не указан — адаптер не поднимается. Полные детали — USAGE §3.6 аддендум-2.
- Навык агенту: `.opencode/skills/docprep/SKILL.md`.

## Текущее состояние
- Сервисы **остановлены** (`./services.sh start` поднимет; дефолт p=8/p=2;
  гибридный OCR — `start --ocr-proxy`, см. §3.6 аддендум-2).
- `outbox/`: готовый MD «АРХИТЕКТУРА…» (+ фигуры, `_meta.json`, `.qc_pass`);
  артефакты A/B адаптера — `outbox/ocrtests/adapter_ab/` (вне git).
- `inbox/`: тот же PDF. Git: remote НЕ настроен (публикация за пользователем).
- `.env` не в git: пути моделей (`/media/llm/Models/OCR/...`), `CAPTION_PARALLEL=2`.

## Следующие шаги (по приоритету)
1. Загрузить готовый `.md` в БЗ Cherry Studio через UI; проверить реальными
   поисковыми запросами (главный приёмочный тест всей затеи).
2. **Склейка фрагментов после обработки** (требование 2026-10-16, бедка
   `cell--ll3um-mvapljefi2y`): при
   `--fragments K` после обработки всех частей — объединить их в один `.md`
   без потери информации: ссылки/якоря фигур (относительные пути, нумерация
   `*_md_prefix.md` → единый файл + общая папка figures), заголовки/TOC,
   метаданные `_meta.json`. Нужна проверка: QC на склеенном файле + диф-чек
   что все части вошли (размер/строки, ссылки целевые). Сейчас фрагменты
   остаются раздельными выходами с мягким QC.
3. Опционально: chandra-ocr-2 (преемник surya-2, 5,3B Qwen3.5-VLM, ru 85,5%,
   olmOCR 85,8 vs ~83,3 у surya-2) — llama.cpp уже поддерживает конверсию
   Qwen3_5ForConditionalGeneration → GGUF под Vulkan возможен; на marker 2.0.0
   НЕ интегрирована (свой CLI, vLLM/HF, лицензия OpenRAIL-M для коммерции).
   Альтернатива-конкурент адаптеру: замена surya целиком (включая layout).
4. Эксперимент A: fast-режим на чистом цифровом PDF (нужен файл в inbox/,
   например ФР-2025) — USAGE §3.5.
5. Вернуть DAMA Часть 1 (225 стр.) → полный прогон (~35 мин) → БЗ
   (использовать фрагменты + склейку из п.2).
6. Этап 3 плана README: локальный MinerU (`mineru-api :8000`, backend pipeline)
   как авто-путь в CS + `local-paddleocr` для картинок.
7. Опубликовать репозиторий (remote за пользователем).

## Грабли / примечания
- **Баг restart исправлен** (было: `${NO_LLM:+--no-llm}` передавал флаг всегда,
  т.к. «0» непустое → surya не перезапускалась, LLM не поднимался). Если сервер
  не меняет конфиг после restart — проверять `pgrep -af llama-server | grep -oE "parallel [0-9]+"`.
- marker/surya умеет спавнить СВОЙ llama-server на случайном порту, если :8510
  недоступен → сирота ест VRAM. Лечится: `pgrep -af llama-server`, kill лишних.
- Терминальные окна экспериментов: без `read` без таймаута (зомби) — hold
  только с `read -t N`.
- llama-сервер логи: `/tmp/docprep-{surya,llm}.log`; авто-spawn surya:
  `~/.cache/datalab/surya/llamacpp_server.log`.
- QC «таблицы не распознаны» может ложно срабатывать на текстовых выборках
  (`--pages`) — для фрагментов QC мягкий by design.
- Ревиз issues marker/surya (2026-10-14): публичных аналогов адаптации чужих
  OCR-моделей в marker НЕТ (marker 1.x имел OCR_ENGINE/tesseract — в 2.0
  удалена); расширяемая точка marker — только LLM-сервисы (--use_llm,
  PR #1097 litellm). Грабля upstream: свежие llama.cpp ломаются на `\d` в
  bbox-паттерне грамматики guided-layout (surya #542, «Failed to parse
  grammar», 400) — наш llama-server 0.6.0-dev build 11541 работает; при
  апгрейде llama.cpp — учесть (проверить `[ ]`-ответ layout-запроса).
- Кириллица QC считается СРЕДИ БУКВ; доля <55% на полном документе — сигнал
  потери текста → `--retry-ocr`.
- `--ocr-proxy` (адаптер): в balanced block-OCR вызывается только в
  table-fallback (таблицы с битыми шрифтами), формулы и fast-mode хирургия —
  на чистом цифровом PDF Paddle простаивает, прокси работает как pass-through
  (§3.6 аддендум-2). Для смены набора сервисов: явный `stop` → `start`.

## Ключевые артефакты
| Что | Где |
|---|---|
| Главный справочник (возможности/замеры/реестр) | `USAGE.md` |
| План/обоснование стека | `README.md` |
| Сервисы / пайплайн | `services.sh`, `scripts/{launch_batch,prepare_all,prepare_doc}.sh` |
| QC / captioning / cleanup | `scripts/{qc_check,caption_figures,clean_md}.py` |
| OCR-прокси (гибрид surya+Paddle) + A/B | `scripts/ocr_proxy.py`, `scripts/ab_block_ocr.py` |
| Тайминги (ETA-калибровка) | `logs/timings.jsonl` |
| Готовый MD | `outbox/Рекомендации… АРХИТЕКТУРА И МОДЕЛИРОВАНИЕ ДАННЫХ/` |
| Навык агента | `.opencode/skills/docprep/SKILL.md` |
