#!/usr/bin/env bash
# services.sh — запуск, остановка и проверка inference-сервисов docpreparation.
#
# Управляет (llama-server, Vulkan/GPU Radeon 8060S):
#   1. Surya VLM (surya-2.gguf + mmproj, alias datalab-to/surya-ocr-2) —
#      layout/OCR для marker. prepare_doc.sh автоматически аттачится к нему
#      (SURYA_INFERENCE_URL) вместо spawn собственного сервера.
#   2. LLM для --use_llm marker и VLM-описаний фигур (captioning); модель
#      по умолчанию — локальная Qwen3.6-35B-A3B (UD-Q6_K_XL + mmproj-F32,
#      32.7 ГБ; склейка кросс-страничных таблиц, постправка, чтение схем).
#
# Usage:
#   ./services.sh start [--modelset N] [опции]   # N=1 (деф.): surya + прокси
#                                       # pass-through + LLM (прокси — по
#                                       # умолчанию; --no-ocr-proxy — marker
#                                       # напрямую к surya); N=2: surya +
#                                       # Chandra (:8513) + прокси
#                                       # (block-OCR->chandra) + LLM
#   ./services.sh start [--no-llm] [--llm-model PATH] [--llm-parallel N]   # N деф. 2
#                                       # surya (+ LLM, если не --no-llm);
#                                       # --llm-parallel — слоты LLM-сервера
#   ./services.sh start [--surya-model PATH] [--surya-mmproj PATH]
#                       [--surya-parallel N]          # подмена OCR-VLM за алиасом
#                                       # datalab-to/surya-ocr-2 (тест новых
#                                       # OCR-VLM); N деф. 8 (SURYA_PARALLEL)
#   ./services.sh stop [--no-llm]                       # остановка только своих сервисов
#   ./services.sh restart [--modelset N] [--no-llm] [--llm-model PATH] [--llm-parallel N]
#                       [--surya-model PATH] [--surya-mmproj PATH] [--surya-parallel N]
#   ./services.sh check [--deep]                         # статус; --deep = проба генерации
#
# Наборы моделей (MODELSET):
#   1 (деф.) — surya-2 :8510 + прокси :8511 pass-through (деф.,
#              --no-ocr-proxy отключает) + Qwen3.6-35B-A3B :8080 (--use_llm)
#   2        — surya-2 :8510 (layout/table для marker) + Chandra OCR 2 :8513
#              (block-OCR за прокси :8511, layout-HTML с таблицами/alt) +
#              Qwen3.6-35B-A3B :8080. Гейты Chandra: USAGE §3.7 (кириллица
#              97,5-100%, таблицы DMRB Table 4.19 ок, 13,6 с/стр @ parallel 4)
#
# Сервис считается «своим», если запущен этим скриптом (pid-файл). Чужой
# llama-server на том же порту не останавливается; при несовпадении имени
# модели на порту — ошибка (marker откажется аттачиться).
#
# Модели и порты (переопределяются окружением или .env):
#   Все веса по умолчанию — в models/ в КОРНЕ РЕПО (каталог в .gitignore;
#   куда что класть и ссылки — README «Веса моделей»).
#   MODELSET=1 (деф.; набор запускаемых OCR-сервисов, см. выше)
#   SURYA_MODEL / SURYA_MMPROJ  (деф. models/surya-ocr-2-gguf/…)
#   SURYA_PORT=8510  SURYA_PARALLEL=8  SURYA_CTX=parallel×12288
#   CHANDRA_MODEL / CHANDRA_MMPROJ (деф. models/chandra-ocr-2-gguf/…)
#   CHANDRA_PORT=8513  CHANDRA_PARALLEL=4  CHANDRA_CTX=parallel×8192
#   OCR_PROXY_PORT=8511 (scripts/ocr_proxy.py; modelset 1 — деф., 2 — всегда)
#   LLM_MODEL (деф. models/Qwen3.6-35B-A3B-MTP-GGUF/Qwen3.6-35B-A3B-UD-Q6_K_XL.gguf)
#   LLM_MMPROJ (деф. mmproj-F32.gguf рядом с моделью; пусто = текст-only сервис)
#   LLM_PORT=8080  LLM_CTX=32768  LLM_PARALLEL=2 (35B: 2 слота × 16k)
#   WAIT_SVC=600 (сек на загрузку модели в GPU)
#   LLAMA_BIN (деф. llama-server из PATH) — какой бинарь llama-server запускать
#      во всех сервисах. ПИНЬТЕ версию перед обновлением llama.cpp
#      (~/AI/install-llama-cpp.sh перелинковывает /usr/local/bin на новый
#      nightly; свежие сборки могут ломать грамматику guided-layout surya,
#      см. HANDOFF «Грабли»), напр. в .env:
#      LLAMA_BIN=/usr/local/lib/llama.cpp/b11734-linux-x64-vulkan/llama-b11734/llama-server

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [ -f "$ROOT/.env" ]; then set -a; . "$ROOT/.env"; set +a; fi

