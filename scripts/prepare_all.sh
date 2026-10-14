#!/usr/bin/env bash
# Пакетная обработка inbox/*.pdf: конвертация (marker) + captioning + QC,
# с resume (готовые файлы с outbox/<имя>/.qc_pass пропускаются).
# Прогресс и стадии печатаются здесь же; на каждый файл — logs/<имя>.log,
# сводный лог — logs/batch_<датавремя>.log.
# Usage: scripts/prepare_all.sh [--force]
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
FORCE=""
[ "${1:-}" = "--force" ] && FORCE="--force"
mkdir -p logs outbox

BATCH_LOG="logs/batch_$(date +%Y%m%d_%H%M).log"
ln -sf "$(basename "$BATCH_LOG")" logs/batch_latest.log   # стабильный путь для мониторинга
rm -f logs/batch_latest.result
exec > >(tee -a "$BATCH_LOG") 2>&1

mapfile -t FILES < <(ls -1 inbox/*.pdf 2>/dev/null | sort)
N=${#FILES[@]}
[ "$N" -gt 0 ] || { echo "inbox/ пуст — нечего обрабатывать"; exit 0; }

echo "== Batch: $N файл(ов), $(date '+%Y-%m-%d %H:%M:%S'), лог: $BATCH_LOG =="
ROWS=()
k=0; fails=0
for f in "${FILES[@]}"; do
    k=$((k+1)); stem="$(basename "${f%.*}")"
    echo ""
    echo "=================================================================="
    echo "[$k/$N] $stem — старт $(date +%H:%M:%S)"
    echo "=================================================================="
    t0=$SECONDS
    if scripts/prepare_doc.sh $FORCE "$f" 2>&1 | tee "logs/$stem.log"; then
        ROWS+=("PASS|$stem|$((SECONDS-t0))")
    else
        ROWS+=("FAIL|$stem|$((SECONDS-t0))"); fails=$((fails+1))
    fi
done

echo ""
echo "== Итог batch ($N файл(ов)) =="
printf '%-5s %-58s %7s\n' "QC" "Файл" "сек"
for r in "${ROWS[@]}"; do IFS='|' read -r q s t <<< "$r"; printf '%-5s %-58s %7s\n' "$q" "$s" "$t"; done
echo "Готовых: $((N-fails))/$N; готовые .md — в outbox/, логи — в logs/"
printf 'ts=%s\nfiles=%s\nfails=%s\n' "$(date +%s)" "$N" "$fails" > logs/batch_latest.result
[ "$fails" = 0 ]
