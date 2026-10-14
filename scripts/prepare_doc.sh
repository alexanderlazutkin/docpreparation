#!/usr/bin/env bash
# PDF -> Markdown: конвертация (marker, balanced) -> QC-гейт -> авто-ретрай --force_ocr
# Использование: scripts/prepare_doc.sh inbox/file.pdf [доп. флаги marker_single]
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
if [ -f "$ROOT/.env" ]; then set -a; . "$ROOT/.env"; set +a; fi

IN="${1:?usage: prepare_doc.sh <file.pdf> [marker flags...]}"
shift
EXTRA=("$@")

STEM="$(basename "${IN%.*}")"
OUT="${OUT:-$ROOT/outbox}"
MD="$OUT/$STEM/$STEM.md"

echo ">> marker: $IN -> $OUT"
uv run --project "$ROOT" marker_single "$IN" --output_dir "$OUT" --mode balanced ${EXTRA[@]+"${EXTRA[@]}"}

qc() { uv run --project "$ROOT" python "$ROOT/scripts/qc_check.py" "$MD"; }

if ! qc; then
    echo ">> QC FAIL, ретрай с --force_ocr"
    rm -rf "$OUT/$STEM"
    uv run --project "$ROOT" marker_single "$IN" --output_dir "$OUT" --mode balanced --force_ocr ${EXTRA[@]+"${EXTRA[@]}"}
    qc || { echo ">> QC FAIL даже после force_ocr: $MD"; exit 1; }
fi
echo ">> QC PASS: $MD"
