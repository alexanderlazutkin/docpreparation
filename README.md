# Подготовка документов для базы знаний Cherry Studio: план и заметка

> Дата: 2026-10-14 · Контекст: Cherry Studio 2.1.4, локальные БЗ (`.cherry/index.sqlite`, schema v2),
> документы на русском (DAMA-DMBOK, рекомендации Банка России): таблицы, картинки, сканы, битые шрифты.

---

## 1. Итоги анализа (почему нужен внешний преп-пайплайн)

Проверка двух БЗ показала:

| Факт | Следствие |
|---|---|
| «Часть 4 (гл.14-17)»: 94 862 симв. в индексе, **доля кириллицы 0.00%** | встроенный PDF-конвертер (pdf.js/anydoc) тихо теряет текст на шрифтах без ToUnicode |
| «Часть 3»: склеенные слова из таблиц, переносы не склеены, 46 мусорных чанков <100 симв. | нет layout-анализа и склейки переносов |
| Прямой PDF-путь: жёсткие окна ~500 симв., оглавление «.....» в индексе | фиксированный чанкинг, нет фильтрации |
| `title=None`, `locator=None` у всех чанков | нет заголовков и привязки к страницам |
| Фолбэк на OCR срабатывает только по признаку «текст пустой» | «мусорный, но непустой» текст проходит как успех |
| Материалы `.md` (внешне подготовленные) индексируются чисто: 500–800 симв., 81% кириллицы, таблицы читаемы | **готовый `.md` — лучший вход в БЗ** |

Вывод: PDF не должен быть рабочим форматом. Целевой формат — Markdown, конвертация
вне Cherry Studio с обязательным контролем качества.

## 2. Выбранный стек

| Роль | Инструмент | Обоснование |
|---|---|---|
| Ручной «золотой» путь | **Marker** (datalab-to/marker) | детект «плохого, но непустого» текста (точечный ремонт блоков в fast-режиме, re-OCR страницы в balanced); CPU-режим на poppler (`pdftext`) — доказанно работает на наших PDF; таблицы: текст-слой + VLM-фолбэк + LLM-склейка кросс-страничных таблиц (через локальный **llama.cpp**); колонтитулы сносятся; `marker_single`/GUI заточены под поштучную обработку |
| Авто-путь в CS | **open-mineru** (локальный MinerU, `http://127.0.0.1:8000/file_parse`) | layout + HTML-таблицы + OCR 109 языков; встройка в настройки CS |
| OCR в CS (картинки) | **local-paddleocr** (PP-OCRv5/v6) | встроен, в каталоге есть кириллические модели `cyrillic_PP-OCRv5_mobile_rec` |
| Не использовать | CS `local-document` | пропускает битый текст, деградация всего документа до OCR при одной скан-странице, лимит 300 стр. |
| Облако (mineru.net, doc2x, mistral) | — | исключено: требование локальности |

Лицензии: код Marker — Apache 2.0; веса surya — OpenRAIL-M (личное использование бесплатно).
Границы размеров в CS: open-mineru ≤200 МБ; paddleocr-движок ≤50 МБ/100 стр.

## 3. План установки (этапы с проверками)

### Этап 0. Аудит железа и окружения
```bash
nvidia-smi                      # GPU есть? → balanced-режим Marker + vLLM
python3 --version               # нужно 3.10+
which llama-server || echo "нет llama.cpp"   # CPU-сервер для surya VLM
docker --version && nvidia-container-cli --version   # для vLLM-бэкенда
```
- GPU ≥8GB VRAM → путь A (vLLM, balanced).
- Только CPU → путь B (llama.cpp, fast-режим; full-OCR больших сканов будет медленным).

### Этап 1. Marker
```bash
pip install marker-pdf
# CPU/Apple: установить llama.cpp (llama-server в PATH), например:
#   grab release https://github.com/ggml-org/llama.cpp/releases
# GPU: docker + NVIDIA Container Toolkit (vLLM поднимется сам при первом запуске)
marker_single --help             # проверка установки
```
Первый запуск скачает веса surya и поднимет inference-сервер автоматически.
Проверка: `marker_single test.pdf --output_dir /tmp/markertest`

