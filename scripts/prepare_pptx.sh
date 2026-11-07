#!/usr/bin/env bash
# Презентация PPTX (inbox/*.pptx) -> готовый MD (USAGE §1.5, §2.8):
#   [1/5] pptx_to_md.py: текст/таблицы/заметки докладчика/данные диаграмм/
#         SmartArt/картинки (SVG->PNG), гео-порядок фигур, логические блоки,
#         метки <!-- x,y,w -->, цветной wireframe слайдов, якоря ⌗s.N
#   [2/5] язык (ru|en): --lang > CAPTION_LANG > текст MD (большинство
#         букв) > деф. ru; логируется
#   [3/5] captioning: фигуры + слайдовые wireframe (по alt «Раскладка слайда
#         N» — слайдовый промпт «Суть слайда»+Mermaid); порог мелких 4096 —
#         графика слайдов мельче PDF-врезок (CAPTION_MIN_BYTES из .env имеет
#         приоритет)
#   [4/5] cleanup + QC (мягкий: презентация без таблиц — норма)
#   [5/5] .qc_pass (resume); итог: outbox/<Имя>/<Имя>.md + figures/.
# В БЗ грузится только MD (описания уже в тексте). Настройки .env:
#   PPTX_MIN_IMAGE_BYTES (деф. 4096), PPTX_LAYOUTS=0 — без wireframe.
# Usage: scripts/prepare_pptx.sh [--force] [--lang ru|en] [<файл.pptx из inbox>]
#        (без файла — все inbox/*.pptx)
set -euo pipefail

FORCE=0 LANG_FLAG="" FILE=""
while [ $# -gt 0 ]; do
    case "$1" in
        --force) FORCE=1; shift ;;
        --lang) LANG_FLAG="${2:?--lang требует значение}"; shift 2 ;;
        --lang=*) LANG_FLAG="${1#*=}"; shift ;;
        -*) echo "неизвестный аргумент: $1"; exit 2 ;;
        *) FILE="$1"; shift ;;
    esac
done
case "$LANG_FLAG" in ""|ru|en) ;; *) echo "--lang: ожидается ru|en"; exit 2 ;; esac

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
if [ -f "$ROOT/.env" ]; then set -a; . "$ROOT/.env"; set +a; fi
LLM_URL="${LLM_URL:-http://127.0.0.1:${LLM_PORT:-8080}}"
CAPTION_FIGURES="${CAPTION_FIGURES:-1}"
export CAPTION_MIN_BYTES="${CAPTION_MIN_BYTES:-4096}"
PPTX_ARGS=(--min-image-bytes "${PPTX_MIN_IMAGE_BYTES:-4096}")
[ "${PPTX_LAYOUTS:-1}" = 0 ] && PPTX_ARGS+=(--no-layouts)

