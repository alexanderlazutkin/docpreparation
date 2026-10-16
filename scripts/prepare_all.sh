#!/usr/bin/env bash
# Пакетная обработка inbox/*.pdf: конвертация (marker) + captioning + QC.
# Resume: фрагменты/файлы с outbox/<имя>/.qc_pass пропускаются; --force — всё заново.
#
# Диапазон и фрагменты (для тестов и больших файлов):
#   --pages "S[-E]"   начальная страница S (деф. 1), конечная E (деф. 0 = до конца)
#   --fragments K     диапазон делится на K фрагментов, обрабатываемых поочерёдно
#                     (деф. 1); каждый — свой каталог outbox/<имя>__pS-E/
#   --retry-ocr       при QC FAIL выполнять ретрай --force_ocr (по умолчанию ВЫКЛ)
#
# Прогресс: стадии [k/5] по каждому файлу/фрагменту + ETA по logs/timings.jsonl
# (медиана с/страницу; при пустой истории — сид 8.4 с/стр, 0.5 с/стр caption).
# Логи: logs/<имя>.log на файл, logs/batch_<датавремя>.log сводный,
#   logs/batch_latest.log / logs/batch_latest.result — для мониторинга агентом.
# Usage: scripts/prepare_all.sh [--force] [--pages "S[-E]"] [--fragments K]
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
FORCE="" PAGES="" FRAGS=1 RETRY=""
while [ $# -gt 0 ]; do
    case "$1" in
        --force) FORCE="--force"; shift ;;
        --pages) PAGES="${2:?--pages требует значение}"; shift 2 ;;
        --fragments) FRAGS="${2:?--fragments требует значение}"; shift 2 ;;
        --retry-ocr) RETRY="--retry-ocr"; shift ;;
        *) echo "неизвестный аргумент: $1"; exit 2 ;;
    esac
done
mkdir -p logs outbox

BATCH_LOG="logs/batch_$(date +%Y%m%d_%H%M).log"
ln -sf "$(basename "$BATCH_LOG")" logs/batch_latest.log   # стабильный путь для мониторинга
rm -f logs/batch_latest.result
exec > >(tee -a "$BATCH_LOG") 2>&1

# медианные скорости из истории (с/страницу: marker и caption раздельно)
read -r R_MARK R_CAP < <(python3 - "$ROOT/logs/timings.jsonl" <<'PY'
import json, statistics, sys
rows = []
try:
    for line in open(sys.argv[1], encoding="utf-8"):
        try:
            rows.append(json.loads(line))
        except Exception:  # битые строки не роняют оценку
            pass
except FileNotFoundError:
    pass
try:
    rm = statistics.median(r["marker_s"] / r["pages"] for r in rows
                           if r.get("pages") and r.get("marker_s"))
    rc = statistics.median(r["caption_s"] / r["pages"] for r in rows
                           if r.get("pages") and r.get("caption_s"))
except statistics.StatisticsError:
    rm = rc = None
print(f"{rm if rm is not None else 8.4:.2f} {rc if rc is not None else 0.5:.2f}")
PY
)
R_MARK="${R_MARK:-8.4}"; R_CAP="${R_CAP:-0.5}"
ETA_SRC="история" ; [ -f logs/timings.jsonl ] || ETA_SRC="сид-оценка"

fmt_mmss() { printf '%d мин %02d с' $(( $1 / 60 )) $(( $1 % 60 )); }

mapfile -t FILES < <(ls -1 inbox/*.pdf 2>/dev/null | sort)
N=${#FILES[@]}
[ "$N" -gt 0 ] || { echo "inbox/ пуст — нечего обрабатывать"; exit 0; }

echo "== Batch: $N файл(ов), $(date '+%Y-%m-%d %H:%M:%S'), лог: $BATCH_LOG =="
echo "== ETA-скорости: marker ${R_MARK} с/стр, caption ${R_CAP} с/стр (${ETA_SRC}) =="
RS="выкл"; [ -n "$RETRY" ] && RS="вкл"
echo "== Диапазон: ${PAGES:-весь документ}; фрагментов: ${FRAGS}; retry-ocr: $RS =="

ROWS=()
k=0; fails=0
for f in "${FILES[@]}"; do
    k=$((k+1)); stem="$(basename "${f%.*}")"
    total="$(pdfinfo "$f" 2>/dev/null | awk '/^Pages:/{print $2}')"; total="${total:-0}"

    # разбор диапазона (1-индекс, E=0 -> до конца)
    S=1; E=0
    if [ -n "$PAGES" ]; then
        S="${PAGES%%-*}"; E="${PAGES#*-}"; [ "$E" = "$PAGES" ] && E=0
        S="${S:-1}"; E="${E:-0}"
    fi
    [ "$E" = 0 ] && E=$total
    if [ "$E" -lt "$S" ]; then echo "[--] $stem: пустой диапазон $S-$E, пропуск"; continue; fi
    n=$((E-S+1))

    # деление диапазона на FRAGS фрагментов
    FR_RANGES=()
    if [ "$FRAGS" -le 1 ]; then
        FR_RANGES+=("$S-$E")
    else
        per=$(( (n + FRAGS - 1) / FRAGS ))
        s=$S
        while [ "$s" -le "$E" ]; do
            e=$((s+per-1)); [ "$e" -gt "$E" ] && e=$E
            FR_RANGES+=("$s-$e"); s=$((e+1))
        done
    fi
    K=${#FR_RANGES[@]}

    j=0
    for rng in "${FR_RANGES[@]}"; do
        j=$((j+1))
        fs="${rng%%-*}"; fe="${rng#*-}"; fn=$((fe-fs+1))
        eta=$(python3 -c "print(int($fn*($R_MARK+$R_CAP)))")
        echo ""
        echo "=================================================================="
        echo "[$k/$N] $stem [фрагмент $j/$K: стр. $fs-$fe ($fn стр.)]"
        echo "старт $(date +%H:%M:%S), ETA ~$(fmt_mmss $eta)"
        echo "=================================================================="
        t0=$SECONDS
        PA=(--pages "$rng")                       # фрагмент всегда с диапазоном
        [ "$K" -le 1 ] && [ -z "$PAGES" ] && PA=()  # полный документ без --pages
        if scripts/prepare_doc.sh $FORCE $RETRY ${PA[@]+"${PA[@]}"} "$f" 2>&1 | tee "logs/$stem.log"; then
            ROWS+=("PASS|$stem|$j/$K|$((SECONDS-t0))|$fn")
        else
            ROWS+=("FAIL|$stem|$j/$K|$((SECONDS-t0))|$fn"); fails=$((fails+1))
        fi
    done
done

echo ""
echo "== Итог batch ($N файл(ов)) =="
printf '%-5s %-46s %-7s %6s %8s\n' "QC" "Файл" "фрагм." "сек" "с/стр"
for r in "${ROWS[@]}"; do
    IFS='|' read -r q s fr t n <<< "$r"
    spp="$(awk "BEGIN{n=$n+0; if (n>0) printf \"%.1f\", $t/n; else print \"—\"}")"
    printf '%-5s %-46s %-7s %6s %8s\n' "$q" "${s:0:46}" "$fr" "$t" "$spp"
done
echo "Готовых: $(( ${#ROWS[@]} - fails ))/${#ROWS[@]}; .md — в outbox/, логи — в logs/"
printf 'ts=%s\nfiles=%s\nfails=%s\n' "$(date +%s)" "$N" "$fails" > logs/batch_latest.result
[ "$fails" = 0 ]