SURYA_MODEL="${SURYA_GGUF_LOCAL_MODEL_PATH:-$ROOT/models/surya-ocr-2-gguf/surya-2.gguf}"
SURYA_MMPROJ="${SURYA_GGUF_LOCAL_MMPROJ_PATH:-$ROOT/models/surya-ocr-2-gguf/surya-2-mmproj.gguf}"
SURYA_ALIAS="datalab-to/surya-ocr-2"
SURYA_PORT="${SURYA_PORT:-8510}"
SURYA_PARALLEL="${SURYA_PARALLEL:-8}"
SURYA_CTX="${SURYA_CTX:-$((SURYA_PARALLEL * 12288))}"
LLM_MODEL="${LLM_MODEL:-$ROOT/models/Qwen3.6-35B-A3B-MTP-GGUF/Qwen3.6-35B-A3B-UD-Q6_K_XL.gguf}"
LLM_MMPROJ="${LLM_MMPROJ:-$ROOT/models/Qwen3.6-35B-A3B-MTP-GGUF/mmproj-F32.gguf}"
LLM_PORT="${LLM_PORT:-8080}"
LLM_CTX="${LLM_CTX:-40000}"
LLM_PARALLEL="${LLM_PARALLEL:-2}"
CHANDRA_MODEL="${CHANDRA_MODEL:-$ROOT/models/chandra-ocr-2-gguf/chandra-ocr-2.Q8_0.gguf}"
CHANDRA_MMPROJ="${CHANDRA_MMPROJ:-$ROOT/models/chandra-ocr-2-gguf/chandra-ocr-2.mmproj-Q8_0.gguf}"
CHANDRA_ALIAS="chandra-ocr-2"
CHANDRA_PORT="${CHANDRA_PORT:-8513}"
CHANDRA_PARALLEL="${CHANDRA_PARALLEL:-4}"
CHANDRA_CTX="${CHANDRA_CTX:-$((CHANDRA_PARALLEL * 8192))}"
OCR_PROXY_PORT="${OCR_PROXY_PORT:-8511}"
HOST="${SURYA_INFERENCE_HOST:-127.0.0.1}"
WAIT_SVC="${WAIT_SVC:-600}"
# Бинарь llama-server для всех сервисов: имя (деф.) = поиск в PATH, как раньше;
# абсолютный путь = пин конкретной сборки. export — чтобы переживал restart
# (он заново вызывает $0) и передавался дочерним процессам.
LLAMA_BIN="${LLAMA_BIN:-llama-server}"
export LLAMA_BIN

SURYA_PID="/tmp/docprep-surya.pid"
SURYA_LOG="/tmp/docprep-surya.log"
LLM_PID="/tmp/docprep-llm.pid"
LLM_LOG="/tmp/docprep-llm.log"
CHANDRA_PID="/tmp/docprep-chandra.pid"
CHANDRA_LOG="/tmp/docprep-chandra.log"
PROXY_PID="/tmp/docprep-proxy.pid"
PROXY_LOG="/tmp/docprep-proxy.log"

ok()   { printf '  [OK]   %s\n' "$*"; }
bad()  { printf '  [FAIL] %s\n' "$*"; }
warn() { printf '  [WARN] %s\n' "$*"; }
inf()  { printf '  [--]   %s\n' "$*"; }
die()  { printf 'Ошибка: %s\n' "$*" >&2; exit 1; }

