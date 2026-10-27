#!/usr/bin/env bash
# PDF -> Markdown: [1/6] marker (balanced, метки страниц ⌗p.N) -> [2/6]
#   captioning -> [3/6] cleanup (лидеры TOC; {N}----- -> ⌗p.N+1) -> [4/6] QC
#   -> [5/6] ретрай --force_ocr (ТОЛЬКО с флагом --retry-ocr; для фрагментов
#   QC всегда мягкий) -> [6/6] готово.
# Метки страниц PDF (⌗p.N, 1-based как в просмотрщике) — всегда, отключить:
#   --no-page-anchors. Позволяют из БЗ ссылаться на исходную страницу файла.
# Диапазон: --pages "S[-E]" (1-индекс; без E или E=0 — до конца документа).
#   Фрагмент пишется в outbox/<имя>__pS-E/ (полный документ — в outbox/<имя>/).
# Статус: outbox/<...>/.status; успех: .qc_pass (resume); --force — переобработка.
# Captioning: локальный LLM-сервис (services.sh) или облако через env
#   CAPTION_API/CAPTION_MODEL/CAPTION_API_KEY (см. .env.example).
# Язык документа (ru|en, только эти два): флаг --lang > env CAPTION_LANG >
#   авто-детект (имя файла -> текст документа -> метаданные pdfinfo; деф. ru).
#   Управляет языком VLM-описаний и гейтом QC (кириллица/латиница >= 55%);
#   определение пишется в лог.
# Usage: prepare_doc.sh [--force] [--retry-ocr] [--lang ru|en] [--pages "S[-E]"]
#        [--no-page-anchors] <file.pdf> [флаги marker_single]
set -euo pipefail

FORCE=0 PAGES="" RETRY_OCR="${RETRY_OCR:-0}" LANG_FLAG="" PAGE_ANCHORS=1
while :; do
    case "${1:-}" in
        --force) FORCE=1; shift ;;
        --pages) PAGES="${2:?--pages требует значение}"; shift 2 ;;
        --retry-ocr) RETRY_OCR=1; shift ;;
        --lang) LANG_FLAG="${2:?--lang требует значение}"; shift 2 ;;
        --no-page-anchors) PAGE_ANCHORS=0; shift ;;
        *) break ;;
    esac
done
case "$LANG_FLAG" in ""|ru|en) ;; *) echo "--lang: ожидается ru|en"; exit 2 ;; esac

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
if [ -f "$ROOT/.env" ]; then set -a; . "$ROOT/.env"; set +a; fi

# Аттач к OCR-стеку (services.sh): если поднят прокси-адаптер (по умолчанию
# в modelset 1/2) — цепляемся к нему (layout/table_rec→surya,
# block-OCR→PaddleOCR-VL/Chandra); иначе к постоянному surya-серверу напрямую.
SURYA_PORT="${SURYA_PORT:-8510}"
OCR_PROXY_PORT="${OCR_PROXY_PORT:-8511}"
ATTACHED_PROXY=0
if [ -z "${SURYA_INFERENCE_URL:-}" ]; then
    if curl -sf -m 2 "http://127.0.0.1:$OCR_PROXY_PORT/health" >/dev/null 2>&1; then
        export SURYA_INFERENCE_URL="http://127.0.0.1:$OCR_PROXY_PORT/v1"
        ATTACHED_PROXY=1
        echo "[--] аттач к OCR-прокси :$OCR_PROXY_PORT"
    elif curl -sf -m 2 "http://127.0.0.1:$SURYA_PORT/v1/models" >/dev/null 2>&1; then
        export SURYA_INFERENCE_URL="http://127.0.0.1:$SURYA_PORT/v1"
        echo "[--] аттач к постоянному surya-серверу :$SURYA_PORT"
    fi
elif [ "${SURYA_INFERENCE_URL#*:8511}" != "$SURYA_INFERENCE_URL" ]; then
    ATTACHED_PROXY=1
fi

