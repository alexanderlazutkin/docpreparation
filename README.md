# docpreparation — преп-пайплайн PDF → Markdown для базы знаний

Локальный конвейер подготовки документов: PDF → готовый `.md` (текст, таблицы,
VLM-описания графики, метки страниц) с QC-гейтом качества. Целевые потребители —
БЗ/RAG Cherry Studio и Obsidian. Всё считается на локальном железе
(Strix Halo / Radeon 8060S, llama.cpp/Vulkan), без облака.

> Главный справочник по возможностям, примерам, замерам и реестру требований —
> [docs/USAGE.md](docs/USAGE.md). Статус/планы — [docs/HANDOFF.md](docs/HANDOFF.md).

## Зачем

Встроенный конвертер Cherry Studio тихо теряет текст на русскоязычных PDF:
шрифты без ToUnicode дают 0% кириллицы в индексе при «успешной» загрузке,
таблицы склеиваются в мусор, оглавления с точечными лидерами попадают в чанки,
нет layout-анализа и привязки к страницам. Вывод (аудит двух БЗ, детально —
[docs/COMPARISON.md](docs/COMPARISON.md)): **PDF не должен быть рабочим форматом
входа в БЗ** — конвертация делается заранее, вне CS, с контролем качества.
Готовый `.md` индексируется чисто (детальный разбор — USAGE).

## Архитектура

```
inbox/*.pdf ──► [1/6] marker (balanced) ──► [1.5] авто-ремонт таблиц
                    │  layout/OCR: surya-2 (GGUF, llama.cpp/Vulkan :8510)
                    │  опц. гибрид: OCR-прокси :8511 (block-OCR → PaddleOCR-VL :8512)
                    ▼
                [2/6] captioning фигур — VLM Qwen3.6-35B-A3B (:8080)
                    │  prose / структур. режим (Mermaid+таблицы+связи)
                    ▼
                [3/6] cleanup — TOC-лидеры, {N}----- → метки страниц ⌗p.N
                    ▼
                [4/6] QC-гейт (кириллица/латиница ≥55%, лидеры, переносы, таблицы)
                    ▼
                [5/6] ретрай force_ocr (только --retry-ocr) ──► [6/6] готово
 outbox/<имя>/<имя>.md + фигуры + _meta.json          (.qc_pass = resume-маркер)
```

- Пакетная обвязка: `launch_batch.sh` (видимое окно терминала для пользователя)
  → `prepare_all.sh` (resume, ETA по `logs/timings.jsonl`, фрагменты, склейка).
- Обратный канал качества: метки страниц `⌗p.N` — из ответа БЗ открыть исходную
  страницу PDF; авто-ремонт опирается на них как на границы секций.
- Вэб-страницы: Obsidian Web Clipper → `inbox/*.md` → `prepare_web.sh`
  (captioning + QC, без marker).

## Возможности — какую задачу решает каждая

| # | Возможность | Решаемая задача |
|---|---|---|
| 1 | **Конвертация с сохранением контента** — marker + surya-2: layout, таблицы (текст-слой → table-OCR → фолбэк), полнотраничный OCR сканов и битых шрифтов | тихая потеря текста в CS; битые шрифты без ToUnicode; сканы |
| 2 | **QC-гейт** (`qc_check.py`) — доля целевого алфавита ≥55% среди букв, TOC-лидеры, переносы, таблицы; язык ru/en: флаг > env > авто-детект | «мусорный, но непустой» текст проходит в CS как успех — здесь это FAIL |
| 3 | **VLM-описания фигур** (`caption_figures.py`) — локальная 35B быстрее облака; структур. режим: Mermaid-диаграммы (валидация mmdc), таблицы значений, «Связи:» | графика в RAG ненайдома — описание текстом делает её искомой; диаграммы воспроизводятся схемой |
| 4 | **Метки страниц PDF** `⌗p.N` (деф. ВКЛ, `--no-page-anchors`) | привязка «чанк БЗ → страница исходника»: метка переживает чанкинг, грепается |
| 5 | **Авто-ремонт таблиц** (`table_repair.py`) — «Table OCR failed» в логе → fast-ретрай страницы → вставка в конец секции страницы; + VLM-реконструкция сложных случаев | потерянные таблицы восстанавливаются автоматически, вставка точная (по секциям страниц) |
| 6 | **Фрагментация + автосклейка** (`--fragments K`, `merge_fragments.py`) — большие файлы кусками; посимвольная проверка вхождения частей, ссылки фигур, объединение `_meta.json`, мягкий QC | огромные книги (400+ стр.) на потребительском ПК; перегрев; resume с места обрыва |
| 7 | **Гибридный OCR-стек** (`ocr_proxy.py`, `--ocr-proxy`) — OpenAI-совместимый прокси за алиасом surya: layout/table_rec = surya, block-OCR = PaddleOCR-VL (3–5× быстрее, устойчивее на repetition-loop) | скорость block-OCR без потери качества; A/B-сравнение движков (`ab_block_ocr.py`) |
| 8 | **Batch-обвязка** — `launch_batch.sh`/`prepare_all.sh`: resume (`.qc_pass`), ETA по медиане таймингов, факт-скорость по `/stats` прокси, сводная таблица PASS/FAIL, `--fragment-pause` (охлаждение) | пакетная обработка без присмотра, мониторинг агентом по `logs/` |
| 9 | **Вэб-путь** (`prepare_web.sh`) — MD из Obsidian Web Clipper → captioning + cleanup + QC | одинаковая гарантия качества для вэб-клипов |

