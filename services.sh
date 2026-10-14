#!/usr/bin/env bash
# services.sh — запуск, остановка и проверка inference-сервисов docpreparation.
#
# Управляет (llama-server, Vulkan/GPU Radeon 8060S):
#   1. Surya VLM (surya-2.gguf + mmproj, alias datalab-to/surya-ocr-2) —
#      layout/OCR для marker. prepare_doc.sh автоматически аттачится к нему
#      (SURYA_INFERENCE_URL) вместо spawn собственного сервера.
#   2. LLM для --use_llm marker (склейка кросс-страничных таблиц, постправка);
#      модель по умолчанию — локальная Qwen2.5-VL-7B-Instruct (UD-Q4_K_XL).
#
# Usage:
#   ./services.sh start [--no-llm] [--llm-model PATH]   # surya (+ LLM, если не --no-llm)
#   ./services.sh stop  [--no-llm]                       # остановка только своих сервисов
#   ./services.sh restart [--no-llm] [--llm-model PATH]
#   ./services.sh check [--deep]                         # статус; --deep = проба генерации
#
# Сервис считается «своим», если запущен этим скриптом (pid-файл). Чужой
# llama-server на том же порту не останавливается; при несовпадении имени
# модели на порту — ошибка (marker откажется аттачиться).
#
# Модели и порты (переопределяются окружением или .env):
#   SURYA_MODEL / SURYA_MMPROJ  (деф. /media/llm/Models/datalab-to/surya-ocr-2-gguf/…)
#   SURYA_PORT=8510  SURYA_PARALLEL=8  SURYA_CTX=parallel×12288
#   LLM_MODEL (деф. /media/llm/Models/qwen-image/Qwen2.5-VL-7B-Instruct-UD-Q4_K_XL.gguf)
#   LLM_PORT=8080  LLM_CTX=32768  LLM_PARALLEL=4
#   WAIT_SVC=600 (сек на загрузку модели в GPU)

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [ -f "$ROOT/.env" ]; then set -a; . "$ROOT/.env"; set +a; fi

SURYA_MODEL="${SURYA_GGUF_LOCAL_MODEL_PATH:-/media/llm/Models/datalab-to/surya-ocr-2-gguf/surya-2.gguf}"
SURYA_MMPROJ="${SURYA_GGUF_LOCAL_MMPROJ_PATH:-/media/llm/Models/datalab-to/surya-ocr-2-gguf/surya-2-mmproj.gguf}"
SURYA_ALIAS="datalab-to/surya-ocr-2"
SURYA_PORT="${SURYA_PORT:-8510}"
SURYA_PARALLEL="${SURYA_PARALLEL:-8}"
SURYA_CTX="${SURYA_CTX:-$((SURYA_PARALLEL * 12288))}"
LLM_MODEL="${LLM_MODEL:-/media/llm/Models/qwen-image/Qwen2.5-VL-7B-Instruct-UD-Q4_K_XL.gguf}"
LLM_PORT="${LLM_PORT:-8080}"
LLM_CTX="${LLM_CTX:-32768}"
LLM_PARALLEL="${LLM_PARALLEL:-4}"
HOST="${SURYA_INFERENCE_HOST:-127.0.0.1}"
WAIT_SVC="${WAIT_SVC:-600}"

SURYA_PID="/tmp/docprep-surya.pid"
SURYA_LOG="/tmp/docprep-surya.log"
LLM_PID="/tmp/docprep-llm.pid"
LLM_LOG="/tmp/docprep-llm.log"

ok()   { printf '  [OK]   %s\n' "$*"; }
bad()  { printf '  [FAIL] %s\n' "$*"; }
warn() { printf '  [WARN] %s\n' "$*"; }
inf()  { printf '  [--]   %s\n' "$*"; }
die()  { printf 'Ошибка: %s\n' "$*" >&2; exit 1; }

model_id() {  # первый id из /v1/models на порту, или пусто
  curl -s -m 5 "http://$HOST:$1/v1/models" \
    | python3 -c 'import json,sys
try: print(json.load(sys.stdin)["data"][0]["id"])
except Exception: pass' 2>/dev/null || true
}

svc_up() { [ -n "$(model_id "$1")" ]; }

