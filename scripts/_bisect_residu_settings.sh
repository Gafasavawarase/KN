#!/usr/bin/env bash
# Pelacak residu `system_settings` di blok POC FASE (gate.sh 807..848).
set -uo pipefail
cd /app
export $(grep -v '^#' backend/.env | xargs) >/dev/null 2>&1

count() {
  python - <<'PY'
import os
from pymongo import MongoClient
db = MongoClient(os.environ["MONGO_URL"])[os.environ.get("DB_NAME", "test_database")]
print(db.system_settings.count_documents({}))
PY
}

mapfile -t RAW < <(sed -n '807,848p' scripts/gate.sh | grep -P '^\s*run_gate ')
PREV=$(count)
echo "AWAL system_settings=$PREV"
for line in "${RAW[@]}"; do
  label=$(echo "$line" | grep -oP 'run_gate "\K[^"]+' | head -1)
  cmd=$(python - "$line" <<'PY'
import sys, shlex
print(shlex.split(sys.argv[1].strip())[2])
PY
)
  eval "$cmd" >/dev/null 2>&1
  rc=$?
  NOW=$(count)
  printf 'delta=%+d rc=%s :: %s\n' "$((NOW-PREV))" "$rc" "$label"
  PREV=$NOW
done
echo "SELESAI system_settings=$PREV"