## Быстрый старт

```bash
cp .env.example .env          # пути к GGUF-моделям (+ опц. облако captioning)
uv sync                       # .venv (marker-pdf), глобальный Python не трогается
./services.sh start --ocr-proxy   # surya :8510 + Paddle :8512 + Qwen :8080

cp файл(ы).pdf inbox/
scripts/launch_batch.sh       # пакет: видимое окно, логи logs/batch_latest.*
./services.sh stop
# результат: outbox/<имя>/<имя>.md — грузить в БЗ через UI Cherry Studio
```

Примеры вызовов (полный набор — USAGE §2.0–2.2):

```bash
scripts/launch_batch.sh                    # всё, что в inbox/
scripts/launch_batch.sh --pages "1-10"     # проба: первые 10 стр. каждого файла
scripts/launch_batch.sh --fragments 4 --fragment-pause 120   # большой файл кусками
scripts/launch_batch.sh --force --retry-ocr --lang en       # перегон + OCR-ретрай
scripts/prepare_doc.sh --pages "5-20" <pdf> # один файл, диапазон
CAPTION_STRUCTURED=1 scripts/launch_batch.sh  # структур. VLM-описания (Mermaid)
```

## Компоненты

| Компонент | Роль |
|---|---|
| [marker-pdf](https://github.com/datalab-to/marker) 2.x (Apache-2.0) | ядро конвертации: layout, таблицы, пагинация (`paginate_output` → `⌗p`) |
| [surya-2](https://github.com/datalab-to/surya) (GGUF, OpenRAIL-M) | OCR/layout VLM; локально: `/media/llm/Models/OCR/surya-ocr-2-gguf` |
| [llama.cpp](https://github.com/ggml-org/llama.cpp) (Vulkan) | inference-серверы: surya `:8510`, Paddle `:8512`, Qwen `:8080` |
| Qwen3.6-35B-A3B (GGUF Q6, mmproj-F32) | captioning/VLM-описания; p=2 (5,4 с/фигура) |
| PaddleOCR-VL-1.6 (GGUF) | block-OCR за прокси (деф. выключен: `OCR_PROXY_BLOCK_ENGINE=surya`) |
| poppler (`pdfinfo`, `pdftoppm`, `pdftotext`) | метаданные, рендеры, текст-слой |
| node + mmdc (Mermaid CLI) | валидация Mermaid в структур. режиме captioning |
| uv | изолированное окружение проекта (`.venv`, `uv.lock`) |
| opencode + skill `.opencode/skills/docprep/` | агент ведёт пайплайн (версионируется в репо) |

## Особенности железа и ограничения

- Strix Halo (gfx1151): **vLLM/ROCm неприменимы** — GPU-инференс только через
  llama.cpp/Vulkan. Системный ROCm не нужен.
- marker умеет спавнить свой llama-server на случайном порту, если `:8510` занят/
  недоступен (сирота ест VRAM) — поднимай сервисы через `services.sh` до запуска.
- Границы Cherry Studio: открытых API добавления материалов нет — финальная
  загрузка `.md` в БЗ через UI; повторно проверить реальными поисковыми запросами.
- Смена набора сервисов (`--ocr-proxy`, `--surya-*`) — только через явный
  `./services.sh stop` → `start` (restart с `--no-llm` не трогает surya).

## Документы

| Документ | Что внутри |
|---|---|
| [docs/USAGE.md](docs/USAGE.md) | главный справочник: сценарии, все флаги, замеры, реестр требований |
| [docs/HANDOFF.md](docs/HANDOFF.md) | статус, открытые вопросы, следующие шаги, грабли |
| [docs/PLAN-VLM.md](docs/PLAN-VLM.md) | план структурированных VLM-описаний (Mermaid/DOT) |
| [docs/COMPARISON.md](docs/COMPARISON.md) | сравнение с Docling/DeepDoc, аудит БЗ Cherry Studio |
