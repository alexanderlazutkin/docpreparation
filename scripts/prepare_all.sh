#!/usr/bin/env bash
# Пакетная обработка inbox/*.pdf: конвертация (marker) + captioning + QC.
# Resume: фрагменты/файлы с outbox/<имя>/.qc_pass пропускаются; --force — всё заново.
#
# Диапазон и фрагменты (для тестов и больших файлов):
#   --pages "S[-E]"   начальная страница S (деф. 1), конечная E (деф. 0 = до конца)
#   --fragments K     диапазон делится на K фрагментов, обрабатываемых поочерёдно
#                     (деф. авто: см. ниже; 1 = без деления); каждый — свой
#                     каталог outbox/<имя>__pS-E/ и свой resume;
#                     при K>1 фрагменты автоматически склеиваются в один результат
#                     (outbox/<имя>/ без --pages, иначе <имя>__pS-E/; проверки:
#                     все части в результате, ссылки на картинки целы, мягкий QC);
#                     после УСПЕШНОЙ склейки каталоги фрагментов удаляются
#                     (деф.). --debug — оставить фрагменты для ручной проверки
#                     склейки (и доп. отладочный функционал в будущем)
#   --fragment-pause N  пауза (сек) между фрагментами И между файлами —
#                     охлаждение ПК при длинных пакетных прогонах
#                     (деф. 0, при авто-фрагментации — 90; не после
#                     последнего фрагмента/файла)
#   АВТО-ФРАГМЕНТАЦИЯ (2026-10-24, стандарт для больших книг): если --fragments
#   не задан и в диапазоне > AUTO_FRAGMENT_PAGES стр. (деф. 120; 0 = откл.) —
#   K = ceil(страниц / AUTO_FRAGMENT_SIZE) (деф. 80/фрагмент), пауза
#   AUTO_FRAGMENT_PAUSE (деф. 90с, если пауза не задана флагом/env). Причина:
#   marker на ПОЛНЫХ прогонах >111 стр. с block-OCR через прокси молча выдавал
#   хвост сырым HTML (2026-10-24, USAGE §3.7-известные-проблемы); фрагменты
#   ~80 стр. проверены (234 стр. → 3 фрагмента). Флаг/env FRAGMENTS/FRAGMENT_PAUSE
#   перебивают авто (.env, читается этим скриптом).
#   --retry-ocr       при QC FAIL выполнять ретрай --force_ocr (по умолчанию ВЫКЛ)
#   --lang ru|en      язык документа: VLM-описания и гейт QC (кириллица/латиница);
#                     без флага — авто-детект в prepare_doc (деф. ru)
#
# Прогресс: стадии [k/5] по каждому файлу/фрагменту + ETA по logs/timings.jsonl
# (медиана с/страницу; при пустой истории — сид 8.4 с/стр, 0.5 с/стр caption).
# Логи: logs/<имя>.log на файл, logs/batch_<датавремя>.log сводный,
#   logs/batch_latest.log / logs/batch_latest.result — для мониторинга агентом.
# Usage: scripts/prepare_all.sh [--force] [--pages "S[-E]"] [--fragments K]
#        [--no-page-anchors] (метки страниц ⌗p.N — деф. ВКЛ, см. prepare_doc.sh)
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
# .env — дефолты FRAGMENTS/FRAGMENT_PAUSE/AUTO_FRAGMENT_* (флаги сильнее)
if [ -f "$ROOT/.env" ]; then set -a; . "$ROOT/.env"; set +a; fi