mapfile -t FILES < <(ls -1 inbox/*.pptx 2>/dev/null | grep -v '/~\$' | sort)
if [ -n "$FILE" ]; then
    [ -f "$FILE" ] || FILE="inbox/$(basename "$FILE")"
    [ -f "$FILE" ] || { echo "файл не найден: $FILE (ожидался в inbox/)"; exit 2; }
    FILES=("$FILE")
fi
[ ${#FILES[@]} -gt 0 ] || { echo "inbox/*.pptx пуст — нечего обрабатывать"; exit 0; }

stage() { printf '[%s/5] %s %s\n' "$1" "$(date +%H:%M:%S)" "$2"; }
mkdir -p outbox logs

PPTX_LOG="logs/pptx_$(date +%Y%m%d_%H%M).log"
ln -sf "$(basename "$PPTX_LOG")" logs/pptx_latest.log
exec > >(tee -a "$PPTX_LOG") 2>&1

detect_lang() {  # $1=MD: --lang > CAPTION_LANG > текст (большинство букв)
    case "$LANG_FLAG" in
        ru|en) DOC_LANG="$LANG_FLAG"; DOC_LANG_SRC="--lang (принудительно)"; return 0 ;;
    esac
    case "${CAPTION_LANG:-}" in
        ru|en) DOC_LANG="$CAPTION_LANG"; DOC_LANG_SRC="CAPTION_LANG (принудительно)"; return 0 ;;
    esac
    local out
    out="$(python3 - "$1" <<'PY'
import re, sys
txt = open(sys.argv[1], encoding="utf-8", errors="ignore").read(65536)
letters = re.findall(r"[а-яёА-ЯЁa-zA-Z]", txt)
if letters:
    n = sum(1 for c in letters if re.match(r"[а-яёА-ЯЁ]", c))
    print(("ru" if n > len(letters) / 2 else "en")
          + f"\tтекст документа (кириллица {100 * n / len(letters):.0f}% из букв)")
else:
    print("ru\tдефолт (букв нет)")
PY
    )" || out=$'ru\tошибка детекта (дефолт)'
    DOC_LANG="${out%%$'\t'*}"; DOC_LANG_SRC="${out#*$'\t'}"
}

process() {
    local src="$1" stem dir md t0=$SECONDS
    stem="$(basename "${src%.pptx}")"; dir="outbox/$stem"; md="$dir/$stem.md"
    echo ""
    echo "== $stem =="
    if [ -f "$dir/.qc_pass" ] && [ "$FORCE" = 0 ]; then
        echo "[--] уже готово (.qc_pass); --force — переобработать"; return 0
    fi
    rm -rf "$dir"; mkdir -p "$dir"

    stage 1 "конвертация (pptx_to_md: гео-порядок, блоки, wireframe)"
    uv run --project "$ROOT" python "$ROOT/scripts/pptx_to_md.py" "${PPTX_ARGS[@]}" "$src"

    stage 2 "язык документа"
    detect_lang "$md"
    local word="русском"; [ "$DOC_LANG" = en ] && word="английском"
    echo "[--] язык: $DOC_LANG — $DOC_LANG_SRC; VLM-описания и QC — на $word"

    local tc=0
    if [ "$CAPTION_FIGURES" = 1 ]; then
        if curl -sf -m 2 "$LLM_URL/v1/models" >/dev/null 2>&1 || [ -n "${CAPTION_API:-}" ]; then
            local tc0=$SECONDS
            stage 3 "captioning (фигуры + «Суть слайда» по wireframe)"
            uv run --project "$ROOT" python "$ROOT/scripts/caption_figures.py" \
                --lang "$DOC_LANG" "$md" \
                || echo "[--] captioning: ошибка, продолжаем без описаний"
            tc=$((SECONDS-tc0))
        else
            echo "[--] LLM-сервис не поднят — captioning пропущен (./services.sh start)"
        fi
    fi

    stage 4 "cleanup + QC (мягкий — презентация)"
    uv run --project "$ROOT" python "$ROOT/scripts/clean_md.py" "$md"
    if uv run --project "$ROOT" python "$ROOT/scripts/qc_check.py" --lang "$DOC_LANG" "$md"; then
        echo "[--] QC PASS"
    else
        echo "[--] QC FAIL — мягкий режим презентации (без таблиц — норма), .qc_pass ставится"
    fi

    touch "$dir/.qc_pass"
    local figs
    figs="$(grep -cE '\*\*VLM-(описание|description):\*\*' "$md" 2>/dev/null || true)"
    printf '{"stem":"%s","slides":0,"caption_s":%s,"figs":%s,"total_s":%s}\n' \
        "$stem" "$tc" "$figs" "$((SECONDS-t0))" >> logs/timings.jsonl
    echo "[PASS] $stem -> $md (captioning ${tc}с, VLM-блоков $figs, всего $((SECONDS-t0))с)"
}

fails=0
for f in "${FILES[@]}"; do
    process "$f" || fails=$((fails+1))
done
[ "$fails" = 0 ] || { echo "[FAIL] ошибок: $fails"; exit 1; }
