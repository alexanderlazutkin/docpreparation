#!/usr/bin/env bash
# Открывает ВИДИМОЕ окно терминала с пакетной обработкой — для запуска агентом
# (opencode): пользователь наблюдает стадии, агент следит по logs/.
# Требует запущенных сервисов (./services.sh start).
#
# Диспетчер по типу исх. файлов в inbox/ (конвенция: типы НЕ смешиваем):
#   *.md  (вэб-клипы Obsidian) -> scripts/prepare_web.sh
#         (PDF-флаги --pages/--fragments/--fragment-pause/--debug/--retry-ocr
#          НЕ применимы — отбрасываются с подсказкой; остаются --force/--lang)
#   *.pdf -> scripts/prepare_all.sh (все флаги, как раньше)
#   пусто / оба типа сразу -> понятная ошибка.
#
# Usage: scripts/launch_batch.sh [--force] [--lang ru|en] [PDF-флаги prepare_all]
# Debug: LAUNCH_BATCH_ECHO=1 — напечатать команду вместо открытия окна.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

HAS_MD=0 HAS_PDF=0
ls inbox/*.md  >/dev/null 2>&1 && HAS_MD=1
ls inbox/*.pdf >/dev/null 2>&1 && HAS_PDF=1

if [ "$HAS_MD" = 1 ] && [ "$HAS_PDF" = 1 ]; then
    echo "inbox/ содержит ОБА типа (*.md и *.pdf) — конвенция: не смешивать." >&2
    echo "Убери лишний тип и перезапусти." >&2
    exit 2
elif [ "$HAS_MD" = 1 ]; then
    MODE="web"
elif [ "$HAS_PDF" = 1 ]; then
    MODE="pdf"
else
    echo "inbox/ пуст — нечего обрабатывать." >&2
    exit 2
fi

ARGS="${*:-}"

if [ "$MODE" = web ]; then
    # для MD оставляем только применимые флаги; PDF-специфика — отбрасываем
    WARGS=()
    while [ $# -gt 0 ]; do
        case "$1" in
            --force) WARGS+=(--force); shift ;;
            --lang)  WARGS+=(--lang "${2:?--lang требует значение}"); shift 2 ;;
            --lang=*) WARGS+=("$1"); shift ;;
            --pages|--fragments|--fragment-pause|--fragment_pause)
                echo "[--] $1 <знач.> — только для PDF, для MD пропущен"; shift 2 ;;
            --pages=*|--fragments=*|--fragment-pause=*|--fragment_pause=*)
                echo "[--] $1 — только для PDF, для MD пропущен"; shift ;;
            --debug|--retry-ocr|--no-page-anchors)
                echo "[--] $1 — только для PDF, для MD пропущен"; shift ;;
            *) echo "[--] неизвестный флаг: $1 — пропущен"; shift ;;
        esac
    done
    RUN="scripts/prepare_web.sh ${WARGS[*]:-}"
else
    RUN="scripts/prepare_all.sh ${ARGS}"
fi

HOLD="echo; echo 'Обработка завершена. Окно закроется само через 5 мин (или Enter)'; read -t 300 -r _ || true"
CMD="cd '$ROOT' && $RUN; rc=\$?; $HOLD; exit \$rc"

if [ "${LAUNCH_BATCH_ECHO:-0}" = 1 ]; then
    echo "MODE=$MODE"
    echo "CMD=$RUN"
    exit 0
fi

if command -v gnome-terminal >/dev/null 2>&1; then
    setsid gnome-terminal --hide-menubar --title="docprep: batch ($MODE)" -- bash -lc "$CMD" >/dev/null 2>&1 &
elif command -v konsole >/dev/null 2>&1; then
    setsid konsole -p ToolBarVisible=false -e bash -lc "$CMD" >/dev/null 2>&1 &
elif command -v xterm >/dev/null 2>&1; then
    setsid xterm -title "docprep: batch ($MODE)" -e bash -lc "$CMD" >/dev/null 2>&1 &
else
    echo "Терминал не найден (gnome-terminal/konsole/xterm)." >&2
    echo "Запусти вручную: $RUN" >&2
    exit 1
fi
echo "Окно терминала открыто (batch/$MODE: $RUN). Мониторинг агентом:"
echo "  tail -f logs/batch_latest.log     # ход (только PDF-batch)"
echo "  ls outbox/                        # результаты (MD появляются по мере готовности)"
