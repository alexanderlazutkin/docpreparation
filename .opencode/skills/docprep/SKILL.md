---
name: docprep
description: Подготовка PDF к загрузке в базу знаний Cherry Studio — конвертация в Markdown через marker (uv-проект), QC-гейт (кириллица/таблицы/переносы), выбор режима fast/balanced/force_ocr, локальный GPU через llama.cpp/Vulkan (Strix Halo). Использовать при задачах «конвертируй PDF», «подготовь документ для БЗ», разборе QC FAIL.
---

# docprep: PDF → Markdown для базы знаний Cherry Studio

## Контекст окружения
- Репозиторий: `~/Git/RAG/docpreparation` (uv-проект; marker-pdf в `.venv`, глобальный Python не трогать)
- GPU: Radeon 8060S через **llama.cpp/Vulkan** (spawn `llama-server` происходит автоматически при первом обращении к surya; vLLM/ROCm не используется)
- Веса surya-2 GGUF — локально (`/media/llm/Models/datalab-to/surya-ocr-2-gguf`, проверены байт-в-байт с HF), пути задаются в `.env` через `SURYA_GGUF_LOCAL_MODEL_PATH`/`SURYA_GGUF_LOCAL_MMPROJ_PATH`
- Исходники кладём в `inbox/`, результаты появляются в `outbox/` — оба каталога вне git

## Базовый цикл (начинать всегда с него)
```bash
cp файл.pdf inbox/
scripts/prepare_doc.sh inbox/файл.pdf   # balanced → QC → авто-ретрай --force_ocr
```
Скрипт печатает `QC PASS`/`QC FAIL` и путь к `.md`. При FAIL даже после `--force_ocr` — разбор причин ниже.

## Выбор режима marker_single
| Ситуация | Флаги |
|---|---|
| Чистый цифровой PDF | `--mode fast` (текст-слой, VLM почти не запускается) |
| Битые шрифты (кириллица ≈ 0% в QC) | `--force_ocr` |
| Скан | auto full-page OCR или `--force_ocr` |
| Кросс-страничные таблицы, формы, инлайн-математика | `+ --use_llm` (llama.cpp `:8080`; полные флаги — README, раздел 4) |
| Тест сложного фрагмента | `--page_range "5-10"` |
| Только таблицы | `--converter_cls marker.converters.table.TableConverter` |

## QC-гейт (`scripts/qc_check.py`) и лечение FAIL
| Причина FAIL | Действие |
|---|---|
| Кириллица < 55% | `--force_ocr` (шрифты без ToUnicode; обычно уже сделано ретраем) |
| Точечные лидеры оглавления | вырезать блок оглавления из `.md` вручную или прогнать без страниц оглавления (`--page_range`) |
| Переносы не склеены | постправка (sed/Qwen) или `--use_llm --block_correction_prompt "склеивай переносные русские слова…"` |
| Таблицы не распознаны | спот-чек страницы: `pdftoppm -png -r 100 -f N -l N file.pdf`; при провале — `--use_llm` или TableConverter |

## Самопроверка сложных документов
1. `pdftoppm -png -r 100 -f N -l N` страницу → сверить кусок `.md` с рендером глазами.
2. Локальная правка `.md` (переносы, склейка таблиц) → повторный `uv run python scripts/qc_check.py <md>`.

## Правила
- Готовый `.md` в БЗ Cherry Studio добавляется **только через UI** (API добавления материалов нет).
- `inbox/`, `outbox/`, `.env` никогда не коммитить (уже в `.gitignore`).
- Первый запуск marker в сессии поднимает `llama-server` сам — в логе должно быть `Vulkan0: AMD Radeon 8060S`; если llama-server отсутствует, см. README «Этап 1».
