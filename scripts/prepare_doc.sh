#!/usr/bin/env bash
# PDF -> Markdown: [1/6] marker (balanced) -> [2/6] captioning -> [3/6] cleanup
#   (точечные лидеры TOC) -> [4/6] QC -> [5/6] ретрай --force_ocr (ТОЛЬКО с
#   флагом --retry-ocr; для фрагментов QC всегда мягкий) -> [6/6] готово.
# Диапазон: --pages "S[-E]" (1-индекс; без E или E=0 — до конца документа).
#   Фрагмент пишется в outbox/<имя>__pS-E/ (полный документ — в outbox/<имя>/).
# Статус: outbox/<...>/.status; успех: .qc_pass (resume); --force — переобработка.
# Captioning: локальный LLM-сервис (services.sh) или облако через env
#   CAPTION_API/CAPTION_MODEL/CAPTION_API_KEY (см. .env.example).
# Usage: prepare_doc.sh [--force] [--retry-ocr] [--pages "S[-E]"] <file.pdf> [флаги marker_single]
set -euo pipefail

FORCE=0 PAGES="" RETRY_OCR="${RETRY_OCR:-0}"
while :; do
    case "${1:-}" in
        --force) FORCE=1; shift ;;
        --pages) PAGES="${2:?--pages требует значение}"; shift 2 ;;
        --retry-ocr) RETRY_OCR=1; shift ;;
        *) break ;;
    esac
done

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
if [ -f "$ROOT/.env" ]; then set -a; . "$ROOT/.env"; set +a; fi

# Аттач к постоянному surya-серверу (services.sh start), если поднят
SURYA_PORT="${SURYA_PORT:-8510}"
if [ -z "${SURYA_INFERENCE_URL:-}" ] && curl -sf -m 2 "http://127.0.0.1:$SURYA_PORT/v1/models" >/dev/null 2>&1; then
    export SURYA_INFERENCE_URL="http://127.0.0.1:$SURYA_PORT/v1"
    echo "[--] аттач к постоянному surya-серверу :$SURYA_PORT"
fi

IN="${1:?usage: prepare_doc.sh [--force] [--pages S[-E]] <file.pdf> [marker flags...]}"
shift
EXTRA=("$@")

STEM_BASE="$(basename "${IN%.*}")"
OUT="${OUT:-$ROOT/outbox}"
LLM_URL="http://127.0.0.1:${LLM_PORT:-8080}"
TOTAL_PAGES="$(pdfinfo "$IN" 2>/dev/null | awk '/^Pages:/{print $2}')"
TOTAL_PAGES="${TOTAL_PAGES:-0}"

# --pages -> (S,E) 1-индекс, E=0 значит до конца; имя каталога; флаги marker
PAGE_ARGS=() STEM="$STEM_BASE" QC_SOFT=0 S=1 E=0
if [ -n "$PAGES" ]; then
    S="${PAGES%%-*}"; E="${PAGES#*-}"; [ "$E" = "$PAGES" ] && E=0
    S="${S:-1}"; E="${E:-0}"
    [ "$S" -ge 1 ] 2>/dev/null || { echo "неверный --pages: $PAGES (S>=1)"; exit 2; }
    [ "$E" = 0 ] || [ "$E" -ge "$S" ] 2>/dev/null || { echo "неверный --pages: $PAGES (E>=S)"; exit 2; }
    [ "$E" = 0 ] || [ "$E" -le "$TOTAL_PAGES" ] || { echo "--pages: E=$E > страниц ($TOTAL_PAGES)"; exit 2; }
    STEM="${STEM_BASE}__p${S}-${E}"; QC_SOFT=1
    M0=$((S-1)); M1=$(( E>0 ? E-1 : TOTAL_PAGES-1 ))
    PAGE_ARGS=(--page_range "${M0}-${M1}")
fi

DIR="$OUT/$STEM"; MD="$DIR/$STEM.md"; N_PAGES=$(( E>0 ? E-S+1 : TOTAL_PAGES-S+1 ))
T0=$SECONDS TM=0 TC=0 FIGS=0

stage() {
    echo "[$1] $(date +%H:%M:%S) $2"
    mkdir -p "$DIR"
    printf 'stage=%s\nname=%s\nts=%s\nelapsed=%s\n' "$1" "$2" "$(date +%s)" "$((SECONDS-T0))" > "$DIR/.status"
}

if [ -f "$DIR/.qc_pass" ] && [ "$FORCE" = 0 ]; then
    echo "[--] $STEM уже обработан (.qc_pass); --force для переобработки"
    exit 0
fi

qc() { uv run --project "$ROOT" python "$ROOT/scripts/qc_check.py" "$MD"; }

