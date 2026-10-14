# HANDOFF: docpreparation — преп-пайплайн PDF → Markdown для БЗ Cherry Studio

> Обновлён 2026-10-14 (вечер). Осн. документы: `README.md` (план/стек),
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
  даже нативно. **PaddleOCR-VL нативно (`<|ocr|>`) читает чисто и полно
  (5,5 с/стр full-page)** — кандидат на «отдельный OCR-сервис с адаптером
  формата», если понадобится быстрый путь. GLM-OCR отклонён окончательно.
  Новое: `services.sh --surya-model/--surya-mmproj/--surya-parallel N` (подмена
  OCR-VLM за алиасом). Тестовые выходы: `outbox/ocrtests/` (вне git).
  Грабли: `restart --no-llm` НЕ перезапускает surya (stop с --no-llm её не
  трогает) — для смены --surya-* флагов делать явный `stop` → `start`.
- Навык агенту: `.opencode/skills/docprep/SKILL.md`.

## Текущее состояние
- Сервисы **остановлены** (`./services.sh start` поднимет; дефолты p=8/p=2).
- `outbox/`: готовый MD «АРХИТЕКТУРА…» (+ фигуры, `_meta.json`, `.qc_pass`).
- `inbox/`: тот же PDF. Git: 11+ коммитов, remote НЕ настроен (публикация за пользователем).
- `.env` не в git: пути моделей (`/media/llm/Models/OCR/...`), `CAPTION_PARALLEL=2`.

## Следующие шаги (по приоритету)
1. Загрузить готовый `.md` в БЗ Cherry Studio через UI; проверить реальными
   поисковыми запросами (главный приёмочный тест всей затеи).
2. Эксперимент A: fast-режим на чистом цифровом PDF (нужен файл в inbox/,
   например ФР-2025) — USAGE §3.5.
3. Вернуть DAMA Часть 1 (225 стр.) → полный прогон (~35 мин) → БЗ.
4. Этап 3 плана README: локальный MinerU (`mineru-api :8000`, backend pipeline)
   как авто-путь в CS + `local-paddleocr` для картинок.
5. Опционально: native-замер GLM-OCR/PaddleOCR-VL прямой транскрипцией рендеров
   (one-off llama-server, без marker) — drop-in путь закрыт (§3.6).
6. Опубликовать репозиторий (remote за пользователем).

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
- Кириллица QC считается СРЕДИ БУКВ; доля <55% на полном документе — сигнал
  потери текста → `--retry-ocr`.

## Ключевые артефакты
| Что | Где |
|---|---|
| Главный справочник (возможности/замеры/реестр) | `USAGE.md` |
| План/обоснование стека | `README.md` |
| Сервисы / пайплайн | `services.sh`, `scripts/{launch_batch,prepare_all,prepare_doc}.sh` |
| QC / captioning / cleanup | `scripts/{qc_check,caption_figures,clean_md}.py` |
| Тайминги (ETA-калибровка) | `logs/timings.jsonl` |
| Готовый MD | `outbox/Рекомендации… АРХИТЕКТУРА И МОДЕЛИРОВАНИЕ ДАННЫХ/` |
| Навык агента | `.opencode/skills/docprep/SKILL.md` |
