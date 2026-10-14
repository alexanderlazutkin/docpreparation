#!/usr/bin/env bash
# PDF -> Markdown: [1/5] marker (balanced) -> [2/5] captioning фигур ->
# [3/5] QC -> [4/5] ретрай --force_ocr (только при QC FAIL) -> [5/5] готово.
# Статус: outbox/<имя>/.status; успех: outbox/<имя>/.qc_pass (повторный запуск
# пропускает готовый файл; --force — переобработать).
# Usage: scripts/prepare_doc.sh [--force] <file.pdf> [доп. флаги marker_single]
set -euo pipefail

FORCE=0
[ "${1:-}" = "--force" ] && { FORCE=1; shift; }

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
if [ -f "$ROOT/.env" ]; then set -a; . "$ROOT/.env"; set +a; fi

# Аттач к постоянному surya-серверу (services.sh start), если поднят
SURYA_PORT="${SURYA_PORT:-8510}"
if [ -z "${SURYA_INFERENCE_URL:-}" ] && curl -sf -m 2 "http://127.0.0.1:$SURYA_PORT/v1/models" >/dev/null 2>&1; then
    export SURYA_INFERENCE_URL="http://127.0.0.1:$SURYA_PORT/v1"
    echo "[--] аттач к постоянному surya-серверу :$SURYA_PORT"
fi

IN="${1:?usage: prepare_doc.sh [--force] <file.pdf> [marker flags...]}"
shift
EXTRA=("$@")

STEM="$(basename "${IN%.*}")"
OUT="${OUT:-$ROOT/outbox}"
DIR="$OUT/$STEM"
MD="$DIR/$STEM.md"
T0=$SECONDS

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
caption() {
    [ "$CAPTION_FIGURES" = 1 ] || return 0
    if ! curl -sf -m 2 "http://127.0.0.1:${LLM_PORT:-8080}/v1/models" >/dev/null 2>&1; then
        echo "[--] LLM-сервис не поднят — captioning пропущен (./services.sh start)"
        return 0
    fi
    stage 2/5 "captioning фигур (VLM-описания для поиска)"
    uv run --project "$ROOT" python "$ROOT/scripts/caption_figures.py" "$MD" \
        || echo "[--] captioning: ошибка, продолжаем без описаний"
}

echo "== $STEM =="
stage 1/5 "marker (balanced)"
uv run --project "$ROOT" marker_single "$IN" --output_dir "$OUT" --mode balanced ${EXTRA[@]+"${EXTRA[@]}"}
caption

stage 3/5 "QC"
if ! qc; then
    stage 4/5 "QC FAIL -> ретрай --force_ocr"
    rm -rf "$DIR"
    uv run --project "$ROOT" marker_single "$IN" --output_dir "$OUT" --mode balanced --force_ocr ${EXTRA[@]+"${EXTRA[@]}"}
    caption
    stage 3/5 "QC (после force_ocr)"
    qc || { echo "[FAIL] $STEM: QC FAIL даже после force_ocr -> $MD"; exit 1; }
fi
touch "$DIR/.qc_pass"
stage 5/5 "готово"
echo "[PASS] $STEM -> $MD ($((SECONDS-T0)) c)"