CAPTION_FIGURES="${CAPTION_FIGURES:-1}"
cleanup() {
    stage 3/6 "cleanup (лидеры оглавления)"
    uv run --project "$ROOT" python "$ROOT/scripts/clean_md.py" "$MD"
}
caption() {
    [ "$CAPTION_FIGURES" = 1 ] || return 0
    local est; est="$(find "$DIR" -maxdepth 1 -type f \( -name '*.jp*g' -o -name '*.png' \) -size +20k 2>/dev/null | wc -l)"
    if [ "$est" -gt 0 ] && curl -sf -m 2 "$LLM_URL/v1/models" >/dev/null 2>&1; then
        echo "[--] фигур к описанию: ~$est (оценка ~$((est*5))с)"
    fi
    if ! curl -sf -m 2 "$LLM_URL/v1/models" >/dev/null 2>&1; then
        if [ -n "${CAPTION_API:-}" ]; then
            echo "[--] captioning через внешний API: ${CAPTION_API}"
        else
            echo "[--] LLM-сервис не поднят — captioning пропущен (./services.sh start)"; return 0
        fi
    fi
    local tc0=$SECONDS
    stage 2/6 "captioning фигур (VLM-описания для поиска)"
    uv run --project "$ROOT" python "$ROOT/scripts/caption_figures.py" "$MD" \
        || echo "[--] captioning: ошибка, продолжаем без описаний"
    TC=$((SECONDS-tc0))
}

echo "== $STEM (${N_PAGES} стр.) =="
stage 1/6 "marker (balanced)"
# marker именует выход по имени файла: гоним во временный каталог и переносим
TMP="$OUT/.tmp.$$"; rm -rf "$TMP"; mkdir -p "$TMP"
local_tm0=$SECONDS
uv run --project "$ROOT" marker_single "$IN" --output_dir "$TMP" --mode balanced \
    "${PAGE_ARGS[@]}" ${EXTRA[@]+"${EXTRA[@]}"}
TM=$((SECONDS-local_tm0))
if [ ! -f "$TMP/$STEM_BASE/$STEM_BASE.md" ]; then
    echo "[FAIL] marker не создал MD ($TMP/$STEM_BASE/)"; rm -rf "$TMP"; exit 1
fi
mkdir -p "$DIR"
mv "$TMP/$STEM_BASE"/* "$DIR"/ ; rm -rf "$TMP"
if [ "$STEM" != "$STEM_BASE" ]; then  # фрагмент: MD и meta под именем фрагмента
    mv "$DIR/$STEM_BASE.md" "$MD"
    [ -f "$DIR/${STEM_BASE}_meta.json" ] && mv "$DIR/${STEM_BASE}_meta.json" "$DIR/${STEM}_meta.json"
fi
caption
cleanup

QCRC=0
stage 4/6 "QC"
if ! qc; then
    QCRC=1
    if [ "$QC_SOFT" = 1 ]; then
        echo "[--] QC FAIL — мягкий режим фрагмента, без ретрая"
    elif [ "$RETRY_OCR" = 1 ]; then
        stage 5/6 "QC FAIL -> ретрай --force_ocr (--retry-ocr)"
        rm -rf "$DIR" "$TMP"
        mkdir -p "$TMP"
        local_tm0=$SECONDS
        uv run --project "$ROOT" marker_single "$IN" --output_dir "$TMP" --mode balanced --force_ocr \
            "${PAGE_ARGS[@]}" ${EXTRA[@]+"${EXTRA[@]}"}
        TM=$((SECONDS-local_tm0))
        mkdir -p "$DIR"
        mv "$TMP/$STEM_BASE"/* "$DIR"/ ; rm -rf "$TMP"
        if [ "$STEM" != "$STEM_BASE" ]; then
            mv "$DIR/$STEM_BASE.md" "$MD"
            [ -f "$DIR/${STEM_BASE}_meta.json" ] && mv "$DIR/${STEM_BASE}_meta.json" "$DIR/${STEM}_meta.json"
        fi
        caption
        cleanup
        stage 4/6 "QC (после force_ocr)"
        qc || { echo "[FAIL] $STEM: QC FAIL даже после force_ocr -> $MD"; exit 1; }
        QCRC=0
    else
        echo "[FAIL] $STEM: QC FAIL; ретрай force_ocr отключён по умолчанию (запускай с --retry-ocr) -> $MD"
        exit 1
    fi
fi

FIGS="$(grep -cF '**VLM-описание:**' "$MD" 2>/dev/null || true)"
touch "$DIR/.qc_pass"
stage 6/6 "готово"
mkdir -p "$ROOT/logs"
printf '{"stem":"%s","pages":%s,"marker_s":%s,"caption_s":%s,"figs":%s,"total_s":%s}\n' \
    "$STEM" "$N_PAGES" "$TM" "$TC" "$FIGS" "$((SECONDS-T0))" >> "$ROOT/logs/timings.jsonl"
echo "[PASS] $STEM -> $MD (marker ${TM}с + caption ${TC}с; всего $((SECONDS-T0))с)"
[ "$QCRC" = 0 ] || echo "[!] QC мягко провален (фрагмент/выборка) — проверь вручную"
