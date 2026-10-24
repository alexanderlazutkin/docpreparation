#!/usr/bin/env bash
# Перевод ГОТОВЫХ MD (outbox/) на структурированные VLM-описания
# БЕЗ перегона marker: снимает старые blockquote-описания и описывает фигуры
# заново в режиме CAPTION_STRUCTURED=1 (схемы -> prose + Mermaid + «Связи:»,
# таблицы -> реконструкция с пометкой «сверить с PDF»).
#
# Зачем: полный прогон (launch_batch --force) переконвертирует весь PDF
# (~80% времени тратит marker); для апгрейда описаний достаточно captioning —
# это в разы быстрее. ЦЕНА: метки страниц ⌗p при этом НЕ добавляются
# (их даёт только marker; см. USAGE §2.3-П, §2.4-С).
#
# Безопасность: перед перезаписью каждой книги — бэкап <имя>.md.bak_caption
# (делает caption_figures.py --redescribe). Прогон прерываем/перезапускаем:
# уже переописанные книги просто пройдут заново (идемпотентно).
#
# Требует поднятых сервисов: ./services.sh start (Qwen :8080
# для VLM-описаний; surya/прокси для этого скрипта не нужны).
#
# Usage: scripts/redescribe_all.sh [--pause N] [--lang en|ru] [подстрока-фильтр]
#   --pause N    пауза (сек) между книгами для охлаждения ПК (деф. 90,
#                не после последней; 0 = без пауз)
#   --lang XX    язык описаний: en|ru (деф. en — все готовые книги англоязычные)
#   подстрока    обработать только книги, чьё имя её содержит
#                (напр. "Vol. 1" или "Halpin"); без неё — все outbox/*/
#
# Примеры:
#   scripts/redescribe_all.sh                          # все книги, пауза 90с
#   scripts/redescribe_all.sh --pause 120              # холоднее паузы
#   scripts/redescribe_all.sh --pause 0 "Halpin"       # одна книга без пауз
#   scripts/redescribe_all.sh "Vol."                   # три тома DMRB
#   CAPTION_PARALLEL=1 scripts/redescribe_all.sh       # щадящий режим GPU (деф. 2)
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

PAUSE=90 LANG_ARG=en FILTER=""
while [ $# -gt 0 ]; do
    case "$1" in
        --pause) PAUSE="${2:?--pause требует значение}"; shift 2 ;;
        --pause=*) PAUSE="${1#*=}"; shift ;;
        --lang) LANG_ARG="${2:?--lang требует значение}"; shift 2 ;;
        --lang=*) LANG_ARG="${1#*=}"; shift ;;
        -h|--help) sed -n '2,30p' "$0"; exit 0 ;;
        *) FILTER="$1"; shift ;;
    esac
done
case "$LANG_ARG" in en|ru) ;; *) echo "--lang: ожидается en|ru"; exit 2 ;; esac
[ "$PAUSE" -ge 0 ] 2>/dev/null || { echo "--pause: ожидается целое >= 0 (сек)"; exit 2; }

# LLM-сервис должен отвечать (VLM-описания): иначе честный отказ
LLM_URL="http://127.0.0.1:${LLM_PORT:-8080}"
if ! curl -sf -m 2 "$LLM_URL/v1/models" >/dev/null 2>&1; then
    echo "[FAIL] LLM-сервис не поднят ($LLM_URL) — сначала: ./services.sh start" >&2
    exit 2
fi

# книги: строго outbox/<имя>/<имя>.md (vlm_pilot/report.md и прочее — не книги);
# фрагменты/склейки (__pS-E) не трогаем
BOOKS=()
while IFS= read -r f; do
    [ "$(basename "$(dirname "$f")")" = "$(basename "${f%.md}")" ] || continue
    BOOKS+=("$f")
done < <(find outbox -mindepth 2 -maxdepth 2 -name '*.md' ! -name '*__p*' | sort)
if [ -n "$FILTER" ]; then
    FILTERED=()
    for f in "${BOOKS[@]}"; do
        case "$f" in *"$FILTER"*) FILTERED+=("$f") ;; esac
    done
    BOOKS=("${FILTERED[@]}")
fi
[ "${#BOOKS[@]}" -gt 0 ] || { echo "книг не найдено (фильтр: «${FILTER:-нет}»)"; exit 0; }

echo "== redescribe: ${#BOOKS[@]} кн., язык $LANG_ARG, пауза ${PAUSE}с, parallel ${CAPTION_PARALLEL:-2} =="
N=${#BOOKS[@]} k=0
for f in "${BOOKS[@]}"; do
    k=$((k+1))
    stem="$(basename "$(dirname "$f")")"
    echo ""
    echo "=================================================================="
    echo "[$k/$N] $stem"
    echo "старт $(date +%H:%M:%S)"
    echo "=================================================================="
    t0=$SECONDS
    CAPTION_STRUCTURED=1 uv run python scripts/caption_figures.py \
        --redescribe --lang "$LANG_ARG" "$f" 2>&1 | tee "logs/${stem}.redescribe.log"
    echo "[$k/$N] $stem: готово за $((SECONDS-t0))с -> $f"
    if [ "$PAUSE" -gt 0 ] && [ "$k" -lt "$N" ]; then
        echo "[--] пауза ${PAUSE}с (охлаждение)…"
        sleep "$PAUSE"
    fi
done

echo ""
echo "== redescribe: все книги обработаны ($N шт.) =="
echo "Напоминание: бэкапы рядом с MD (*.md.bak_caption); метки ⌗p этот путь не добавляет."
