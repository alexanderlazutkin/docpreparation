#!/usr/bin/env bash
# Открывает ВИДИМОЕ окно терминала с пакетной обработкой — для запуска агентом
# (opencode): пользователь наблюдает стадии, агент следит по logs/.
# Требует запущенных сервисов (./services.sh start); PPTX/CHM без captioning
# работают и без GPU.
#
# АВТО-ДИСПЕТЧЕР ПО ТИПАМ в inbox/ — типы МОЖНО смешивать, обработка по
# очереди, порядок: лёгкие без-OCR первыми, PDF-тяжеловес последним:
#   1. *.pptx                       -> scripts/prepare_pptx.sh
#   2. *.hhc (и inbox/*/*.hhc —     -> scripts/prepare_chm.sh
#      распакованный CHM каталогом)
#   3. *.md  (вэб-клипы Obsidian)   -> scripts/prepare_web.sh
#   4. *.pdf                        -> scripts/prepare_all.sh
#
# Общие флаги (всем типам): --force, --lang ru|en. PDF-only (--pages/
# --fragments/--fragment-pause/--debug/--retry-ocr/--no-page-anchors) к не-PDF
# типам не применимы — отбрасываются с подсказкой (передаются только prepare_all).
#
# Usage: scripts/launch_batch.sh [--force] [--lang ru|en] [PDF-флаги prepare_all]
# Debug: LAUNCH_BATCH_ECHO=1 — напечатать команды вместо открытия окна.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
# .env может задавать LAUNCH_BATCH_ECHO=1 как дефолт владельца (печать команд
# вместо окна — ручной запуск); агенту для окна — явный префикс ECHO=0
if [ -f "$ROOT/.env" ]; then set -a; . "$ROOT/.env"; set +a; fi

mapfile -t PPTXS < <(ls -1 inbox/*.pptx 2>/dev/null | grep -v '/~\$' | sort)
mapfile -t HHCS  < <(ls -1 inbox/*.hhc inbox/*/*.hhc 2>/dev/null | sort -u)
mapfile -t MDS   < <(ls -1 inbox/*.md 2>/dev/null | sort)
mapfile -t PDFS  < <(ls -1 inbox/*.pdf 2>/dev/null | sort)

TOTAL=$(( ${#PPTXS[@]} + ${#HHCS[@]} + ${#MDS[@]} + ${#PDFS[@]} ))
[ "$TOTAL" -gt 0 ] || { echo "inbox/ пуст — нечего обрабатывать." >&2; exit 2; }

# флаги для не-PDF типов: только --force/--lang
WARGS=()
while [ $# -gt 0 ]; do
    case "$1" in
        --force) WARGS+=(--force); shift ;;
        --lang)  WARGS+=(--lang "${2:?--lang требует значение}"); shift 2 ;;
        --lang=*) WARGS+=("$1"); shift ;;
        --pages|--fragments|--fragment-pause|--fragment_pause)
            echo "[--] $1 <знач.> — только для PDF, для не-PDF типов пропущен"; shift 2 ;;
        --pages=*|--fragments=*|--fragment-pause=*|--fragment_pause=*)
            echo "[--] $1 — только для PDF, для не-PDF типов пропущен"; shift ;;
        --debug|--retry-ocr|--no-page-anchors)
            echo "[--] $1 — только для PDF, для не-PDF типов пропущен"; shift ;;
        *) echo "[--] неизвестный флаг: $1 — пропущен"; shift ;;
    esac
done
ARGS="${*:-}"

# сборка последовательности (типы по очереди; сбой одного не стопает остальные)
RUN="" MODES=""
add() {  # $1=скрипт $2=флаги $3=метка
    [ -n "$RUN" ] && RUN+="; "
    RUN+="$1 ${2:-}"
    MODES+="${MODES:++}$3"
}
[ ${#PPTXS[@]} -gt 0 ] && add "scripts/prepare_pptx.sh" "${WARGS[*]:-}" "pptx"
[ ${#HHCS[@]}  -gt 0 ] && add "scripts/prepare_chm.sh"  "${WARGS[*]:-}" "chm"
[ ${#MDS[@]}   -gt 0 ] && add "scripts/prepare_web.sh"  "${WARGS[*]:-}" "web"
[ ${#PDFS[@]}  -gt 0 ] && add "scripts/prepare_all.sh" "$ARGS" "pdf"

echo "Диспетчер inbox/: pptx ${#PPTXS[@]}, chm ${#HHCS[@]}, md ${#MDS[@]}, pdf ${#PDFS[@]} -> $MODES"

HOLD="echo; echo 'Обработка завершена. Окно закроется само через 5 мин (или Enter)'; read -t 300 -r _ || true"
CMD="cd '$ROOT' && $RUN; rc=\$?; $HOLD; exit \$rc"

if [ "${LAUNCH_BATCH_ECHO:-0}" = 1 ]; then
    echo "MODE=$MODES"
    echo "CMD=$RUN"
    exit 0
fi

if command -v gnome-terminal >/dev/null 2>&1; then
    setsid gnome-terminal --hide-menubar --title="docprep: batch ($MODES)" -- bash -lc "$CMD" >/dev/null 2>&1 &
elif command -v konsole >/dev/null 2>&1; then
    setsid konsole -p ToolBarVisible=false -e bash -lc "$CMD" >/dev/null 2>&1 &
elif command -v xterm >/dev/null 2>&1; then
    setsid xterm -title "docprep: batch ($MODES)" -e bash -lc "$CMD" >/dev/null 2>&1 &
else
    echo "Терминал не найден (gnome-terminal/konsole/xterm)." >&2
    echo "Запусти вручную: $RUN" >&2
    exit 1
fi
echo "Окно терминала открыто (batch/$MODES). Мониторинг агентом:"
echo "  tail -f logs/batch_latest.log     # ход PDF-batch"
echo "  tail -f logs/pptx_latest.log      # ход PPTX"
echo "  tail -f logs/chm_latest.log       # ход CHM"
echo "  tail -f logs/web_latest.log       # ход вэб-MD"
echo "  ls outbox/                        # результаты (MD появляются по мере готовности)"