svc_owned() {
  [ -f "$2" ] || return 1
  local pid; pid="$(cat "$2" 2>/dev/null || true)"
  [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null
}

llama_version() { llama-server --version 2>&1 | head -1 || true; }

# start_svc <имя> <pid-файл> <лог> <порт> <ожидаемый alias> <args…>
start_svc() {
  local name="$1" pidfile="$2" log="$3" port="$4" alias="$5"; shift 5
  if svc_up "$port"; then
    local mid; mid="$(model_id "$port")"
    if [ "$mid" = "$alias" ]; then
      if svc_owned "$port" "$pidfile"; then ok "$name уже запущен этим скриптом (pid $(cat "$pidfile"))"
      else warn "$name: порт :$port уже отдаёт нужную модель (чужой процесс) — не трогаю"
      fi
      return 0
    else
      die "$name: на :$port чужая модель '$mid' (ожидалась '$alias') — останови её или смени порт"
    fi
  fi
  : > "$log"
  setsid llama-server --host "$HOST" --port "$port" "$@" \
    >> "$log" 2>&1 < /dev/null &
  echo $! > "$pidfile"
  local pid; pid="$(cat "$pidfile")"
  inf "$name: pid $pid, жду модель в GPU (до ${WAIT_SVC}s), лог: $log"
  local i mid
  for i in $(seq 1 "$WAIT_SVC"); do
    mid="$(model_id "$port")"
    if [ -n "$mid" ]; then
      if [ "$mid" = "$alias" ]; then
        ok "$name: :$port, модель '$mid' готова"; return 0
      else
        bad "$name: поднялась модель '$mid' вместо '$alias' (лог: $log)"; return 1
      fi
    fi
    if ! kill -0 "$pid" 2>/dev/null; then
      bad "$name: процесс умер, последние строки лога:"; tail -5 "$log"; return 1
    fi
    sleep 1
  done
  bad "$name: не поднялся за ${WAIT_SVC}s (лог: $log)"; return 1
}

# stop_svc <имя> <pid-файл> <порт>
stop_svc() {
  local name="$1" pidfile="$2" port="$3"
  if svc_owned "$port" "$pidfile"; then
    local pid; pid="$(cat "$pidfile")"
    inf "$name: остановка (группа процессов pid $pid)…"
    kill -- -"$pid" 2>/dev/null || kill "$pid" 2>/dev/null || true
    local i
    for i in $(seq 1 30); do kill -0 "$pid" 2>/dev/null || break; sleep 1; done
    kill -0 "$pid" 2>/dev/null && kill -9 -"$pid" 2>/dev/null || true
    rm -f "$pidfile"
    ok "$name остановлен"
  elif svc_up "$port"; then
    warn "$name: на :$port сервер, запущенный НЕ этим скриптом — не останавливаю"
  else
    ok "$name не запущен"
  fi
}

start_surya() {
  [ -f "$SURYA_MODEL" ]  || die "surya: модель не найдена: $SURYA_MODEL"
  [ -f "$SURYA_MMPROJ" ] || die "surya: mmproj не найден: $SURYA_MMPROJ"
  inf "surya: $SURYA_MODEL (+ mmproj), parallel=$SURYA_PARALLEL, ctx=$SURYA_CTX"
  start_svc surya "$SURYA_PID" "$SURYA_LOG" "$SURYA_PORT" "$SURYA_ALIAS" \
    -m "$SURYA_MODEL" --mmproj "$SURYA_MMPROJ" \
    -ngl 99 --parallel "$SURYA_PARALLEL" --ctx-size "$SURYA_CTX" \
    --alias "$SURYA_ALIAS" --jinja
}

start_llm() {
  [ -f "$LLM_MODEL" ] || die "llm: модель не найдена: $LLM_MODEL"
  inf "llm: $LLM_MODEL, parallel=$LLM_PARALLEL, ctx=$LLM_CTX"
  start_svc llm "$LLM_PID" "$LLM_LOG" "$LLM_PORT" "$(basename "${LLM_MODEL%.gguf}")" \
    -m "$LLM_MODEL" -ngl 99 --parallel "$LLM_PARALLEL" --ctx-size "$LLM_CTX" \
    --alias "$(basename "${LLM_MODEL%.gguf}")" --jinja
}

# deep_chat <порт> <модель> — проба генерации (max_tokens 300: thinking-модели
# тратят бюджет на reasoning до content)
deep_chat() {
  local port="$1" model="$2"
  local out; out="$(curl -s -m 300 "http://$HOST:$port/v1/chat/completions" \
    -H 'Content-Type: application/json' \
    -d "{\"model\":\"$model\",\"messages\":[{\"role\":\"user\",\"content\":\"Ответь одним словом: работает\"}],\"max_tokens\":300}" | \
    python3 -c 'import json,sys
try:
    m=json.load(sys.stdin)["choices"][0]["message"]
    t=m.get("content") or m.get("reasoning_content") or ""
    print(t.strip()[:60])
except Exception: print("")' 2>/dev/null || true)"
  [ -n "$out" ] && ok "chat ($model @ :$port): «$out»" \
    || bad "chat ($model @ :$port) не ответил"
}

check() {
  echo "== llama-server =="
  inf "$(llama_version)"
  command -v llama-server >/dev/null || bad "llama-server не в PATH"

  echo "== Surya VLM (:$SURYA_PORT) — OCR/layout для marker =="
  local mid="" ; mid="$(model_id "$SURYA_PORT")"
  if [ -n "$mid" ]; then
    if svc_owned "$SURYA_PORT" "$SURYA_PID"; then ok "запущен этим скриптом (pid $(cat "$SURYA_PID"))"
    else ok "запущен внешне (этот скрипт его не остановит)"; fi
    [ "$mid" = "$SURYA_ALIAS" ] && ok "модель: $mid (marker аттачится)" \
      || bad "модель '$mid' != '$SURYA_ALIAS' — marker откажется аттачиться"
    inf "prepare_doc.sh подхватит автоматически (SURYA_INFERENCE_URL)"
  else
    bad "не запущен (./services.sh start)"
  fi

  echo "== LLM для --use_llm (:$LLM_PORT) =="
  mid="$(model_id "$LLM_PORT")"
  if [ -n "$mid" ]; then
    if svc_owned "$LLM_PORT" "$LLM_PID"; then ok "запущен этим скриптом (pid $(cat "$LLM_PID"))"
    else ok "запущен внешне (этот скрипт его не остановит)"; fi
    ok "модель: $mid"
    inf "флаги для marker: --use_llm --llm_service marker.services.openai.OpenAIService --openai_base_url http://$HOST:$LLM_PORT/v1 --openai_model $mid --openai_api_key none"
  else
    warn "не запущен (нужен только для --use_llm; ./services.sh start без --no-llm)"
  fi

  if [ -x "$ROOT/.venv/bin/marker_single" ]; then ok "marker_single в .venv доступен"
  else warn "marker_single не найден в .venv (uv sync)"; fi

  if [ "$DEEP" = 1 ]; then
    echo "== Deep-пробы генерации =="
    mid="$(model_id "$SURYA_PORT")"; [ "$mid" = "$SURYA_ALIAS" ] && deep_chat "$SURYA_PORT" "$mid"
    mid="$(model_id "$LLM_PORT")";    [ -n "$mid" ] && deep_chat "$LLM_PORT" "$mid"
  fi
}

# ------------------------------------------------------------------ main ----

CMD="help"; [ $# -gt 0 ] && CMD="$1"; shift || true
DEEP=0 NO_LLM=0
for arg in "$@"; do
  case "$arg" in
    --deep) DEEP=1 ;;
    --no-llm) NO_LLM=1 ;;
    --llm-model) : ;; # значение разберём ниже
    *) die "неизвестный аргумент: $arg (см. ./services.sh help)" ;;
  esac
done
# --llm-model PATH (отдельно, т.к. принимает значение)
Prev=""
for arg in "$@"; do
  if [ "$Prev" = "--llm-model" ]; then LLM_MODEL="$arg"; fi
  Prev="$arg"
done

case "$CMD" in
  start)
    echo "== Start =="
    start_surya || true
    if [ "$NO_LLM" = 1 ]; then inf "LLM пропущен (--no-llm)"; else start_llm || true; fi
    echo "Готово. Проверка: ./services.sh check"
    ;;
  stop)
    echo "== Stop =="
    stop_svc llm "$LLM_PID" "$LLM_PORT"
    if [ "$NO_LLM" = 1 ]; then inf "surya не трогаем (--no-llm)"; else stop_svc surya "$SURYA_PID" "$SURYA_PORT"; fi
    ;;
  restart)
    echo "== Restart =="
    "$0" stop ${NO_LLM:+--no-llm}; "$0" start ${NO_LLM:+--no-llm} ${LLM_MODEL:+--llm-model "$LLM_MODEL"}
    ;;
  check|status) check ;;
  help|-h|--help|*)
    sed -n '2,29p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
    ;;
esac