### Этап 2. llama.cpp (опционально, для `--use_llm`)
```bash
# 1) установить llama.cpp (release с github.com/ggml-org/llama.cpp/releases, бинарник llama-server)
# 2) скачать GGUF-модель, понимающую русский (например, Qwen2.5-7B-Instruct)
# 3) поднять сервер OpenAI-совместимого API:
llama-server -m qwen2.5-7b-instruct-q4_k_m.gguf --port 8080 --ctx-size 8192
curl -s http://127.0.0.1:8080/v1/models    # проверка
```
Marker подключается к нему через OpenAI-совместимый сервис (см. таблицу режимов ниже).
Альтернатива без правок команд — Ollama (`marker_single in.pdf --use_llm --llm_service ollama`),
но чистый llama.cpp экономнее по RAM и переиспользует тот же бинарник, что нужен для surya-сервера.
Нужен для: склейки таблиц через страницы, сложных форм, инлайн-математики.

### Этап 3. Локальный MinerU (авто-поток в CS)
```bash
pip install -U "mineru[core]"    # или mineru[all]
mineru-api --host 127.0.0.1 --port 8000 --backend pipeline   # детерминированный бэкенд
curl -s http://127.0.0.1:8000/docs | head                    # проверка
```

### Этап 4. Настройки Cherry Studio
- Обработка документов → **Локальный MinerU (open-mineru)** на `127.0.0.1:8000`.
- OCR → **Локальный PaddleOCR** (модель PP-OCRv5+; кириллические модели подгрузятся из каталога).
- Если оставить Tesseract как запасной: `sudo apt install tesseract-ocr-rus`.

### Этап 5. Верификация (обязательный QC-гейт)
Скрипт `qc_check.py` (положить рядом, вызывать после каждой конвертации):
```python
#!/usr/bin/env python3
"""QC: доля кириллицы, артефакты оглавления, переносы, пустые таблицы."""
import re, sys, pathlib
p = pathlib.Path(sys.argv[1]); t = p.read_text(encoding="utf-8")
cyr = len(re.findall(r"[А-Яа-яЁё]", t)) / max(1, len(t))
issues = []
if cyr < 0.55: issues.append(f"КИРИЛЛИЦА {cyr:.1%} < 55% — возможна потеря текста")
if len(re.findall(r"\.{8,}", t)) > 5: issues.append("оглавление/точечные лидеры не вычищены")
if len(re.findall(r"[а-яё]-\n[а-яё]", t)) > 10: issues.append("переносы не склеены")
if t.count("|--") < 2 and t.count("<table") < 2: issues.append("таблицы не распознаны (а ожидались)")
print(f"{p.name}: cyr={cyr:.1%}, {len(t)} chars")
print("QC PASS" if not issues else "QC FAIL:\n- " + "\n- ".join(issues))
sys.exit(1 if issues else 0)
```
Ручной спот-чек: 2–3 таблицы и 1 страница с картинкой против исходного PDF.

### Этап 6. Загрузка в БЗ
Готовый `.md` (и только он) добавлять в БЗ через UI CS. PDF держать как архивный оригинал
рядом (в raw/ попадёт автоматически, индексируется .md). Повторно прогнать поисковый
запрос из реальной практики и оценить релевантность чанков.

## 4. Режимы Marker под типы документов

| Тип | Команда |
|---|---|
| Чистый цифровой PDF | `marker_single in.pdf` (fast: текст-слой, VLM не запускается) |
| Битые шрифты («Часть 4») | `marker_single in.pdf --force_ocr` |
| Скан | `marker_single in.pdf` (auto full-page OCR) или `--force_ocr` |
| Сложные/кросс-страничные таблицы | `+ --use_llm --llm_service marker.services.openai.OpenAIService --openai_base_url http://127.0.0.1:8080/v1 --openai_model qwen2.5-7b-instruct --openai_api_key none` (llama.cpp) |
| Только таблицы из документа | `--converter_cls marker.converters.table.TableConverter` |
| Максимум качества на GPU | `--mode balanced` (+ `--use_llm` с llama.cpp/Ollama) |

