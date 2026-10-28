# COMPARISON — OCR-модели для Strix Halo + наш пайплайн vs Docling vs DeepDoc

> Дата: 2026-10-19. Две части: **§1–8** — обзор открытых OCR-моделей как
> альтернатив surya-2 на нашем железе (первичный обзор 2026-10-14, актуализирован
> нашими реальными тестами 2026-10-14…18, USAGE §3.6); **§9–11** — сравнение
> нашего пайплайна с Docling и DeepDoc (2026-10-16, актуализировано 2026-10-19).
> Контекст: преп-пайплайн «рус. PDF → качественный MD» для Obsidian (человек) и
> RAG/БЗ Cherry Studio; железо локальное (Strix Halo: Ryzen AI Max 395+,
> Radeon 8060S, 128 ГБ UMA; llama.cpp/Vulkan, без CUDA). QC-гейт — средство
> гарантии качества MD, не самоцель.
>
> Поглощённые доки: `сравнение-ocr-strix-halo.md` (2026-10-14, целиком здесь);
> план структурированных VLM-описаний (бывший `PLAN-VLM.md`) — реализован и
> закрыт, актуальное описание режима — USAGE §2.4-С, история —
> `docs/vlm_pilot/report.md`.

## 1. Резюме (TL;DR): что выбрать на Strix Halo

| Если нужно…                                 | Кандидат                       | Почему                                                        | Статус у нас (10.2026)                                                                                                                    |
| ------------------------------------------- | ------------------------------ | ------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------- |
| Рабочая база: layout+OCR+таблицы, кириллица | **surya-2** (0.65B)            | 10,5 с/стр (p=8), cyr ~97%, llama.cpp/Vulkan                  | **принята, OCR-движок по умолчанию**                                                                                                      |
| Быстрый block-OCR (3–5×) с кириллицей       | **PaddleOCR-VL-1.6** (0.9B)    | нативно `<\|ocr\|>` 5,5 с/стр full-page, читает чисто и полно | **удалён из стека (10.2028)**: в дефолте простаивал, как block-движок терял таблицы table-fallback (§3.6-4); нишу block-OCR занял Chandra |
| Потолок точности, преемник surya            | **Chandra OCR 2** (~5.3B)      | «SOTA» Datalab; Qwen3.5-VL; GGUF community + mmproj есть       | **тест завершён (10.2026)**: гейты PASS, modelset 2 — точечный block-OCR (точнее surya на сложных таблицах/IDEF1X, ~8× медленнее на таблице-плотных); дефолт modelset 1; OpenRAIL-M |
| Лёгкая и быстрая на llama.cpp (GGUF офиц.)  | **HunyuanOCR-1.5** (0.9B)      | официальный GGUF + llama-server, DFlash в апстриме            | не тестировалась; кандидат на A/B против surya                                                                                            |
| Максимальная точность специализированных    | **dots.mocr** (3B)             | топ olmOCR-bench (83.9) / OmniDocBench                        | не тестировалась: CUDA/vLLM-центрична → на SH только transformers+ROCm                                                                    |
| Конвейер «поставил и работает»              | **MinerU 4.0**                 | дефолт: ONNX CPU + llama.cpp Vulkan — целевой iGPU            | этап 3 README (не брали: своя обвязка уже есть)                                                                                           |
| Чистая скорость на CPU                      | **PP-OCRv6** (1.5–34.5M)       | детекция+распознавание, 5.2× ускорение (OpenVINO)             | не тестировалась                                                                                                                          |
| Не подходят без костылей                    | **olmOCR 2**, **DeepSeek-OCR** | пайплайны завязаны на vLLM/CUDA                               | не брали                                                                                                                                  |
|                                             | **GLM-OCR** (0.9B)             | см. §3.3                                                      | **испытана и отклонена** (кириллица, скорость)                                                                                            |

