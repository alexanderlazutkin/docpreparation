# HANDOFF: Cherry Studio 2.1.4 — анализ качества БЗ → пайплайн подготовки документов

> Для продолжения в новой сессии. Осн. документ: `README.md` (план, стек, QC, автоматизация).

## Контекст задачи
Пользователь (Joel) оценивал качество локального RAG Cherry Studio 2.1.4 на русскоязычных
PDF (DAMA-DMBOK, рекомендации Банка России): таблицы, картинки, сканы, битые шрифты.
Цель — преп-пайплайн материалов перед загрузкой в БЗ, предпочтение локальным инструментам.

## Что сделано (завершено)
1. **Анализ 2 БЗ**: `~/.config/CherryStudio/Data/KnowledgeBase/{a01dc538-…,0a2c568e-…}`.
   Схема sqlite v2: `material→content→search_unit→search_text (FTS5 trigram) + embedding`.
   - «DAMA-DMBOK Часть 4»: 94 862 симв., **0.00% кириллицы** — битые шрифты без ToUnicode,
     pdf.js-конвертер потерял текст молча. Poppler из тех же страниц даёт 82.8%.
   - «Часть 3»: склеенные таблицы, переносы не склеены, 46 мусорных чанков <100 симв.
   - У всех чанков `title=None`, `locator=None` (нет якорей страниц).
   - Готовые `.md` индексируются чисто (чанки 500–800 симв., 81% кириллицы) → лучший вход в БЗ.
2. **Реверс app.asar** (`/opt/Cherry Studio/resources/app.asar`, распаковано в `/tmp/opencode/cs/full`):
   - doc→md движки: `local-document` (anydoc; OCR-фолбэк ТОЛЬКО если страница без текста —
     «мусорный, но непустой» проходит; лимит 300 стр.), `open-mineru` (127.0.0.1:8000/file_parse,
     ≤200 МБ), `paddleocr`-API (PaddleOCR-VL-1.5/1.6, PP-StructureV3, ≤50 МБ/100 стр.),
     облако: mineru/doc2x/mistral.
   - OCR: `local-paddleocr` (ppu ONNX, PP-OCRv5/v6, в каталоге есть кириллические модели),
     `tesseract` (нужен пакет tesseract-ocr-rus), `ovocr` (Intel NPU, en/zh).
3. **Сравнение инструментов** (MinerU / Docling / Marker): выбрано —
   ручной путь **Marker** + **llama.cpp** (`--use_llm` через `marker.services.openai.OpenAIService`,
   OpenAI-совместимый endpoint llama-server); авто-путь в CS — **open-mineru + local-paddleocr**.
4. **Написан план**: `/home/lan/Git/RAG/docpreparation/README.md` — этапы 0–6, `qc_check.py`
   (кириллица ≥55%, лидеры оглавления, переносы, таблицы), режимы Marker по типам документов,
   6 способов агентной автоматизации, чеклист приёмки.

## Следующие шаги (по приоритету)
1. Этапы 0–2 из README: `nvidia-smi`, `pip install marker-pdf`, llama.cpp + GGUF
   (qwen2.5-7b-instruct), smoke-тест `marker_single`.
2. «Часть 4» + `--force_ocr` → `qc_check.py` — главный приёмочный тест (кириллица >55%).
3. `mineru-api --port 8000 --backend pipeline`; в CS: документ = open-mineru, OCR = local-paddleocr.
4. Пересоздать БЗ `a01dc538` из свежих .md, проверить реальными поисковыми запросами.
5. Опционально: `prepare_doc.sh` (конвертация→QC→ретрай force_ocr) + skill `docprep`.

## Грабли / примечания
- `sqlite3` CLI отсутствует — читать через `python3` sqlite3; **не переиспользовать курсор
  во вложенных циклах** (итерация обрывается).
- asar: `npx @electron/asar extract` (распаковка в /tmp может быть вычищена — повторить).
- У CS **нет API добавления материалов в БЗ** — только UI; файлы в `raw/` сами не индексируются.
- `hivemind_store` падает без запущенного Ollama (нужны embeddings) — при желании повторить.
- `/home/lan/Git/RAG` — не git-репозиторий.
- У Chronology: версия CS подтверждена 2.1.4 (deb, version.log).

## Ключевые артефакты
| Что | Где |
|---|---|
| План/заметка | `/home/lan/Git/RAG/docpreparation/README.md` |
| Этот handoff | `/home/lan/Git/RAG/docpreparation/HANDOFF.md` |
| Распакованный asar CS | `/tmp/opencode/cs/full` (временно) |
| БЗ пользователя | `~/.config/CherryStudio/Data/KnowledgeBase/{a01dc538…,0a2c568e…}` |
| QC-скрипт | инлайн в README, раздел «Этап 5» |