FORCE="" PAGES="" FRAGS="${FRAGMENTS:-1}" RETRY="" DEBUG=0 PAUSE="${FRAGMENT_PAUSE:-0}" LANG_ARG="" NO_ANCHORS=""
FRAGS_GIVEN=0 PAUSE_GIVEN=0
[ -n "${FRAGMENTS:-}" ] && FRAGS_GIVEN=1
[ -n "${FRAGMENT_PAUSE:-}" ] && PAUSE_GIVEN=1
AUTO_FR_PAGES="${AUTO_FRAGMENT_PAGES:-120}"   # авто-фрагментация: порог стр. (0 = выкл.)
AUTO_FR_SIZE="${AUTO_FRAGMENT_SIZE:-80}"      # целевой размер фрагмента (стр.)
AUTO_FR_PAUSE="${AUTO_FRAGMENT_PAUSE:-90}"    # пауза при авто (если не задана явно)
while [ $# -gt 0 ]; do
    case "${1//_/-}" in
        --force) FORCE="--force"; shift ;;
        --pages) PAGES="${2:?--pages требует значение}"; shift 2 ;;
        --fragments) FRAGS="${2:?--fragments требует значение}"; FRAGS_GIVEN=1; shift 2 ;;
        --retry-ocr) RETRY="--retry-ocr"; shift ;;
        --no-page-anchors) NO_ANCHORS="--no-page-anchors"; shift ;;
        --debug) DEBUG=1; shift ;;
        --fragment-pause) PAUSE="${2:?--fragment-pause требует значение}"; PAUSE_GIVEN=1; shift 2 ;;
        --fragment-pause=*) PAUSE="${1#*=}"; PAUSE_GIVEN=1; shift ;;
        --lang) LANG_ARG="${2:?--lang требует значение}"; shift 2 ;;
        --lang=*) LANG_ARG="${1#*=}"; shift ;;
        *) echo "неизвестный аргумент: $1"; exit 2 ;;
    esac