LLM-режим: Marker поддерживает Gemini/Claude/Vertex/Azure/OpenRouter/Ollama и **любой
OpenAI-совместимый эндпоинт** (`--llm_service marker.services.openai.OpenAIService` +
`--openai_base_url/--openai_model/--openai_api_key`) — им и пользуемся для llama.cpp.

Полезные флаги: `--paginate_output` (маркеры страниц), `--output_format json`
(блочные метаданные + страницы для provenance), `--disable_image_extraction`,
`--page_range "0,5-10"` (тестовый прогон на отдельных страницах).

## 5. Автоматизация с агентом (способы, по возрастанию сложности)

1. **CLI-обёртка с QC-гейтом** (база): скрипт `prepare_doc.sh <in.pdf>` —
   конвертация → `qc_check.py` → при FAIL авто-ретрай с `--force_ocr` → отчёт.
   Агент (opencode/Claude Code) вызывает один скрипт, читает вердикт.
2. **Batch-обход папки**: `marker /path/in --workers N` + цикл QC по каждому `.md`;
   итоговая сводная таблица PASS/FAIL → агент разбирает только FAILи.
3. **Skill для агента** (`~/.config/opencode/skills/docprep/SKILL.md`): инструкции
   «какой режим под какой тип документа, когда --force_ocr, когда --use_llm, как
   читать qc_check и чинить». Агент получает skill → сам ведёт преп-пайплайн.
4. **Агентный цикл с самопроверкой**: агент конвертирует → сам сверяет куски MD с
   рендером страницы (pdftoppm + чтение как картинка) → локальная правка MD
   (переносы, склейка таблиц) → повторный QC. Самый высокий процент спасения сложных docs.
5. **Сторожевая автоматизация (watch folder)**: systemd path unit / inotifywait на
   `inbox/` → скрипт п.1 → готовый MD в `outbox/` → ручная досыпка в БЗ (у CS нет
   API добавления материалов — финальный шаг через UI).
6. **`--use_llm` с кастомным промптом** (`--block_correction_prompt`): агент
   генерирует промпт под конкретный документ (например, «склеивай переносные
   русские слова, сохраняй таблицы ГЛАВЫ N») и передаёт в Marker — коррекция
   форматирования без ручной постправки. LLM — локальный llama.cpp через
   OpenAI-совместимый сервис.

Рекомендуемый минимум на старт: п.1 + п.2 (полдня работы), затем п.3 по мере
повторяемости задач.

## 6. Чеклист приёмки

- [ ] Marker ставится, `marker_single` проходит на тестовом PDF
- [ ] surya-сервер поднимается (vLLM/llama.cpp) без ручных плясок
- [ ] `llama-server` с GGUF-моделью отвечает на `:8080/v1/models`, Marker с `--use_llm` (OpenAIService) проходит тест
- [ ] «Часть 4» прогнана с `--force_ocr` → QC PASS (кириллица > 55%)
- [ ] «Часть 3» прогнана в fast → таблицы читаемы, спот-чек против PDF
- [ ] mineru-api отвечает на 127.0.0.1:8000, CS проходит тест коннекта
- [ ] В CS: движок = open-mineru, OCR = local-paddleocr
- [ ] Исправленная БЗ переиндексирована, тестовые запросы возвращают релевантные чанки
- [ ] qc_check.py лежит в PATH проекта, вызывается после каждой конвертации

## 7. Ссылки (см. ниже раздел 8 — реализованная структура)

## 8. Реализованная структура репозитория

> Возможности с примерами запуска, рекомендации с замерами и **реестр
> требований → функционал** — в [USAGE.md](USAGE.md).

```
docpreparation/
├── pyproject.toml + uv.lock        # uv-проект, marker-pdf (uv sync; .venv вне git)
├── services.sh                     # запуск/остановка inference-сервисов (llama-server, Vulkan)
├── scripts/
│   ├── prepare_doc.sh              # конвертация → captioning → cleanup → QC (стадии 1/6…6/6)
│   ├── prepare_all.sh              # пакет: inbox/*.pdf, resume, ETA, --pages/--fragments/--retry-ocr
│   ├── qc_check.py                 # кириллица ≥55% (среди букв), лидеры, переносы, таблицы
│   ├── caption_figures.py          # VLM-описания фигур (Qwen3.6-35B) → находимость графики в RAG
│   └── clean_md.py                 # вычистка точечных лидеров оглавления
├── .env.example                    # пути к локальным GGUF surya → cp .env.example .env
├── .opencode/skills/docprep/       # навык агента opencode (версионируется в репо)
├── inbox/                          # исходные PDF (вне git)
└── outbox/                         # готовые .md (вне git)
```

