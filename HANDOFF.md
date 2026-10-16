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
  surya; selftest `--test`), `services.sh start --ocr-proxy` (+Paddle-сервис,
  check/stop/restart), `prepare_doc.sh` аттачится к прокси автоматически.
  Приёмка: полный PDF 39 стр. — QC PASS (cyr 97,1%), дифф с эталоном
  косметический. A/B block-OCR (`scripts/ab_block_ocr.py`,
  `outbox/ocrtests/adapter_ab/`): Paddle 3–5× быстрее surya при равной
  кириллице, на тяжёлой таблице устойчивее (у surya repetition-loop).
  **Но на DMRB Vol.1 (стр. 161–170) первый же реальный block-OCR (table-fallback
  Table 4.19) показал: Paddle отдаёт plain без `<table>` → таблица теряна.**
  → дефолт `OCR_PROXY_BLOCK_ENGINE=surya` (чистый pass-through, безопасно),
  Paddle — опция для экспериментов. **Рекомендация: стартовать всегда с
  `--ocr-proxy`**; `--no-ocr-proxy` не вводился. Полные детали — USAGE §3.6
  аддендум-2/3/4.
- **Склейка фрагментов (2026-10-16, бедка `cell--ll3um-mvapljefi2y`, реестр
  #26)**: `scripts/merge_fragments.py` (selftest `--selftest`, 14 проверок) —
  авто-склейка в `prepare_all.sh` при `--fragments K>1` всегда: MD конкатенация,
  медиа в общий каталог (коллизия с иным содержимым → переименование с
  префиксом фрагмента + переписывание ссылок), `_meta.json` объединяется
  (TOC/page_stats), проверки: каждая часть входит посимвольно, все ссылки
  резолвятся, мягкий QC + `.qc_pass`. Имя результата: полный диапазон →
  `outbox/<имя>/`, выборка → `outbox/<имя>__pS-E/`. Приёмка: СиММА 71 стр. ×
  3 фрагм. — 164К симв., кириллица 90,1%, 35/35 картинок, 0 коллизий,
  байт-в-байт с суммой частей.
- **Управление фрагментами (2026-10-16)**: `--debug` — по умолчанию ВЫКЛ, т.е.
  каталоги фрагментов УДАЛЯЮТСЯ после успешной склейки (проверки внутри);
  `--debug` оставляет их для ручной проверки склейки. `--fragment-pause N`
  (сек, деф. 0) — пауза между фрагментами для охлаждения ПК (не после
  последнего); принимает `--fragment-pause` и `--fragment_pause`. Оба — USAGE
  §2.0-Б/§2.2 рядом с `--fragments`.
- **Факт-скорость (2026-10-16)**: `/stats` прокси классифицирует запросы
  (layout/table_rec/fullpage/block); `prepare_doc.sh` при аттаче к прокси
  печатает каждые 15 с факт по завершённым layout-запросам
  (`marker факт: 3м20с | layout 25/39 стр | 8.0 с/стр`), финал —
  `marker Nс = X.X с/стр`, в сводке batch — колонка «с/стр».
- **cleanup TOC-лидеров в таблицах (2026-10-16)**: паттерн расширен — 4+ точки
  перед `|` или номером страницы (DAMA: `| 3.3 Название ..... | 54 |` проходили
  из-за порога 8 точек; вычищено 114 строк на выборке p1-15).
- **Типовые сценарии запуска (2026-10-16)**: USAGE §2.0 — копируй-и-запускай
  (проба→полный, фрагменты+пауза, retry-ocr, черновик, точечно, контроль);
  выжимка в SKILL агента.
- **lemonade-server/Nemotron (2026-10-16, закрыто)**: см. Грабли — модель
  удалена (−2,9 ГБ), порт :8080 освобождён, captioning на Qwen проверен полным
  прогоном СиММА (2 фрагмента, VLM-описания созданы).
- Навык агенту: `.opencode/skills/docprep/SKILL.md`.

## Текущее состояние
- Сервисы **остановлены** (рекомендуемый подъём: `./services.sh start --ocr-proxy`
  — surya + Paddle/прокси + Qwen; дефолт p=8/p=2, block-OCR=surya).
- `outbox/`: «СиММА РП ч.1 Архитектор» (71 стр., 2 фрагмента + склейка,
  VLM-описания включены) + тестовая выборка `__p1-2`; артефакты A/B адаптера —
  `outbox/ocrtests/adapter_ab/`. Прежний эталон «АРХИТЕКТУРА…» удалён при
  очистке outbox (2026-10-16, по указанию пользователя).
- `inbox/`: «СиММА РП ч. 1 Архитектор v2_12 2026-06-25.pdf» (71 стр.,
  цифровой, кириллица 99,3%). Git: коммиты до `80359f1`, remote НЕ настроен.
- `.env` не в git: пути моделей (`/media/llm/Models/OCR/...`), `CAPTION_PARALLEL=2`
  (временные CAPTION_FIGURES=0 из тестов убраны).

## Следующие шаги (по приоритету)
1. Загрузить готовый `СиММА….md` в БЗ Cherry Studio через UI; проверить
   реальными поисковыми запросами (главный приёмочный тест всей затеи).
2. `sudo snap remove lemonade-server` — окончательно закрыть :8080 (snapd
   refresh может снова поднять демон; модель уже удалена, будет просто
   торчать на порту и ломать captioning).
3. Опционально: chandra-ocr-2 (преемник surya-2, 5,3B Qwen3.5-VLM, ru 85,5%,
   olmOCR 85,8 vs ~83,3 у surya-2) — llama.cpp уже поддерживает конверсию
   Qwen3_5ForConditionalGeneration → GGUF под Vulkan возможен; на marker 2.0.0
   НЕ интегрирована (свой CLI, vLLM/HF, лицензия OpenRAIL-M для коммерции).
   Альтернатива-конкурент адаптеру: замена surya целиком (включая layout).
4. Эксперимент A: fast-режим на чистом цифровом PDF — USAGE §3.5.
5. Этап 3 плана README: локальный MinerU (`mineru-api :8000`, backend pipeline)
   как авто-путь в CS + `local-paddleocr` для картинок.
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
- Ревиз issues marker/surya (2026-10-14): публичных аналогов адаптации чужих
  OCR-моделей в marker НЕТ (marker 1.x имел OCR_ENGINE/tesseract — в 2.0
  удалена); расширяемая точка marker — только LLM-сервисы (--use_llm,
  PR #1097 litellm). Грабля upstream: свежие llama.cpp ломаются на `\d` в
  bbox-паттерне грамматики guided-layout (surya #542, «Failed to parse
  grammar», 400) — наш llama-server 0.6.0-dev build 11541 работает; при
  апгрейде llama.cpp — учесть (проверить `[ ]`-ответ layout-запроса).
- Кириллица QC считается СРЕДИ БУКВ; доля <55% на полном документе — сигнал
  потери текста → `--retry-ocr`. Для АНГЛОЯЗЫЧНЫХ документов гейт не применим
  (DMRB: cyr=0% — ожидаемый FAIL в мягком режиме).
- Склейка: после успешной проверки каталоги фрагментов удаляются (деф.);
  `--debug` — оставить. QC склеенного — мягкий отчёт (гейты уже пройдены
  фрагментами).
- `--ocr-proxy` (адаптер): в balanced block-OCR вызывается только в
  table-fallback (таблицы с битыми шрифтами), формулы и fast-mode хирургия —
  на чистом цифровом PDF Paddle простаивает; дефолт block-OCR=surya (аддендум-4).
  Для смены набора сервисов: явный `stop` → `start`.
- **lemonade-server snap (AMD) и :8080 — история вопроса (закрыто 2026-10-16)**:
  snap-демон поднимался snapd при авто-обновлении (юнит disabled!), обслужива
  недокачанный Nemotron из `/media/llm/Models/Nemotron-…-GGUF`; процесс скрыт
  (PID-namespace snap — виден только сокет). Из-за него captioning молча бился
  об Error 500 (figs=0, prepare_doc верил /v1/models). Модель УДАЛЕНА (−2,9 ГБ),
  порт освобождён, Qwen на :8080 проверен. Остаток: snap установлен — при
  следующем refresh демон может снова занять :8080 → `sudo snap remove
  lemonade-server`. Опознать врага: `curl :8080/v1/models` с полями
  `checkpoint/checkpoints` = lemonade.

## Ключевые артефакты
| Что | Где |
|---|---|
| Главный справочник (возможности/замеры/реестр) | `USAGE.md` |
| План/обоснование стека | `README.md` |
| Сервисы / пайплайн | `services.sh`, `scripts/{launch_batch,prepare_all,prepare_doc}.sh` |
| QC / captioning / cleanup | `scripts/{qc_check,caption_figures,clean_md}.py` |
| Склейка фрагментов (авто при K>1) | `scripts/merge_fragments.py` |
| OCR-прокси (гибрид surya+Paddle) + A/B | `scripts/ocr_proxy.py`, `scripts/ab_block_ocr.py` |
| Тайминги (ETA-калибровка) | `logs/timings.jsonl` |
| Готовый MD (СиММА, с VLM-описаниями) | `outbox/СиММА РП ч. 1 Архитектор v2_12 2026-06-25/` |
| Навык агента | `.opencode/skills/docprep/SKILL.md` |
