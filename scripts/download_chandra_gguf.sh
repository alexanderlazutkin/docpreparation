#!/usr/bin/env bash
# download_chandra_gguf.sh — скачивание Chandra OCR 2 GGUF (модель + mmproj)
# с Hugging Face с проверкой целостности по LFS sha256 из HF API.
#
# Зачем отдельный скрипт: community-репозитории не публикуют SHA256SUMS —
# единственный источник эталонных хэшей — HF API (?blobs=true, поле lfs.sha256).
# Скачивание — официальным CLI `hf` (huggingface_hub из .venv), он докачивает
# после обрыва; после скачивания хэш сверяется явно.
#
# Usage:
#   scripts/download_chandra_gguf.sh                 # дефолт: mradermacher Q8_0 + mmproj-Q8_0
#   scripts/download_chandra_gguf.sh --dry-run       # только метаданные (размеры+sha), без скачивания
#   scripts/download_chandra_gguf.sh --mmproj-f16    # дополнительно mmproj-f16 (свап при странностях vision)
#   scripts/download_chandra_gguf.sh --file ИМЯ      # свой набор файлов (повторяемый; заменяет дефолт)
#   scripts/download_chandra_gguf.sh --repo РЕПО     # другой репо (напр. prithivMLmods/chandra-ocr-2-GGUF)
#   scripts/download_chandra_gguf.sh --force         # перекачать даже при совпавшем хэше
#
# Дефолты (переопределяются): REPO=mradermacher/chandra-ocr-2-GGUF,
# DEST=/media/llm/Models/OCR/chandra-ocr-2-gguf (см. HANDOFF «Следующие шаги» п.4).

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

REPO="mradermacher/chandra-ocr-2-GGUF"
DEST="/media/llm/Models/OCR/chandra-ocr-2-gguf"
DRY_RUN=0 FORCE=0
FILES=()
EXTRA=()

while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run)    DRY_RUN=1; shift ;;
    --force)      FORCE=1; shift ;;
    --repo)       REPO="${2:?--repo требует значение}"; shift 2 ;;
    --repo=*)     REPO="${1#*=}"; shift ;;
    --dest)       DEST="${2:?--dest требует значение}"; shift 2 ;;
    --dest=*)     DEST="${1#*=}"; shift ;;
    --file)       FILES+=("${2:?--file требует значение}"); shift 2 ;;
    --file=*)     FILES+=("${1#*=}"); shift ;;
    --mmproj-f16) EXTRA+=("chandra-ocr-2.mmproj-f16.gguf"); shift ;;
    *) printf 'Ошибка: неизвестный аргумент: %s\n' "$1" >&2; exit 1 ;;
  esac
done