### Особенности этой машины (Strix Halo, Radeon 8060S, ROCm 7.1.4)
- GPU-инференс surya — через **llama.cpp/Vulkan**: marker сам поднимает `llama-server`
  (в логе `Vulkan0: AMD Radeon 8060S`). Путь A (docker + vLLM) **неприменим**:
  vLLM на gfx1151 официально не поддерживается; системный ROCm для marker не нужен.
- Веса surya-2 GGUF уже локально: `/media/llm/Models/datalab-to/surya-ocr-2-gguf`
  (проверены: размеры байт-в-байт с HF Hub, новее lastModified репозитория) —
  подключаются через `.env` (`SURYA_GGUF_LOCAL_MODEL_PATH` / `..._MMPROJ_PATH`), без скачивания.
- Layout-модель fast-режима (`datalab-to/surya_layout2`) при первом fast-прогоне
  скачается в HF-кэш (~сотни МБ) — это нормально.

### Рабочий цикл (инструкция)
```bash
cp .env.example .env                 # один раз: пути к моделям (+ опц. облако для captioning)
./services.sh start                  # 1) модели на GPU: surya :8510 + Qwen3.6-35B :8080
cp файл(ы).pdf inbox/                # 2) исходники
scripts/prepare_all.sh               # 3) обработка ВСЕХ файлов inbox/ (окно терминала:
                                     #    агент запускает через scripts/launch_batch.sh)
./services.sh stop                   # 4) по завершении
```
- Стадии на файл: `[1/6] marker → [2/6] captioning → [3/6] cleanup (лидеры TOC) → [4/6] QC → [5/6] force_ocr-ретрай (только с `--retry-ocr`) → [6/6] готово`. Кириллица в QC считается среди букв (иммунитет к пробельным паддингам таблиц).
- **ETA**: при старте файла/фрагмента — оценка по медиане из `logs/timings.jsonl`
  (сид 8.4 с/стр до накопления истории); в captioning — остаток по скользящему среднему.
- **Диапазон/фрагменты** (тесты и большие файлы):
  `prepare_all.sh --pages "1-30"` — только стр. 1–30; `--fragments 4` — диапазон делится
  на 4 фрагмента, обрабатываются поочерёдно (каждый — `outbox/<имя>__pS-E/`, свой resume;
  QC в мягком режиме без force_ocr-ретрая). Дефолты: S=1, E=0 (до конца), фрагментов=1.
- **Облако для captioning** (ускорение; OCR/layout остаётся локальным на surya):
  раскомментировать в `.env` `CAPTION_API/CAPTION_MODEL/CAPTION_API_KEY` (OpenAI-совместимый,
  напр. OpenRouter с VL-моделью). `--use_llm` marker тоже умеет любое облако (README раздел 4).
- Resume: готовые (`outbox/<имя>[__pS-E]/.qc_pass`) пропускаются; `--force` — переобработать.
- Логи: `logs/<имя>.log` на файл, `logs/batch_<датавремя>.log` сводный; мониторинг агентом —
  `logs/batch_latest.log` (ход) и `logs/batch_latest.result` (итог).
- Результат: `outbox/<имя>/<имя>.md` (+ фигуры, `_meta.json`).

## 9. Ссылки

- Marker: https://github.com/datalab-to/marker · docs: https://documentation.datalab.to
- Surya (модели, языки): https://github.com/datalab-to/surya
- llama.cpp: https://github.com/ggml-org/llama.cpp
- MinerU: https://github.com/opendatalab/MinerU
- Docling (запасной вариант): https://github.com/docling-project/docling
- Cherry Studio: https://github.com/CherryHQ/cherry-studio