# Факт-скорость marker: дельта /stats прокси (surya_layout = готовые страницы).
# watch_speed <страниц в фрагменте> — фон при marker, печать при изменении.
watch_speed() {
    local n="$1" base="http://127.0.0.1:$OCR_PROXY_PORT" t0=$SECONDS
    local base_layout fp0 last=-1
    base_layout="$(curl -s -m 2 "$base/stats" | python3 -c 'import json,sys
try: print(json.load(sys.stdin).get("surya_layout",0))
except Exception: print(0)' 2>/dev/null || echo 0)"
    fp0="$(curl -s -m 2 "$base/stats" | python3 -c 'import json,sys
try: print(json.load(sys.stdin).get("surya_fullpage",0))
except Exception: print(0)' 2>/dev/null || echo 0)"
    while sleep 15; do
        local st done fp el rate extra
        st="$(curl -s -m 2 "$base/stats" 2>/dev/null)" || continue
        read -r done fp <<EOF
$(printf '%s' "$st" | python3 -c 'import json,sys
try:
    d=json.load(sys.stdin); print(d.get("surya_layout",0), d.get("surya_fullpage",0))
except Exception: print(-1,-1)' 2>/dev/null || echo "-1 -1")
EOF
        [ "$done" -ge 0 ] 2>/dev/null || continue
        done=$((done - base_layout)); fp=$((fp - fp0))
        [ "$done" = "$last" ] && continue
        last="$done"; el=$((SECONDS - t0))
        rate="—"; [ "$done" -gt 0 ] && rate="$(awk "BEGIN{printf \"%.1f\", $el/$done}")"
        extra=""; [ "$fp" -gt 0 ] && extra=" | full-page OCR: $fp стр"
        printf '[--] marker факт: %dм%02dс | layout %s/%s стр | %s с/стр%s\n' \
            "$((el/60))" "$((el%60))" "$done" "$n" "$rate" "$extra"
    done
}

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

qc() { uv run --project "$ROOT" python "$ROOT/scripts/qc_check.py" --lang "$DOC_LANG" "$MD"; }

CAPTION_FIGURES="${CAPTION_FIGURES:-1}"
cleanup() {
    stage 3/6 "cleanup (лидеры оглавления)"
    uv run --project "$ROOT" python "$ROOT/scripts/clean_md.py" "$MD"
}
# Язык документа (ru|en): --lang (принудительно) > CAPTION_LANG > имя файла
# (кириллица) > ТЕКСТ документа (сильнейший сигнал — он и есть содержимое) >
# метаданные pdfinfo (слабый сигнал: переведённые издания, шаблонный Title,
# годится как fallback). Деф. ru. Результат: DOC_LANG + DOC_LANG_SRC.
detect_lang() {
    case "$LANG_FLAG" in
        ru|en) DOC_LANG="$LANG_FLAG"; DOC_LANG_SRC="--lang (принудительно)"; return 0 ;;
    esac
    case "${CAPTION_LANG:-}" in
        ru|en) DOC_LANG="$CAPTION_LANG"; DOC_LANG_SRC="CAPTION_LANG (принудительно)"; return 0 ;;
    esac
    local out
    out="$(python3 - "$IN" "$MD" <<'PY'
import re, subprocess, sys
pdf, md = sys.argv[1], sys.argv[2]
cyr = re.compile(r"[а-яёА-ЯЁ]")
# 1) имя файла: кириллица -> ru (явный человеческий сигнал)
if cyr.search(pdf):
    print("ru\tимя файла (кириллица)"); sys.exit(0)
# 2) текст документа — сильнейший сигнал: доля кириллицы среди букв
try:
    txt = open(md, encoding="utf-8", errors="ignore").read(65536)
    letters = re.findall(r"[а-яёА-ЯЁa-zA-Z]", txt)
    if letters:
        n = sum(1 for c in letters if cyr.match(c))
        print(("ru" if n / len(letters) >= 0.3 else "en")
              + "\tтекст документа (кириллица "
              + f"{100 * n / len(letters):.0f}%)")
        sys.exit(0)
except Exception:
    pass
# 3) метаданные pdfinfo (fallback: нет текста): Title/Author/Subject/Keywords
try:
    meta = subprocess.run(["pdfinfo", pdf], capture_output=True,
                          text=True, timeout=10).stdout
    vals = [l.split(":", 1)[1] for l in meta.splitlines()
            if ":" in l and l.split(":", 1)[0].strip()
            in ("Title", "Author", "Subject", "Keywords")]
    joined = " ".join(vals).strip()
    if joined:
        if cyr.search(joined):
            print("ru\tметаданные PDF"); sys.exit(0)
        if re.search(r"[A-Za-z]", joined):
            print("en\tметаданные PDF"); sys.exit(0)
except Exception:
    pass
print("ru\tдефолт (сигналов нет)")
PY
)" || out=$'ru\tошибка детекта (дефолт)'
    DOC_LANG="${out%%$'\t'*}"; DOC_LANG_SRC="${out#*$'\t'}"
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
    uv run --project "$ROOT" python "$ROOT/scripts/caption_figures.py" \
        --lang "$DOC_LANG" "$MD" \
        || echo "[--] captioning: ошибка, продолжаем без описаний"
    TC=$((SECONDS-tc0))
}

echo "== $STEM (${N_PAGES} стр.) =="
WATCHER_PID=""
stop_watcher() { [ -n "$WATCHER_PID" ] && kill "$WATCHER_PID" 2>/dev/null || true; WATCHER_PID=""; }
trap stop_watcher EXIT
stage 1/6 "marker (balanced)"
# вендор-патч marker (raw-HTML хвост на больших документах; scripts/patch_marker.py,
# HANDOFF «raw-HTML баг»): идемпотентно, живёт в .venv до uv sync
if ! PATCH_OUT="$(uv run --project "$ROOT" python "$ROOT/scripts/patch_marker.py" 2>&1)"; then
    echo "[WARN] вендор-патч marker НЕ применён: $PATCH_OUT"
    echo "[WARN] риск raw-HTML хвоста на >111 стр. — QC-гейт это поймает"
