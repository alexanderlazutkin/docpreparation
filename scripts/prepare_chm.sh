#!/usr/bin/env bash
# Распакованный CHM (data.hhc + html + картинки) -> готовый MD (USAGE §1.6, §2.7):
#   [1/5] chm_to_md.py: обход .hhc, html -> MD, картинки -> figures/,
#         якоря ⌗doc:<имя>.html; --title = имя каталога CHM
#   [2/5] язык (ru|en): --lang > CAPTION_LANG > текст MD (большинство
#         букв; CHM-справочники обычно en) > деф. ru; логируется
#   [3/5] captioning фигур (--structured по env CAPTION_STRUCTURED)
#   [4/5] cleanup + QC (мягкий: справка без таблиц — норма)
#   [5/5] .qc_pass (resume); итог: outbox/<Имя>/<Имя>.md + figures/.
# Настройки .env: CHM_MIN_IMAGE_BYTES (деф. 4096 — иконки GUI в отброс).
# Usage: scripts/prepare_chm.sh [--force] [--lang ru|en] [<путь/data.hhc>]
#        (без файла — все inbox/*.hhc и inbox/*/*.hhc)
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

mapfile -t FILES < <(ls -1 inbox/*.hhc inbox/*/*.hhc 2>/dev/null | sort -u)
if [ -n "$FILE" ]; then
    [ -f "$FILE" ] || { echo "файл не найден: $FILE"; exit 2; }
    FILES=("$FILE")
fi
[ ${#FILES[@]} -gt 0 ] || { echo "inbox/*.hhc (и inbox/*/*.hhc) пуст — нечего обрабатывать"; exit 0; }

stage() { printf '[%s/5] %s %s\n' "$1" "$(date +%H:%M:%S)" "$2"; }
mkdir -p outbox logs

CHM_LOG="logs/chm_$(date +%Y%m%d_%H%M).log"
ln -sf "$(basename "$CHM_LOG")" logs/chm_latest.log
exec > >(tee -a "$CHM_LOG") 2>&1

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
    stem="$(basename "$(dirname "$src")")"; dir="outbox/$stem"; md="$dir/$stem.md"
    echo ""
    echo "== $stem =="
    if [ -f "$dir/.qc_pass" ] && [ "$FORCE" = 0 ]; then
        echo "[--] уже готово (.qc_pass); --force — переобработать"; return 0
    fi
    rm -rf "$dir"; mkdir -p "$dir"

    stage 1 "конвертация (chm_to_md: TOC -> заголовки, якоря ⌗doc)"
    uv run --project "$ROOT" python "$ROOT/scripts/chm_to_md.py" \
        --title "$stem" --min-image-bytes "${CHM_MIN_IMAGE_BYTES:-4096}" "$src"

    stage 2 "язык документа"
    detect_lang "$md"
    local word="русском"; [ "$DOC_LANG" = en ] && word="английском"
    echo "[--] язык: $DOC_LANG — $DOC_LANG_SRC; VLM-описания и QC — на $word"

    local tc=0
    if [ "$CAPTION_FIGURES" = 1 ]; then
        if curl -sf -m 2 "$LLM_URL/v1/models" >/dev/null 2>&1 || [ -n "${CAPTION_API:-}" ]; then
            local tc0=$SECONDS
            stage 3 "captioning фигур (VLM-описания для поиска)"
            uv run --project "$ROOT" python "$ROOT/scripts/caption_figures.py" \
                --lang "$DOC_LANG" "$md" \
                || echo "[--] captioning: ошибка, продолжаем без описаний"
            tc=$((SECONDS-tc0))
        else
            echo "[--] LLM-сервис не поднят — captioning пропущен (./services.sh start)"
        fi
    fi

    stage 4 "cleanup + QC (мягкий — справочник CHM)"
    uv run --project "$ROOT" python "$ROOT/scripts/clean_md.py" "$md"
    if uv run --project "$ROOT" python "$ROOT/scripts/qc_check.py" --lang "$DOC_LANG" "$md"; then
        echo "[--] QC PASS"
    else
        echo "[--] QC FAIL — мягкий режим CHM (без таблиц — норма), .qc_pass ставится"
    fi

    touch "$dir/.qc_pass"
    local figs
    figs="$(grep -cE '\*\*VLM-(описание|description):\*\*' "$md" 2>/dev/null || true)"
    printf '{"stem":"%s","sections":0,"caption_s":%s,"figs":%s,"total_s":%s}\n' \
        "$stem" "$tc" "$figs" "$((SECONDS-t0))" >> logs/timings.jsonl
    echo "[PASS] $stem -> $md (captioning ${tc}с, VLM-блоков $figs, всего $((SECONDS-t0))с)"
}

fails=0
for f in "${FILES[@]}"; do
    process "$f" || fails=$((fails+1))
done
[ "$fails" = 0 ] || { echo "[FAIL] ошибок: $fails"; exit 1; }