# Дефолтный набор: модель Q8_0 + mmproj-Q8_0 (выбор 2026-10-21, HANDOFF п.4)
[ ${#FILES[@]} -gt 0 ] || FILES=("chandra-ocr-2.Q8_0.gguf" "chandra-ocr-2.mmproj-Q8_0.gguf")
ALL_FILES=("${FILES[@]}" ${EXTRA[@]+"${EXTRA[@]}"})

ok()   { printf '  [OK]   %s\n' "$*"; }
bad()  { printf '  [FAIL] %s\n' "$*"; }
inf()  { printf '  [--]   %s\n' "$*"; }
die()  { printf 'Ошибка: %s\n' "$*" >&2; exit 1; }

# --- HF CLI: из .venv проекта, иначе из PATH -------------------------------
HF_BIN=""
[ -x "$ROOT/.venv/bin/hf" ] && HF_BIN="$ROOT/.venv/bin/hf"
[ -n "$HF_BIN" ] || HF_BIN="$(command -v hf || true)"
[ -n "$HF_BIN" ] || die "hf CLI не найден (.venv/bin/hf или PATH; uv sync)"

# --- Метаданные: имя -> размер + sha256 (HF API, LFS) -----------------------
inf "репо: $REPO"
inf "метаданные HF API (lfs.sha256)…"
META="$("$HF_BIN" api "models/$REPO?blobs=true" 2>/dev/null \
  || curl -sf -m 60 "https://huggingface.co/api/models/$REPO?blobs=true")" \
  || die "не удалось получить метаданные $REPO (сеть? rate limit?)"

META_TSV="$(printf '%s' "$META" | python3 -c '
import json, sys
want = {a.strip() for a in sys.argv[1:]}
seen = {}
try:
    d = json.load(sys.stdin)
    for s in d.get("siblings", []):
        name = s.get("rfilename", "")
        if name not in want:
            continue
        lfs = s.get("lfs") or {}
        sha = lfs.get("sha256") or ""
        seen[name] = name + "\t" + str(s.get("size", 0)) + "\t" + sha
except Exception as e:
    print(f"PARSE_ERROR {e}", file=sys.stderr); sys.exit(1)
for n in want:
    if n in seen:
        print(seen[n])
    else:
        print(f"{n}\t-1\t-", file=sys.stderr)
' "${ALL_FILES[@]}")" || die "парсинг метаданных не удался"
[ -n "$META_TSV" ] || die "ни один из запрошенных файлов не найден в $REPO"

# --- Вывод таблицы + проверка наличия sha -----------------------------------
NEED_BYTES=0 MISSING=0
printf '  %-34s %10s  %s\n' "файл" "размер" "sha256 (первые 12)"
while IFS=$'\t' read -r fname fsize fsha; do
  if [ "$fsize" = "-1" ] || [ -z "$fsha" ]; then
    bad "$fname: нет в репо или не LFS (sha отсутствует)"
    MISSING=1; continue
  fi
  printf '  %-34s %8s ГБ  %s…\n' "$fname" "$(LC_ALL=C awk "BEGIN{printf \"%.2f\", $fsize/1e9}")" "${fsha:0:12}"
  NEED_BYTES=$((NEED_BYTES + fsize))
done <<< "$META_TSV"
[ "$MISSING" = 0 ] || die "часть файлов недоступна — проверь имена (--dry-run в помощь)"

# --- Свободное место ---------------------------------------------------------
mkdir -p "$DEST"
AVAIL="$(df -B1 --output=avail "$DEST" | tail -1)"
if [ "$AVAIL" -lt "$((NEED_BYTES + 2*1024*1024*1024))" ]; then
  die "мало места: нужно $((NEED_BYTES / 1024 / 1024)) МБ + запас, свободно $((AVAIL / 1024 / 1024)) МБ в $DEST"
fi

if [ "$DRY_RUN" = 1 ]; then inf "dry-run: скачивание пропущено"; exit 0; fi

# --- Скачивание + проверка sha256 --------------------------------------------
FAIL=0
while IFS=$'\t' read -r fname fsize fsha; do
  TARGET="$DEST/$fname"
  # уже скачан и хэш совпадает -> скип
  if [ -f "$TARGET" ] && [ "$FORCE" = 0 ]; then
    ACTUAL="$(sha256sum "$TARGET" | cut -d' ' -f1)"
    if [ "$ACTUAL" = "$fsha" ]; then ok "$fname: уже на месте, sha совпал"; continue; fi
    inf "$fname: хэш не совпал (или недокачан) — перекачиваю"
  fi
  inf "$fname: качаю через hf CLI (resume поддерживается)…"
  if ! "$HF_BIN" download "$REPO" "$fname" --local-dir "$DEST"; then
    bad "$fname: скачивание не удалось"; FAIL=1; continue
  fi
  ACTUAL="$(sha256sum "$TARGET" | cut -d' ' -f1)"
  if [ "$ACTUAL" = "$fsha" ]; then
    ok "$fname: sha256 совпал ($((fsize / 1024 / 1024)) МБ)"
  else
    bad "$fname: sha256 НЕ совпал (ожидался $fsha, получен $ACTUAL)"; FAIL=1
  fi
done <<< "$META_TSV"

echo
inf "итог в $DEST:"
ls -lh "$DEST" | awk 'NR>1 {printf "  %8s  %s\n", $5, $9}'
[ "$FAIL" = 0 ] && echo "ГОТОВО: все файлы проверены по sha256" || die "есть FAIL — см. выше"