fi
# marker именует выход по имени файла: гоняем во временный каталог и переносим
mkdir -p "$ROOT/logs"
TMP="$OUT/.tmp.$$"; rm -rf "$TMP"; mkdir -p "$TMP"
local_tm0=$SECONDS
[ "$ATTACHED_PROXY" = 1 ] && { watch_speed "$N_PAGES" & WATCHER_PID=$!; }
# метки страниц: paginate_output=true конфигом (CLI-флага нет); при своём
# --config_json у пользователя — не вмешиваемся, предупреждаем
PAGCONF=()
if [ "$PAGE_ANCHORS" = 1 ]; then
    case " ${EXTRA[*]-} " in
        *" --config_json "*)
            echo "[--] свой --config_json: метки страниц ⌗p не гарантируются (добавь в json \"paginate_output\": true)" ;;
        *)
            printf '{"paginate_output": true}' > "$TMP/paginate.json"
            PAGCONF=(--config_json "$TMP/paginate.json") ;;
    esac
fi
# лог marker — для table repair (провальные таблицы, USAGE §2.6-Б)
uv run --project "$ROOT" marker_single "$IN" --output_dir "$TMP" --mode balanced \
    "${PAGE_ARGS[@]}" "${PAGCONF[@]}" ${EXTRA[@]+"${EXTRA[@]}"} 2>&1 | tee "$ROOT/logs/${STEM}.marker.log"
stop_watcher
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

# [1.5] Ремонт провальных таблиц (USAGE §2.6-Б): 2-я попытка fast/pdftext по
# страницам из «Table OCR failed» лога marker; вставка в конец секции страницы;
# отчёт: восстановлено/не восстановлено (страница + блок + метрики).
# Работает и в фрагментном режиме (индексы /page/N в логе marker —
# документные 0-based, {N}-метки фрагмента тоже документные; 2026-10-24).
if [ "${TABLE_REPAIR_DONE:-0}" = 0 ] && [ -s "$ROOT/logs/${STEM}.marker.log" ]; then
    RP_OUT="$(uv run --project "$ROOT" python "$ROOT/scripts/table_repair.py" --parse "$ROOT/logs/${STEM}.marker.log" || true)"
    RP_PAGES="$(printf '%s\n' "$RP_OUT" | sed -n 's/^TABLE_REPAIR_PAGES=//p' | head -1)"
    if [ -n "$RP_PAGES" ]; then
        echo "[--] table repair: провальные таблицы — 2-я попытка (fast/pdftext):"
        printf '%s\n' "$RP_OUT" | grep '^  стр\.' | sed 's/^/    /'
        OLDIFS="$IFS"; IFS=','
        for idx in $RP_PAGES; do
            TR="$OUT/.repair.$$"; rm -rf "$TR"; mkdir -p "$TR"
            if uv run --project "$ROOT" marker_single "$IN" --output_dir "$TR" \
                --page_range "$idx-$idx" --mode fast >/dev/null 2>&1 \
                && [ -f "$TR/$STEM_BASE/$STEM_BASE.md" ]; then
                uv run --project "$ROOT" python "$ROOT/scripts/table_repair.py" --splice \
                    --main "$MD" --page "$idx" --retry "$TR/$STEM_BASE/$STEM_BASE.md" \
                    | sed 's/^/    /' || true
            else
                echo "    стр. $((idx+1)): fast-ретрай не удался (ошибка marker)"
            fi
            rm -rf "$TR"
        done
        IFS="$OLDIFS"
    fi
fi
TABLE_REPAIR_DONE=1

# язык документа: один раз на файл (нужен и captioning, и QC), логируем
detect_lang
word="русском"; [ "$DOC_LANG" = en ] && word="английском"
echo "[--] язык документа: $DOC_LANG — $DOC_LANG_SRC; VLM-описания и QC — на $word"

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
        [ "${#PAGCONF[@]}" -gt 0 ] && printf '{"paginate_output": true}' > "$TMP/paginate.json"
        local_tm0=$SECONDS
        [ "$ATTACHED_PROXY" = 1 ] && { watch_speed "$N_PAGES" & WATCHER_PID=$!; }
        uv run --project "$ROOT" marker_single "$IN" --output_dir "$TMP" --mode balanced --force_ocr \
            "${PAGE_ARGS[@]}" "${PAGCONF[@]}" ${EXTRA[@]+"${EXTRA[@]}"}
        stop_watcher
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

FIGS="$(grep -cE '\*\*VLM-(описание|description):\*\*' "$MD" 2>/dev/null || true)"
touch "$DIR/.qc_pass"
stage 6/6 "готово"
mkdir -p "$ROOT/logs"
printf '{"stem":"%s","pages":%s,"marker_s":%s,"caption_s":%s,"figs":%s,"total_s":%s}\n' \
    "$STEM" "$N_PAGES" "$TM" "$TC" "$FIGS" "$((SECONDS-T0))" >> "$ROOT/logs/timings.jsonl"
echo "[PASS] $STEM -> $MD (marker ${TM}с = $(awk "BEGIN{printf \"%.1f\", $TM/$N_PAGES}") с/стр; всего $((SECONDS-T0))с)"
[ "$QCRC" = 0 ] || echo "[!] QC мягко провален (фрагмент/выборка) — проверь вручную"
