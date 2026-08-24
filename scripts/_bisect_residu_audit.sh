#!/usr/bin/env bash
# Pelacak residu audit_logs: jalankan tiap gate runtime satu-satu dan ukur
# selisih jumlah dokumen audit_logs sebelum/sesudah. Dipakai sekali untuk
# menemukan pemilik residu +2 pada INV-GATE-01 (bukan bagian dari gate.sh).
set -uo pipefail
cd /app
export $(grep -v '^#' backend/.env | xargs) >/dev/null 2>&1

count() {
  python - <<'PY'
import os
from pymongo import MongoClient
db = MongoClient(os.environ["MONGO_URL"])[os.environ.get("DB_NAME", "test_database")]
print(db.audit_logs.count_documents({}))
PY
}

# Ambil daftar perintah runtime dari gate.sh (blok AUTH_READY, baris 499..782).
mapfile -t CMDS < <(sed -n '499,782p' scripts/gate.sh \
  | grep -oP '^\s*run_gate "\K[^"]+(?=" ")' )
mapfile -t RAW < <(sed -n '499,782p' scripts/gate.sh | grep -P '^\s*run_gate ')

PREV=$(count)
echo "AWAL audit_logs=$PREV"
i=0
for line in "${RAW[@]}"; do
  label=$(echo "$line" | grep -oP 'run_gate "\K[^"]+' | head -1)
  cmd=$(python - "$line" <<'PY'
import sys, shlex
line = sys.argv[1].strip()
assert line.startswith("run_gate ")
parts = shlex.split(line)
print(parts[2])
PY
)
  out=$(eval "$cmd" 2>&1)
  rc=$?
  NOW=$(count)
  D=$((NOW - PREV))
  printf 'delta=%+d rc=%s :: %s\n' "$D" "$rc" "$label"
  if [ "$D" -ne 0 ]; then
    echo "  ^^^ PENYUMBANG RESIDU: $cmd"
  fi
  PREV=$NOW
  i=$((i+1))
done
echo "SELESAI audit_logs=$PREV"