done
[ "$PAUSE" -ge 0 ] 2>/dev/null || { echo "--fragment-pause: ожидается целое >= 0 (сек)"; exit 2; }
case "$LANG_ARG" in ""|ru|en) ;; *) echo "--lang: ожидается ru|en"; exit 2 ;; esac
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
# конвенция: типы не смешиваем; *.md (вэб-клипы) — отдельный путь
ls inbox/*.md >/dev/null 2>&1 && echo "[--] внимание: в inbox/ есть *.md — этот batch их не трогает (см. prepare_web.sh)"

echo "== Batch: $N файл(ов), $(date '+%Y-%m-%d %H:%M:%S'), лог: $BATCH_LOG =="
echo "== ETA-скорости: marker ${R_MARK} с/стр, caption ${R_CAP} с/стр (${ETA_SRC}) =="
RS="выкл"; [ -n "$RETRY" ] && RS="вкл"
DB="выкл"; [ "$DEBUG" = 1 ] && DB="вкл (фрагменты сохраняются)"
echo "== Диапазон: ${PAGES:-весь документ}; фрагментов: ${FRAGS}; fragment-pause: ${PAUSE}с (между фрагментами и файлами); язык: ${LANG_ARG:-авто}; retry-ocr: $RS; debug: $DB =="
echo "== Авто-фрагментация: >${AUTO_FR_PAGES} стр. → по ${AUTO_FR_SIZE} стр., пауза ${AUTO_FR_PAUSE}с (если --fragments не задан; выкл: AUTO_FRAGMENT_PAGES=0) =="

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

    # авто-фрагментация больших документов (стандарт 2026-10-24): без явного
    # --fragments/FRAGMENTS и > AUTO_FR_PAGES стр. → K = ceil(n/AUTO_FR_SIZE),
    # пауза AUTO_FR_PAUSE (если пауза не задана явно). Причина: raw-HTML баг
    # marker на полных прогонах >~111 стр. (docs/HANDOFF, 2026-10-24)
    EFF_FRAGS="$FRAGS"; EFF_PAUSE="$PAUSE"
    if [ "$FRAGS_GIVEN" != 1 ] && [ "$AUTO_FR_PAGES" -gt 0 ] 2>/dev/null && [ "$n" -gt "$AUTO_FR_PAGES" ]; then
        EFF_FRAGS=$(( (n + AUTO_FR_SIZE - 1) / AUTO_FR_SIZE ))
        [ "$PAUSE_GIVEN" != 1 ] && EFF_PAUSE="$AUTO_FR_PAUSE"
        echo "[--] $stem: авто-фрагментация — $n стр. > $AUTO_FR_PAGES → $EFF_FRAGS фрагм. (по ~$AUTO_FR_SIZE стр.), пауза ${EFF_PAUSE}с"
    fi

    # деление диапазона на EFF_FRAGS фрагментов
    FR_RANGES=()
    if [ "$EFF_FRAGS" -le 1 ]; then
        FR_RANGES+=("$S-$E")
    else
        per=$(( (n + EFF_FRAGS - 1) / EFF_FRAGS ))
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
        if scripts/prepare_doc.sh $FORCE $RETRY $NO_ANCHORS ${LANG_ARG:+--lang "$LANG_ARG"} ${PA[@]+"${PA[@]}"} "$f" 2>&1 | tee "logs/$stem.log"; then
            ROWS+=("PASS|$stem|$j/$K|$((SECONDS-t0))|$fn")
        else
            ROWS+=("FAIL|$stem|$j/$K|$((SECONDS-t0))|$fn"); fails=$((fails+1))
        fi
        # пауза между фрагментами (охлаждение ПК), не после последнего
        if [ "$EFF_PAUSE" -gt 0 ] && [ "$j" -lt "$K" ]; then
            echo "[--] пауза ${EFF_PAUSE}с перед следующим фрагментом (fragment-pause)…"
            sleep "$EFF_PAUSE"
        fi
    done

    # Склейка фрагментов в один результат — автоматически при K>1.
    # Имя: полный диапазон -> outbox/<имя>/; выборка (--pages) -> outbox/<имя>__pS-E/
    if [ "$K" -gt 1 ]; then
        MERGE_STEM="$stem"; [ -n "$PAGES" ] && MERGE_STEM="${stem}__p${S}-${E}"
        if [ -f "outbox/$MERGE_STEM/.qc_pass" ] && [ -z "$FORCE" ]; then
            echo "[--] $MERGE_STEM: склеенный результат уже готов (.qc_pass)"
            ROWS+=("PASS|$MERGE_STEM|склейка|-|$total")
        else
            FRAG_DIRS=()
            for rng in "${FR_RANGES[@]}"; do
                fs="${rng%%-*}"; fe="${rng#*-}"
                FRAG_DIRS+=("outbox/${stem}__p${fs}-${fe}")
            done
            echo ""
            echo "=================================================================="
            echo "Склейка: $MERGE_STEM ($K фрагментов, $total стр.)"
            echo "=================================================================="
            t0=$SECONDS
            if uv run --project "$ROOT" python "$ROOT/scripts/merge_fragments.py" \
                --out "outbox/$MERGE_STEM" ${FORCE:+--force} "${FRAG_DIRS[@]}"; then
                ROWS+=("PASS|$MERGE_STEM|склейка|$((SECONDS-t0))|$total")
                # склейка успешна (проверки внутри: части посимвольно, ссылки
                # целы) — удаляем каталоги фрагментов; --debug оставляет их
                if [ "$DEBUG" = 0 ]; then
                    rm -rf "${FRAG_DIRS[@]}"
                    echo "[--] фрагменты удалены после склейки (${#FRAG_DIRS[@]} каталогов; --debug — оставить)"
                else
                    echo "[--] debug: фрагменты оставлены для проверки склейки"
                fi
            else
                ROWS+=("FAIL|$MERGE_STEM|склейка|$((SECONDS-t0))|$total"); fails=$((fails+1))
                echo "[!] фрагменты сохранены (склейка не прошла)"
            fi
        fi
    fi

    # пауза между файлами (охлаждение ПК), не после последнего; действует
    # независимо от фрагментации (файл без фрагментов — та же пауза)
    if [ "$EFF_PAUSE" -gt 0 ] && [ "$k" -lt "$N" ]; then
        echo "[--] пауза ${EFF_PAUSE}с перед следующим файлом (fragment-pause)…"
        sleep "$EFF_PAUSE"
    fi
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