Ключевой практический итог наших тестов: **проблема surya на SH — не модель,
а отсутствие vLLM**; llama.cpp-путь (Vulkan) работоспособен и выверен, а
несовместимость чужих моделей со промптами-контрактами marker/surya решается
не «drop-in подменой», а **адаптером-прокси** (`scripts/ocr_proxy.py`),
маршрутизирующим типы запросов по движкам.

## 2. Базовая точка: surya-2 (datalab-to/surya)

- Архитектура: один VLM ~650M (в духе Qwen3.5) на всё — OCR, layout, порядок
  чтения, распознавание таблиц; детекция строк — отдельная torch-модель.
- Точность: 83.3% на olmOCR-bench (лучший <3B по заявлению Datalab); 87.2% на
  внутреннем бенче 91 языка; русский в топ-15. Наши замеры: кириллица 97,1%
  на полном PDF (39 стр.) через адаптер; таблицы — спот-чек без потерь.
- Скорость у нас: **10,5 с/стр** при `--parallel 8` (оптимум; p=16 хуже на
  57%; параллельные фрагменты выигрыш дают только при p<8 — USAGE §3.5).
- Запуск: vLLM (только NVIDIA) или llama.cpp (у нас Vulkan-сборка; связка
  зафиксирована на llama-server 0.6.0-dev build 11541 — свежие ломаются на
  `\d` в bbox-грамматике surya #542, см. HANDOFF «Грабли»).
- Лицензия: код Apache-2.0; веса — модифицированный AI Pubs OpenRAIL-M
  (бесплатно до $5M оборота/финансирования).
- Подмена OCR-VLM за алиасом (тесты других моделей): `services.sh
  --surya-model/--surya-mmproj/--surya-parallel` + `OCR_PROXY_BLOCK_ENGINE`.

## 3. Окружение Strix Halo: что важно для OCR-моделей

1. **Нет CUDA → нет vLLM.** vLLM не поддерживает gfx1151. Модели, чей основной
   путь «vllm serve …» (dots.mocr, DeepSeek-OCR, olmOCR, Chandra в
   рекомендованном режиме), на SH запускаются лишь через transformers+ROCm —
   с потерей скорости и отладкой на свою ответственность.
2. **ROCm для APU — отдельный трек.** gfx1151 отсутствует в основной таблице
   Supported GPUs; для Ryzen APU — ветка «ROCm on Radeon and Ryzen» со своим
   инсталлятором и PyTorch для Ryzen APU (до 7.2.1, дальше объединена с
   основной). Рабочо, но «второй класс»; проверять колёса под каждую версию.
3. **llama.cpp Vulkan — родной путь для 8060S.** Официально выбран: MinerU 4.0
   (дефолт), HunyuanOCR-1.5 (GGUF из коробки), surya 2 (один из двух
   бэкендов), наш пайплайн. Всё, что есть в GGUF, запускается.
4. **128 ГБ единой памяти.** Влезает всё до Chandra 2 (~11B bf16 ≈ 22 ГБ + KV)
   с запасом; у нас одновременно живут 35B-LLM (32,7 ГБ) + 2 OCR-VLM.
5. **Узкое место — пропускная способность (~256 ГБ/с, делится CPU+iGPU) и нет
   тензорных FP8/FP4.** Decode больших VLM медленнее, чем на RTX 4090/5090;
   конвейерные ONNX-модели (детекция+распознавание) страдают меньше.
6. **NPU (XDNA2, ~50 TOPS) перечисленными стеками не используется.**
7. **16 ядер Zen 5** дают приличный CPU-путь (ONNX/OpenVINO, llama.cpp-CPU).

## 4. Каталог альтернатив

### 4.1 dots.mocr (ex dots.ocr-1.5) — rednote-hilab / studio-dots-ai
- 3B мультимодальная модель разбора страниц (текст/строки/формулы/таблицы, вкл.
  межстраничные); плюс dots.mocr-svg (диаграммы → SVG). Русский — в витрине.
- Цифры (README, Elo-судья Gemini 3 Flash): olmOCR-bench **83.9±0.9** (выше
  Chandra 1 — 83.1, olmOCR v0.4.0 — 82.4, PaddleOCR-VL — 80.0, MinerU 2.5.4 —
  75.2, DeepSeek-OCR — 75.7); OmniDocBench v1.5: TextEdit **0.031** /
  ReadOrderEdit 0.029 — лучший; Elo-среднее **1124.7** (PaddleOCR-VL-1.5 —
  920.5, HunyuanOCR — 984.2; закрытый Gemini 3 Pro — 1210.7).
- Запуск: vLLM или transformers — CUDA-центрично; GGUF не заявлен. Лицензия в
  README не указана (v1 была Apache-2.0) — проверить перед внедрением.
- **У нас:** не тестировалась; главный «лучше surya» по точности, но самый
  неудобный по запуску (transformers+ROCm, ~3B).

### 4.2 PaddleOCR-VL-1.6 (+ PP-OCRv6) — PaddlePaddle, Apache-2.0
- Семейство: VL-1.6 — 0.9B VLM (NaViT + ERNIE-4.5-0.3B), полный разбор
  страницы в MD/JSON; PP-OCRv6 — классический конвейер (tiny 1.5M / small
  7.7M / medium 34.5M). 111 языков вкл. кириллицу.
- Цифры: VL-1.6 — **96.3% OmniDocBench v1.6** (v1.5: 94.5%); v1 — 80.0 на
  olmOCR-bench; PP-OCRv6: +4.6% детекция / +5.1% распознавание против
  PP-OCRv5_server, 5.2× ускорение CPU (OpenVINO). Бэкенды: Paddle,
  transformers (с 3.5.0), ONNX, OpenVINO — максимально переносимые.
- **У нас — ИСПЫТАНА, принят частично (USAGE §3.6, аддендумы 1–4):**
  - drop-in за алиасом surya — НЕ работает: llama-server навязывает
    json_schema → Paddle отвечает пустым `[ ]` → пустые страницы; в
    промптах-контрактах marker bbox массивом вместо строки → блоки skip.
  - нативный режим `<|ocr|>` (GGUF, llama.cpp): читает чисто и полно,
    **5,5 с/стр full-page** (surya — 10,5).
  - адаптер-прокси `scripts/ocr_proxy.py` (:8511; `services.sh start
    --ocr-proxy`): layout/table_rec → surya (pass-through), block-OCR →
    по `OCR_PROXY_BLOCK_ENGINE`. A/B (`ab_block_ocr.py`): Paddle **3–5×
    быстрее** surya при равной кириллице, устойчивее на тяжёлой таблице
    (у surya repetition-loop). Полный PDF через адаптер: QC PASS, cyr 97,1%.
  - НО: block-OCR table-fallback (Table 4.19 DMRB) — Paddle отдал plain без
    `<table>` → таблица теряна. Поэтому **дефолт block-engine = surya**
    (pass-through, безопасно), Paddle — опция для экспериментов.
    **2026-10-28: Paddle удалён из стека** (в дефолте простаивал; нишу
    качественного block-OCR занял Chandra, §4.5).

### 4.3 MinerU 4.0 — OpenDataLab
- Комбайн: PDF/изображения/Office/EPUB/HTML → MD/JSON, сервер, агентский CLI;
  4 уровня качества; Office парсит нативно (flash).
- Ключевое для SH: дефолт «малые модели ONNX CPU, VLM — **llama.cpp Vulkan**,
  good compatibility with modern iGPU»; basic = ONNX + llama.cpp; advanced =
  + vLLM/lmdeploy (нужна NVIDIA 8GB+ или Apple MPS).
- Точность: 75.2 на olmOCR-bench (конвейер 2.5.4); MinerU2.5 TextEdit 0.047.
- Лицензия: «MinerU Open Source License» — Apache 2.0 с доп. условиями.
- **У нас:** не брали — своя обвязка уже построена; записан этапом 3 README.

### 4.4 HunyuanOCR-1.5 — Tencent Hunyuan
- Лёгкая (0.9B) сквозная OCR-VLM; 1.5 (07/2026) ради скорости: DFlash
  speculative decoding (влит в апстрим llama.cpp), «Agentic Data Flow».
- Ключевое для SH: **официальный GGUF (base + mmproj) и llama-server** с
  OpenAI-совместимым API — целевой сценарий «CPU / consumer-GPU / laptop».
  Elo-среднее 984.2. Лицензия: Hunyuan Community (не OSI — проверить).
- **У нас:** не тестировалась; естественный следующий кандидат на A/B против
  surya-2 тем же путём (GGUF + `--surya-model` подмена или прокси).

### 4.5 Chandra OCR 2 — Datalab (старший брат surya)
- «SOTA» OCR того же автора: изображения/PDF → HTML/MD/JSON; сложные таблицы,
  формы с чекбоксами, рукопись, 90+ языков; вершина olmOCR-bench по их
  замерам (~2 стр/с на H100). Chandra 1 — 11B на Llama-3.2-Vision;
  **Chandra 2 — Qwen3.5-VL ~5,3B (факт-чек 2026-10-21: safetensors 10,6 ГБ
  bf16, F16 GGUF 9,7 ГБ)**,HF `datalab-to/chandra-ocr-2`
  (arch `Qwen3_5ForConditionalGeneration`).
- Запуск: vLLM (рекомендован) или transformers. Лицензия: код Apache-2.0,
  веса — модифицированный OpenRAIL-M (бесплатно до $2M; **коммерческий
  селф-хостинг платный**).
- **llama.cpp (проверено 2026-10-21):** arch `qwen3_5` поддержана в стабильном
  v0.7.0+; наш текущий b11734 УЖЕ = v0.7.0 (nightly от 19.10, ставился
  `~/AI/install-llama-cpp.sh`) и arch `qwen35`/`qwen35moe` содержит —
  обновляться не нужно. Ловушка диагностики: arch-таблица в `libllama.so`
  сборки, а НЕ в крошечном (17 КБ) лаунчере `llama-server`. Непроверено лишь
  одно: mtmd/mmproj-погрузчик vision-башни — решит первая загрузка модели.
  Своя конверсия не нужна — community GGUF с mmproj:
  `mradermacher/chandra-ocr-2-GGUF` (Q8_0 5,2 ГБ + mmproj 0,37 ГБ —
  выбор 2026-10-21), запасные: `prithivMLmods/…` (12,5k ↓),
  `dougvk/…-BF16-RDNA4` (BF16 + SHA256SUMS).
- **У нас: тест завершён (2026-10-23…28).** Гейты PASS (кириллица 97,5–100%,
  таблицы DMRB полной сеткой, 13,6 с/стр @ parallel 4 = 1,3× surya);
  интеграция — modelset 2 в `services.sh` + движок в `ocr_proxy.py`
  (USAGE §3.7). Пилот качества (Chmura/Heumann 234 стр.): IDEF1X верно,
  сложные таблицы читает лучше surya (у того repetition-loop). A/B на
  таблице-плотном TPC-H (138 стр.): block-OCR **~8× медленнее surya**
  (598 блоков/7 мин vs 567/55 мин), ms2-прогон прерван. Итог: **MODELSET=1
  дефолт, Chandra — точечный block-OCR** (когда включать modelset 2 —
  таблица в USAGE §3.7). Лицензия OpenRAIL-M для своего кейса — ок до $2M.

### 4.6 olmOCR 2 (v0.4.0) — Allen AI, Apache-2.0
- 7B VLM (тонкая настройка Qwen2.5-VL), 82.4 olmOCR-bench, FP8-дефолт.
- Пайплайн: vLLM + CUDA 12.8 + flashinfer, NVIDIA ≥12 ГБ — на SH
  несобираемое окружение; обход — голая Qwen2.5-VL GGUF в llama.cpp (уже не
  olmOCR). **Не брали.**

### 4.7 DeepSeek-OCR / DeepSeek-OCR2
- 3B MoE «оптического сжатия контекста»; 75.7 olmOCR-bench; TextEdit 0.073.
  OCR2 (анонс 2026-01-27, отдельный репо) — не изучался.
- vLLM/transformers, CUDA-центрично; как парсер документов уступает
  специализированным лидерам. **Не брали.**

### 4.8 GLM-OCR (0.9B, Zhipu) — испытана и отклонена (2026-10-14…15)
- Drop-in тест за алиасом surya: несовместима с промптами-контрактами surya
  (bbox массивом вместо строки → блоки skip, потеря ~70% текста, пустые
  страницы) и **медленнее** surya-2 (20,5 против 10,5 с/стр; p=4×2 фрагмента
  не помогли). Аддендум: параметры ни при чём — проблема в самих контрактах;
  нативная кириллица слабая. **Отклонена, веса удалены (−2,5 ГБ).**

### 4.9 Кратко: замеченные, но не разобранные
MonkeyOCR-pro-3B, OCRVerse-4B, Infinity-Parser-7B (82.5 olmOCR-bench),
Nanonets-OCR2-3B, LightOnOCR-2-1B. Кандидаты для отдельной проверки, если
лидеры не устроят.

## 5. Сводная таблица моделей

| Модель | Параметры | Лицензия (веса) | olmOCR-bench | OmniDocBench v1.5 (TextEdit↓) | Русский | Пути запуска | Пригодность для SH / статус у нас |
|---|---|---|---|---|---|---|---|
| **surya 2** | 0.65B | OpenRAIL-M (модиф.) | 83.3 | — | да (топ-15) | vLLM/NV, llama.cpp, torch | **рабочая / OCR-движок, 10,5 с/стр, cyr 97%** |
| PaddleOCR-VL-1.6 | 0.9B | Apache-2.0 | 80.0 (v1) | 0.035 (v1/1.5); **96.3% v1.6** | да (111 языков) | Paddle, transformers, ONNX, GGUF | **высокая / удалён из стека (10.2028): block-OCR 3–5× быстрее, но table-fallback терял таблицы; нативно 5,5 с/стр full-page — кандидат быстрого пути** |
| HunyuanOCR-1.5 | 0.9B | Hunyuan Community | — (Elo 984) | — | да | vLLM, transformers, **GGUF + llama.cpp (офиц.)** | **максимальная / не тестирована — кандидат A/B** |
| MinerU 4.0 | конвейер + VLM | MinerU OSL | 75.2 (2.5.4) | 0.047 (2.5) | да | **ONNX CPU + llama.cpp Vulkan (деф.)**, torch, vLLM | **макс. «из коробки» / не брали (этап 3 README)** |
| dots.mocr | 3B | не указана (v1: Apache-2.0) | **83.9** | **0.031** | да (примеры) | vLLM/CUDA, transformers | средняя (только ROCm) / не тестирована |
| Chandra OCR 2 | ~5.3B | OpenRAIL-M (модиф.) + платный селф-хост | топ (заявл.) | — | да (90+) | vLLM, HF/torch, **llama.cpp GGUF+mmproj** | **высокая / modelset 2 — точечный block-OCR**: точнее surya (IDEF1X, сложные таблицы), ~8× медленнее на таблице-плотных |
| GLM-OCR | 0.9B | — | — | — | слабо | llama.cpp (GGUF) | **отклонена по тесту** (20,5 с/стр, кириллица, контракты) |
| olmOCR 2 | 7B | Apache-2.0 | 82.4 | — | огранич. | vLLM/CUDA (NV ≥12 ГБ) | **низкая / не брали** |
| DeepSeek-OCR (v1) | 3B MoE | MIT (историч.) | 75.7 | 0.073 | частично | vLLM/CUDA, transformers | низкая / не брали |
| PP-OCRv6 (конвейер) | 1.5–34.5M | Apache-2.0 | — | — | да | ONNX/OpenVINO/CPU, Paddle | **высокая (CPU-скорость) / не тестирован** |

Ориентиры скорости на SH (экстраполяции, кроме измеренного): конвейерные
CPU-модели — доли…~0,5 с/стр; 0.65–0.9B VLM Vulkan — единицы с/стр (surya —
**10,5 с/стр измерено** с layout-проходом; Paddle нативный full-page —
**5,5 с/стр измерено**); 3B — ~5–15 с/стр; 7B GGUF Q4 — ~5–15 с/стр; ~11B —
десятки с/стр.

## 6. Как читать бенчмарки (честные оговорки)

1. **olmOCR-bench** (Allen AI) — англо-центричен; **OmniDocBench**
   (v1.5/v1.6) — существенно китайско-центричен; **Elo-таблицы** (dots) —
   LLM-судья, чувствителен к промпту/формату.
2. Сравнивать модели **только внутри одного бенчмарка**; «83.9 в
   olmOCR-bench» и «96.3% в OmniDocBench v1.6» несопоставимы.
3. Таблицы в README вендоров — компиляции в пользу своих моделей;
   независимой консолидированной таблицы нет.
4. **Для русского языка** публичных детальных цифр почти нет у всех —
   финальный выбор проверять на своём корпусе. Мы проверяли: кириллица
   surya-2 ~97% на наших PDF с битыми шрифтами (QC-гейт рулит).

## 7. Рекомендации по сценариям (это железо) — статус 2026-10-19

1. [x] **Гибридный OCR-стек** — сделано: прокси `ocr_proxy.py` (pass-through
   на surya; block-OCR → surya|chandra). Paddle как block-движок был опцией,
   удалён 2026-10-28 (ломал table-fallback, в дефолте простаивал).
2. [ ] **Быстрый A/B без смены стека:** HunyuanOCR-1.5 GGUF против surya-2
   (тот же llama.cpp/Vulkan; подмена `--surya-model` или прокси).
3. [ ] **Максимальная точность:** dots.mocr через transformers+ROCm — замер
   приемлемой скорости на 3B.
4. [x] **Преемник surya:** Chandra OCR 2 — тест завершён (10.2026): гейты
   PASS, интеграция modelset 2; итог — точечный block-OCR (точнее surya на
   сложных таблицах/IDEF1X, ~8× медленнее на таблице-плотных), дефолт
   modelset 1 (детали §4.5, USAGE §3.7); лицензия OpenRAIL-M — ок до $2M.
5. [ ] Пакетная скорость на CPU / фоновые задачи: PP-OCRv6 (ONNX/OpenVINO).
6. [x] Не тратить время на olmOCR/DeepSeek-OCR/GLM-OCR — проверено/ясно.

## 8. Источники

- Surya: https://github.com/datalab-to/surya — Chandra: https://github.com/datalab-to/chandra
- PaddleOCR: https://github.com/PaddlePaddle/PaddleOCR (VL-1.6, PP-OCRv6)
- MinerU: https://github.com/opendatalab/MinerU — dots.ocr: https://github.com/rednote-hilab/dots.ocr
- HunyuanOCR: https://github.com/Tencent-Hunyuan/HunyuanOCR
- olmOCR: https://github.com/allenai/olmocr — DeepSeek-OCR: https://github.com/deepseek-ai/DeepSeek-OCR
- ROCm: https://rocm.docs.amd.com/projects/install-on-linux/en/latest/reference/system-requirements.html
  и трек APU: https://rocm.docs.amd.com/projects/radeon-ryzen/en/latest/
- Наши тесты: USAGE §3.5 (замеры surya), §3.6 (GLM-OCR/Paddle/адаптер,
  аддендумы 1–4); первичный обзор собран прямыми загрузками README
  2026-10-14 (веб-поиск был недоступен).

---

# Часть 2: наш пайплайн vs Docling vs DeepDoc (RAGFlow)

## 9. Сводная таблица

| Ось              | Наш пайплайн (marker/surya + обвязка)                                                                                                               | Docling (IBM, MIT, LF AI)                                                                             | DeepDoc (модуль RAGFlow, Apache-2.0)                                                        |
| ---------------- | --------------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------- |
| Что это          | CLI-конвейер: marker (или CHM→MD) + captioning + cleanup + QC                                                                                       | Библиотека-конвертер (Python/PyTorch), CLI, docling-serve, MCP                                        | Модуль парсинга внутри RAGFlow (Go+ONNX/pdfium); самостоятельного продукта нет              |
| Ниша             | Рус. PDF → качественный MD (Obsidian + RAG), локально                                                                                               | Универсальный парсер многих форматов                                                                  | Внутренний парсер RAG-платформы: блоки+позиции → чанки                                      |
| Форматы          | **PDF + CHM** (`chm_to_md.py`; веб-страницы через Web Clipper + `prepare_web.sh`); прочие источники — кандидат Docling                              | PDF, DOCX, PPTX, XLSX, HTML, EPUB, email, ODF, XBRL, картинки, аудио/видео (ASR)                      | PDF, DOCX, XLSX, PPTX, картинки (в рамках RAGFlow)                                          |
| OCR              | surya-2 (10,5 с/стр, cyr ~97%) + PaddleOCR-VL block-OCR через прокси-адаптер                                                                        | EasyOCR / Tesseract / RapidOCR (`cyrillic`, `eslav` PP-OCRv5) / Nemotron-OCR / KServe                 | Собственные PaddleOCR-производные det/rec ONNX — **фокус zh/en, кириллических моделей нет** |
| Layout / таблицы | surya layout + table-fallback, точечный block-OCR, VLM-ремонт дыр («verify»)                                                                        | DocLayNet-layout + **TableFormer** (силён на цифровых PDF)                                            | Свой DLA + TSR (ONNX), таблицы структурированно                                             |
| VLM              | Qwen3.6-35B captioning: prose + **structured-режим** (Mermaid по типу схемы + текстовые «Связи:»; таблицы-реконструкции с пометкой «сверить с PDF») | GraniteDocling 258M (страница → DocTags; известная болезнь — повторы)                                 | Нет на уровне deepdoc (LLM-постобработка выше по стеку)                                     |
| Железо у нас     | llama.cpp/**Vulkan** — родной для Strix Halo (35B + 2 OCR-VLM одновременно)                                                                         | PyTorch CUDA/MPS; ROCm gfx1151 в wheels нет → **CPU-only**; VLM-путь формально через llama.cpp-сервер | ONNX Runtime, CPU-ориентирован; GPU только CUDA-EP → у нас **CPU**                          |
| Выход            | Готовый `.md` (+ фигуры, `_meta.json`, **метки страниц `⌗p.N`** для ссылок из БЗ на исходник)                                                       | MD / HTML / JSON / DocTags                                                                            | Чанки с позициями для retrieval (MD — не самоцель)                                          |
| QC               | **Гейт, языкозависимый** (`--lang ru/en`: кириллица/латиница ≥55% среди букв), лидеры TOC, переносы, таблицы; retry-ocr                             | Нет                                                                                                   | Нет                                                                                         |
| Обвязка          | Фрагменты+склейка+посимвольные проверки, resume, ETA, факт-скорость, batch-сводка, `--redescribe` (апгрейд описаний без перегона marker)            | docling-serve (API/очередь), chunking, интеграции LangChain/LlamaIndex                                | Вся платформа RAGFlow: чанкеры, индексы, citation, агенты                                   |
| Рус. язык        | Приёмка: 39 стр. полный PASS; СиММА 71 стр. cyr 90,1%; RU-доки ЦБРФ — все PASS; EN-книги через `--lang en` (латиница 100%)                          | Layout/TableFormer обучены гл.образом на англ.; OCR cyr возможен (RapidOCR)                           | Модели заточены под zh/en; в лидерах OmniDocBench не фигурирует                             |
| Лицензии         | Код Apache-2.0; **веса surya OpenRAIL-M**                                                                                                           | MIT (код и базовые модели)                                                                            | Apache-2.0                                                                                  |

## 10. Детали

### 10.1 Docling — сильное / слабое
- **Сильное:** форматная широта (лучший вариант для DOCX/PPTX/XLSX/HTML/EPUB),
  полностью MIT, экосистема (serve-API, MCP, chunking, интеграции),
  TableFormer на чистых цифровых таблицах, активная разработка (IBM/LF AI).
- **Слабое для нас:** на Strix Halo — CPU-only (нет CUDA; ROCm gfx1151 не
  поставляется в wheels); layout/таблицы обучены на англ. корпусах;
  GraniteDocling на кириллице под вопросом + повторы; нет QC-гейтов и нашей
  обвязки — ~70% ценности нашего пайплайна пришлось бы дописывать.

### 10.2 DeepDoc — сильное / слабое
- **Сильное:** позиционное мышление (блоки с координатами → точный citation),
  TSR-таблицы, офисные форматы, Apache-2.0, интеграция в полный RAG-стек —
  если нужна не преп-подготовка, а готовая RAG-платформа.
- **Слабое для нас:** не самостоятельный продукт — использовать = перейти на
  RAGFlow целиком (замена Cherry Studio, другой проект); кириллических
  OCR-моделей нет; onnxruntime → CPU; на выходе чанки, а не чистый MD; нет QC.

### 10.3 Наш пайплайн — сильное / слабое
- **Сильное:** единственный из трёх заточен под наш кейс — качественная и
  производительная конвертация рус. PDF в MD (два потребителя: Obsidian и
  RAG); QC-гейт («не пустить мусор в БЗ», языкозависимый); Vulkan-инференс
  на AMD без CUDA; VLM-captioning локальной 35B (prose + Mermaid-структуры);
  метки страниц ⌗p.N (переживают чанкинг, грепаются); фрагменты/склейка/
  resume/ETA; CHM-источник; приёмка на реальных документах с битыми
  шрифтами (README §1).
- **Слабое:** форматы PDF+CHM (DOCX/EPUB и пр. — через Docling, кандидат);
  связка зафиксирована на llama.cpp 0.6.0-dev (грабля upstream с grammar);
  веса surya OpenRAIL-M; обвязка самописная — поддержка на нас.

## 11. Вердикт

1. **Как замена основного пути — не подходит ни Docling, ни DeepDoc:**
   оба упираются в наше железо (CPU-only) и в русский язык (модели/гейты);
   ни в одном нет QC-обвязки — смысла нашего проекта.
2. **Docling** — практическая ниша: конвертер **не-PDF форматов**
   (DOCX/PPTX/XLSX/HTML/EPUB → MD) в наш inbox/outbox с нашим QC-гейтом
   (кандидат-задача; записан в README).
3. **DeepDoc** — имеет смысл только при миграции с Cherry Studio на RAGFlow
   как платформу; это смена концепции БЗ, а не апгрейд преп-пайплайна.

**Уточнение кейса (2026-10-16, актуально):** MD потребляют двое — Obsidian
(человек) и RAG (Cherry Studio). Ось «читабельность MD человеком» не меняет
вердикт: marker выдаёт чистый MD с относительными ссылками на картинки,
VLM-описания работают как alt-текст для человека и как текст для RAG
(структурированный режим добавляет Mermaid-граф и список связей);
Docling/DeepDoc дают либо «документ для машины», либо MD без наших гарантий
качества.