need_file() {  # need_file <имя> <путь>: вес должен существовать до старта
  [ -f "$2" ] || die "$1: файл не найден: $2
  положи веса в models/ в корне репо или задай путь в .env (см. .env.example)"
}

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

llama_version() { "$LLAMA_BIN" --version 2>&1 | head -1 || true; }

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
  # Ленивая проверка бинаря (только при старте): stop/help/check работают и без него
  case "$LLAMA_BIN" in
    */*) [ -x "$LLAMA_BIN" ] || { bad "$name: LLAMA_BIN не исполняемый: $LLAMA_BIN"; return 1; } ;;
    *)   command -v "$LLAMA_BIN" >/dev/null 2>&1 || { bad "$name: $LLAMA_BIN не найден в PATH"; return 1; } ;;
  esac
  : > "$log"
  setsid "$LLAMA_BIN" --host "$HOST" --port "$port" "$@" \
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
  need_file surya-модель "$SURYA_MODEL"
  need_file surya-mmproj "$SURYA_MMPROJ"
  inf "surya: $SURYA_MODEL (+ mmproj), parallel=$SURYA_PARALLEL, ctx=$SURYA_CTX"
  start_svc surya "$SURYA_PID" "$SURYA_LOG" "$SURYA_PORT" "$SURYA_ALIAS" \
    -m "$SURYA_MODEL" --mmproj "$SURYA_MMPROJ" \
    -ngl 99 --parallel "$SURYA_PARALLEL" --ctx-size "$SURYA_CTX" \
    --alias "$SURYA_ALIAS" --jinja
}

start_llm() {
  need_file llm-модель "$LLM_MODEL"
  local mmargs=()
  if [ -n "$LLM_MMPROJ" ]; then
    need_file llm-mmproj "$LLM_MMPROJ"
    mmargs=(--mmproj "$LLM_MMPROJ")
  fi
  inf "llm: $LLM_MODEL${LLM_MMPROJ:+ (VLM: $(basename "$LLM_MMPROJ"))}, parallel=$LLM_PARALLEL, ctx=$LLM_CTX"
  start_svc llm "$LLM_PID" "$LLM_LOG" "$LLM_PORT" "$(basename "${LLM_MODEL%.gguf}")" \
    -m "$LLM_MODEL" ${mmargs[@]+"${mmargs[@]}"} -ngl 99 --parallel "$LLM_PARALLEL" --ctx-size "$LLM_CTX" \
    --alias "$(basename "${LLM_MODEL%.gguf}")" --jinja
}

start_chandra() {
  need_file chandra-модель "$CHANDRA_MODEL"
  need_file chandra-mmproj "$CHANDRA_MMPROJ"
  inf "chandra: $CHANDRA_MODEL (+ mmproj), parallel=$CHANDRA_PARALLEL, ctx=$CHANDRA_CTX"
  start_svc chandra "$CHANDRA_PID" "$CHANDRA_LOG" "$CHANDRA_PORT" "$CHANDRA_ALIAS" \
    -m "$CHANDRA_MODEL" --mmproj "$CHANDRA_MMPROJ" \
    -ngl 99 --parallel "$CHANDRA_PARALLEL" --ctx-size "$CHANDRA_CTX" \
    --alias "$CHANDRA_ALIAS" --jinja
}

proxy_up() {  # /health прокси = оба бэкенда живы
  curl -sf -m 3 "http://$HOST:$OCR_PROXY_PORT/health" >/dev/null 2>&1
}

start_proxy() {
  if proxy_up; then
    if svc_owned "$OCR_PROXY_PORT" "$PROXY_PID"; then ok "ocr-proxy уже запущен этим скриптом (pid $(cat "$PROXY_PID"))"
    else warn "ocr-proxy: :$OCR_PROXY_PORT уже отвечает — не трогаю"; fi
    return 0
  fi
  [ -x "$ROOT/.venv/bin/python" ] || die "ocr-proxy: нет .venv/bin/python (uv sync)"
  [ -f "$ROOT/scripts/ocr_proxy.py" ] || die "ocr-proxy: нет scripts/ocr_proxy.py"
  : > "$PROXY_LOG"
  setsid env SURYA_URL="http://$HOST:$SURYA_PORT" \
    CHANDRA_URL="http://$HOST:$CHANDRA_PORT" \
    OCR_PROXY_PORT="$OCR_PROXY_PORT" SURYA_ALIAS="$SURYA_ALIAS" \
    CHANDRA_ALIAS="$CHANDRA_ALIAS" \
    OCR_PROXY_BLOCK_ENGINE="${OCR_PROXY_BLOCK_ENGINE:-surya}" \
    "$ROOT/.venv/bin/python" "$ROOT/scripts/ocr_proxy.py" \
    >> "$PROXY_LOG" 2>&1 < /dev/null &
  echo $! > "$PROXY_PID"
  local pid; pid="$(cat "$PROXY_PID")"
  inf "ocr-proxy: pid $pid, :$OCR_PROXY_PORT, лог: $PROXY_LOG"
  local route_info="block-OCR→${OCR_PROXY_BLOCK_ENGINE:-surya}"
  local i
  for i in $(seq 1 60); do
    if proxy_up; then ok "ocr-proxy: :$OCR_PROXY_PORT готов (layout/table→surya :$SURYA_PORT, $route_info)"; return 0; fi
    if ! kill -0 "$pid" 2>/dev/null; then
      bad "ocr-proxy: процесс умер, последние строки лога:"; tail -5 "$PROXY_LOG"; return 1
    fi
    sleep 1
  done
  bad "ocr-proxy: не поднялся за 60s (лог: $PROXY_LOG)"; return 1
}

# deep_chat <порт> <модель> [extra-kv] — проба генерации (max_tokens 300:
# thinking-модели тратят бюджет на reasoning до content; extra-kv — доп.
# поля JSON без внешних скобок, напр. '"chat_template_kwargs":{"enable_thinking":false}')
deep_chat() {
  local port="$1" model="$2" extra="${3:-}"
  local body="{\"model\":\"$model\",\"messages\":[{\"role\":\"user\",\"content\":\"Ответь одним словом: работает\"}],\"max_tokens\":300"
  [ -n "$extra" ] && body="$body,$extra"
  body="$body}"
  local out; out="$(curl -s -m 300 "http://$HOST:$port/v1/chat/completions" \
    -H 'Content-Type: application/json' \
    -d "$body" | \
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
  local bin; bin="$(command -v "$LLAMA_BIN" 2>/dev/null || true)"
  if [ -n "$bin" ]; then
    bin="$(readlink -f "$bin" 2>/dev/null || printf '%s' "$bin")"
    ok "бинарь: $bin"
    [ "$bin" = "$(readlink -f "$(command -v llama-server 2>/dev/null)" 2>/dev/null)" ] \
      || warn "LLAMA_BIN ($bin) отличается от llama-server из PATH — это пин версии, норма если задумано"
  else
    bad "llama-server не найден (LLAMA_BIN='$LLAMA_BIN')"
  fi

  echo "== Surya VLM (:$SURYA_PORT) — OCR/layout для marker =="
  local mid="" ; mid="$(model_id "$SURYA_PORT")"
  if [ -n "$mid" ]; then
    if svc_owned "$SURYA_PORT" "$SURYA_PID"; then ok "запущен этим скриптом (pid $(cat "$SURYA_PID"))"
    else ok "запущен внешне (этот скрипт его не остановит)"; fi
    [ "$mid" = "$SURYA_ALIAS" ] && ok "модель: $mid (marker аттачится)" \
      || bad "модель '$mid' != '$SURYA_ALIAS' — marker откажется аттачится"
    inf "prepare_doc.sh подхватит автоматически (SURYA_INFERENCE_URL)"
  else
    bad "не запущен (./services.sh start)"
  fi

  echo "== Chandra OCR 2 (:$CHANDRA_PORT) — block-OCR через прокси (modelset 2) =="
  mid="$(model_id "$CHANDRA_PORT")"
  if [ -n "$mid" ]; then
    if svc_owned "$CHANDRA_PORT" "$CHANDRA_PID"; then ok "запущен этим скриптом (pid $(cat "$CHANDRA_PID"))"
    else ok "запущен внешне (этот скрипт его не остановит)"; fi
    ok "модель: $mid"
  else
    inf "не запущен (нужен только для modelset 2; ./services.sh start --modelset 2)"
  fi

  echo "== OCR-прокси (:$OCR_PROXY_PORT) =="
  if proxy_up; then
    if svc_owned "$OCR_PROXY_PORT" "$PROXY_PID"; then ok "запущен этим скриптом (pid $(cat "$PROXY_PID"))"
    else ok "запущен внешне (этот скрипт его не остановит)"; fi
    local pengine; pengine="$(curl -s -m 3 "http://$HOST:$OCR_PROXY_PORT/stats" | python3 -c 'import json,sys
try: print(json.load(sys.stdin).get("block_engine","?"))
except Exception: print("?")' 2>/dev/null || echo '?')"
    ok "маршрут: layout/table_rec→surya :$SURYA_PORT, block-OCR→$pengine"
    inf "prepare_doc.sh аттачится сюда автоматически; статистика: curl :$OCR_PROXY_PORT/stats"
  else
    inf "не запущен (./services.sh start [--modelset 1 | --modelset 2])"
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
    mid="$(model_id "$CHANDRA_PORT")"; [ -n "$mid" ] && deep_chat "$CHANDRA_PORT" "$mid" \
      '"chat_template_kwargs":{"enable_thinking":false}'
  fi
}

# ------------------------------------------------------------------ main ----

CMD="help"; [ $# -gt 0 ] && CMD="$1"; shift || true
DEEP=0 NO_LLM=0 OCR_PROXY=0 OCR_PROXY_GIVEN=0 OCR_PROXY_OFF=0 PARALLEL_GIVEN=0 MODEL_GIVEN=0 MODELSET_GIVEN=0
S_PARALLEL_GIVEN=0 S_MODEL_GIVEN=0 S_MMPROJ_GIVEN=0
MODELSET="${MODELSET:-1}"
while [ $# -gt 0 ]; do
  case "$1" in
    --deep) DEEP=1; shift ;;
    --no-llm) NO_LLM=1; shift ;;
    --ocr-proxy) OCR_PROXY_GIVEN=1; shift ;;
    --no-ocr-proxy) OCR_PROXY_OFF=1; shift ;;
    --modelset) MODELSET="${2:?--modelset требует значение}"; MODELSET_GIVEN=1; shift 2 ;;
    --modelset=*) MODELSET="${1#*=}"; MODELSET_GIVEN=1; shift ;;
    --llm-model) LLM_MODEL="${2:?--llm-model требует значение}"; MODEL_GIVEN=1; shift 2 ;;
    --llm-model=*) LLM_MODEL="${1#*=}"; MODEL_GIVEN=1; shift ;;
    --llm-parallel) LLM_PARALLEL="${2:?--llm-parallel требует значение}"; PARALLEL_GIVEN=1; shift 2 ;;
    --llm-parallel=*) LLM_PARALLEL="${1#*=}"; PARALLEL_GIVEN=1; shift ;;
    --surya-model) SURYA_MODEL="${2:?--surya-model требует значение}"; S_MODEL_GIVEN=1; shift 2 ;;
    --surya-model=*) SURYA_MODEL="${1#*=}"; S_MODEL_GIVEN=1; shift ;;
    --surya-mmproj) SURYA_MMPROJ="${2:?--surya-mmproj требует значение}"; S_MMPROJ_GIVEN=1; shift 2 ;;
    --surya-mmproj=*) SURYA_MMPROJ="${1#*=}"; S_MMPROJ_GIVEN=1; shift ;;
    --surya-parallel) SURYA_PARALLEL="${2:?--surya-parallel требует значение}"; S_PARALLEL_GIVEN=1; shift 2 ;;
    --surya-parallel=*) SURYA_PARALLEL="${1#*=}"; S_PARALLEL_GIVEN=1; shift ;;
    *) die "неизвестный аргумент: $1 (см. ./services.sh help)" ;;
  esac
done
[ "${LLM_PARALLEL:-2}" -ge 1 ] 2>/dev/null || die "--llm-parallel: ожидается целое >= 1"
[ "${SURYA_PARALLEL:-8}" -ge 1 ] 2>/dev/null || die "--surya-parallel: ожидается целое >= 1"
if [ "$OCR_PROXY_GIVEN" = 1 ] && [ "$OCR_PROXY_OFF" = 1 ]; then
  die "--ocr-proxy и --no-ocr-proxy взаимоисключаемы"
fi
case "$MODELSET" in
  1) if [ "$OCR_PROXY_OFF" = 1 ]; then
       OCR_PROXY=0
       inf "OCR-прокси отключен (--no-ocr-proxy): marker напрямую к surya"
     else
       OCR_PROXY=1
       [ "$OCR_PROXY_GIVEN" = 1 ] && inf "--ocr-proxy: флаг не нужен — прокси поднимается по умолчанию в modelset 1"
     fi ;;
  2) if [ "$OCR_PROXY_GIVEN" = 1 ]; then
       warn "--ocr-proxy игнорируется в modelset 2 (прокси поднимается сам)"
     fi
     if [ "$OCR_PROXY_OFF" = 1 ]; then
       warn "--no-ocr-proxy игнорируется в modelset 2 (прокси нужен для block-OCR→chandra)"
     fi
     OCR_PROXY=0 ;;
  *) die "--modelset: ожидается 1 (surya+LLM) или 2 (surya+chandra+LLM), получено '$MODELSET'" ;;
esac
[ "$S_PARALLEL_GIVEN" = 1 ] && SURYA_CTX="$((SURYA_PARALLEL * 12288))"  # пересчёт под слоты
if [ "$S_MODEL_GIVEN" = 1 ] && [ "$S_MMPROJ_GIVEN" = 0 ]; then
  die "--surya-model требует также --surya-mmproj (mmproj обязателен для VLM)"
fi

case "$CMD" in
  start)
    echo "== Start (modelset=$MODELSET) =="
    start_surya || true
    if [ "$MODELSET" = 2 ]; then
      start_chandra || true
      OCR_PROXY_BLOCK_ENGINE="chandra" start_proxy || true
    elif [ "$OCR_PROXY" = 1 ]; then
      start_proxy || true
    fi
    if [ "$NO_LLM" = 1 ]; then inf "LLM пропущен (--no-llm)"; else start_llm || true; fi
    echo "Готово. Проверка: ./services.sh check"
    ;;
  stop)
    echo "== Stop =="
    stop_svc ocr-proxy "$PROXY_PID" "$OCR_PROXY_PORT"
    stop_svc chandra "$CHANDRA_PID" "$CHANDRA_PORT"
    stop_svc llm "$LLM_PID" "$LLM_PORT"
    if [ "$NO_LLM" = 1 ]; then inf "surya не трогаем (--no-llm)"; else stop_svc surya "$SURYA_PID" "$SURYA_PORT"; fi
    ;;
  restart)
    echo "== Restart =="
    NL=""; [ "$NO_LLM" = 1 ] && NL="--no-llm"
    LM=""; [ "$MODEL_GIVEN" = 1 ] && LM="--llm-model $LLM_MODEL"
    LP=""; [ "$PARALLEL_GIVEN" = 1 ] && LP="--llm-parallel $LLM_PARALLEL"
    SM=""; [ "$S_MODEL_GIVEN" = 1 ] && SM="--surya-model $SURYA_MODEL"
    SP=""; [ "$S_MMPROJ_GIVEN" = 1 ] && SP="--surya-mmproj $SURYA_MMPROJ"
    SL=""; [ "$S_PARALLEL_GIVEN" = 1 ] && SL="--surya-parallel $SURYA_PARALLEL"
    OP=""; [ "$OCR_PROXY_OFF" = 1 ] && OP="--no-ocr-proxy"
    MS=""; [ "$MODELSET_GIVEN" = 1 ] && MS="--modelset $MODELSET"
    "$0" stop $NL
    "$0" start $NL $LM $LP $SM $SP $SL $OP $MS
    ;;
  check|status) check ;;
  help|-h|--help|*)
    sed -n '2,51p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
    ;;
esac
