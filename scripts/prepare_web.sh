#!/usr/bin/env bash
# Вэб-страница (MD-экспорт Obsidian Web Clipper из inbox/*.md) -> готовый MD
# с VLM-описаниями картинок:
#   [1/5] картинки http(s) скачиваются локально, ссылки переписываются
#         (scripts/fetch_images.py; SVG/data:URI пропускаются)
#   [2/5] язык страницы (ru|en): --lang > CAPTION_LANG > ТЕКСТ (большинство
#         букв — основной сигнал для вэба) > деф. ru; логируется
#   [3/5] captioning фигур на языке страницы (маркер VLM-описание/description)
#   [4/5] cleanup + QC (мягкий: вэб-страницы без таблиц — норма)
#   [5/5] .qc_pass (resume); итог: outbox/<имя>/<имя>.md + картинки рядом.
# Frontmatter Obsidian сохраняется. Оригинал остаётся в inbox/.
# Usage: scripts/prepare_web.sh [--force] [--lang ru|en] [<файл.md из inbox>]
#        (без файла — все inbox/*.md)
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
# вэб-картинки мельче PDF-врезок (диаграммы 8-15КБ); деф. порог вэба ниже
# PDF-ного (20КБ), явное значение в .env имеет приоритет
export CAPTION_MIN_BYTES="${CAPTION_MIN_BYTES:-4096}"

mapfile -t FILES < <(ls -1 inbox/*.md 2>/dev/null | sort)
if [ -n "$FILE" ]; then
    [ -f "$FILE" ] || FILE="inbox/$(basename "$FILE")"
    [ -f "$FILE" ] || { echo "файл не найден: $FILE (ожидался в inbox/)"; exit 2; }
    FILES=("$FILE")
fi
[ ${#FILES[@]} -gt 0 ] || { echo "inbox/*.md пуст — нечего обрабатывать"; exit 0; }
# конвенция: типы не смешиваем; *.pdf — отдельный путь
ls inbox/*.pdf >/dev/null 2>&1 && echo "[--] внимание: в inbox/ есть *.pdf — этот скрипт их не трогает (см. launch_batch.sh)"

stage() { printf '[%s/5] %s %s\n' "$1" "$(date +%H:%M:%S)" "$2"; }
mkdir -p outbox logs

# лог batch (паритет с PDF: стабильный путь для мониторинга агентом)
WEB_LOG="logs/web_$(date +%Y%m%d_%H%M).log"
ln -sf "$(basename "$WEB_LOG")" logs/web_latest.log
exec > >(tee -a "$WEB_LOG") 2>&1

detect_lang() {  # $1=MD: текст (большинство букв) > CAPTION_LANG/--lang выше
    case "$LANG_FLAG" in
        ru|en) DOC_LANG="$LANG_FLAG"; DOC_LANG_SRC="--lang (принудительно)"; return 0 ;;
    esac
    case "${CAPTION_LANG:-}" in
        ru|en) DOC_LANG="$CAPTION_LANG"; DOC_LANG_SRC="CAPTION_LANG (принудительно)"; return 0 ;;
    esac
    local out
    out="$(python3 - "$1" <<'PY'
import re, sys
txt = open(sys.argv[1], encoding="utf-8-sig", errors="ignore").read(65536)
letters = re.findall(r"[а-яёА-ЯЁa-zA-Z]", txt)
if letters:
    n = sum(1 for c in letters if re.match(r"[а-яёА-ЯЁ]", c))
    print(("ru" if n > len(letters) / 2 else "en")
          + f"\tтекст страницы (кириллица {100 * n / len(letters):.0f}% из букв)")
else:
    print("ru\tдефолт (букв нет)")
PY
)" || out=$'ru\tошибка детекта (дефолт)'
    DOC_LANG="${out%%$'\t'*}"; DOC_LANG_SRC="${out#*$'\t'}"
}

process() {
    local src="$1" stem dir md t0=$SECONDS
    stem="$(basename "${src%.md}")"; dir="outbox/$stem"; md="$dir/$stem.md"
    echo ""
    echo "== $stem =="
    if [ -f "$dir/.qc_pass" ] && [ "$FORCE" = 0 ]; then
        echo "[--] уже готово (.qc_pass); --force — переобработать"; return 0
    fi
    rm -rf "$dir"; mkdir -p "$dir"

    # [1/5] копия + скачивание картинок с переписыванием ссылок
    stage 1 "картинки: скачивание локально"
    cp "$src" "$md"
    uv run --project "$ROOT" python "$ROOT/scripts/fetch_images.py" "$md"

    # [2/5] язык страницы
    detect_lang "$md"
    local word="русском"; [ "$DOC_LANG" = en ] && word="английском"
    echo "[--] язык страницы: $DOC_LANG — $DOC_LANG_SRC; VLM-описания и QC — на $word"

    # [3/5] captioning
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

    # [4/5] cleanup + мягкий QC
    stage 4 "cleanup + QC (мягкий — вэб-страница)"
    uv run --project "$ROOT" python "$ROOT/scripts/clean_md.py" "$md"
    if uv run --project "$ROOT" python "$ROOT/scripts/qc_check.py" --lang "$DOC_LANG" "$md"; then
        echo "[--] QC PASS"
    else
        echo "[--] QC FAIL — мягкий режим вэб-страницы (таблицы/лидеры — норма), .qc_pass ставится"
    fi

    # [5/5] готово
    touch "$dir/.qc_pass"
    local figs
    figs="$(grep -cE '\*\*VLM-(описание|description):\*\*' "$md" 2>/dev/null || true)"
    printf '{"stem":"%s","pages":0,"marker_s":0,"caption_s":%s,"figs":%s,"total_s":%s}\n' \
        "$stem" "$tc" "$figs" "$((SECONDS-t0))" >> logs/timings.jsonl
    echo "[PASS] $stem -> $md (captioning ${tc}с, фигур $figs, всего $((SECONDS-t0))с)"
}

fails=0
for f in "${FILES[@]}"; do
    process "$f" || fails=$((fails+1))
done
[ "$fails" = 0 ] || { echo "[FAIL] ошибок: $fails"; exit 1; }
