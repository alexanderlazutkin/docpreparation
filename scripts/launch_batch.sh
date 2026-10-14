#!/usr/bin/env bash
# Открывает ВИДИМОЕ окно терминала с выполняемым prepare_all.sh — для запуска
# агентом (opencode): пользователь наблюдает стадии и прогресс обработки,
# агент следит по logs/batch_latest.log и logs/batch_latest.result.
# Требует запущенных сервисов (./services.sh start).
# Usage: scripts/launch_batch.sh [--force]
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ARGS="${*:-}"
HOLD="echo; echo 'Обработка завершена. Окно закроется само через 5 мин (или Enter)'; read -t 300 -r _ || true"
CMD="cd '$ROOT' && scripts/prepare_all.sh $ARGS; rc=\$?; $HOLD; exit \$rc"

if command -v gnome-terminal >/dev/null 2>&1; then
    setsid gnome-terminal --hide-menubar --title="docprep: batch" -- bash -lc "$CMD" >/dev/null 2>&1 &
elif command -v konsole >/dev/null 2>&1; then
    setsid konsole -p ToolBarVisible=false -e bash -lc "$CMD" >/dev/null 2>&1 &
elif command -v xterm >/dev/null 2>&1; then
    setsid xterm -title "docprep: batch" -e bash -lc "$CMD" >/dev/null 2>&1 &
else
    echo "Терминал не найден (gnome-terminal/konsole/xterm)." >&2
    echo "Запусти вручную: scripts/prepare_all.sh $ARGS" >&2
    exit 1
fi
echo "Окно терминала открыто (batch $ARGS). Мониторинг агентом:"
echo "  tail -f logs/batch_latest.log     # ход обработки"
echo "  cat logs/batch_latest.result      # итог (появится по завершении)"
