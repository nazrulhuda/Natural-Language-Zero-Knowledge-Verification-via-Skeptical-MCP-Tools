#!/bin/bash
# Mistral 24B 5-config orchestrator. Runs all configs A,B,C,D,A_STAR sequentially.
set -e
# Resolve this script's dir, then operate from the repo root (parent of eval/).
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR/.."

LOG_DIR="${LOG_DIR:-/tmp}"       # per-config run logs
LLM_MODEL="${LLM_MODEL:-mistralai/Mistral-Small-3.2-24B-Instruct-2506}"
PY="${PY:-python3}"           # interpreter with requests, redis, psycopg2 installed

set_config() {
  local cfg=$1
  echo "[orch] Setting CONFIG=$cfg in .env"
  sed -i "s|^CONFIG=.*|CONFIG=$cfg|" .env
  # Pin the model explicitly. The 2026 sweeps relied on whatever .env already held,
  # which is why no results file from them records which model ran.
  sed -i "s|^LLM_MODEL=.*|LLM_MODEL=$LLM_MODEL|" .env
  grep -E "^(CONFIG|LLM_MODEL)=" .env
}

restart_stack() {
  echo "[orch] Recreating backend + proverserver"
  docker-compose up -d --force-recreate backend proverserver 2>&1 | tail -3
  sleep 15
  curl -s -c /tmp/cookies_orch.txt -X POST http://127.0.0.1:5001/auth/signin \
    -H "Content-Type: application/json" \
    -d '{"username":"User1","password":"password123"}' --max-time 30 \
    | grep -q '"success": true' && echo "[orch] Auth OK" || echo "[orch] Auth FAIL"
}

run_config() {
  local cfg=$1
  set_config "$cfg"
  restart_stack
  if [ -f "results/results_${cfg}.json" ]; then
    echo "[orch] Removing stale results/results_${cfg}.json"
    rm "results/results_${cfg}.json"
  fi
  echo "[orch] Starting test_runner --config $cfg --runs 5"
  PYTHONUNBUFFERED=1 "$PY" "$SCRIPT_DIR/test_runner.py" --config "$cfg" --runs 5 \
    > "$LOG_DIR/mistral_${cfg}.log" 2>&1
  echo "[orch] test_runner --config $cfg exited rc=$?"
  if [ -f "results/results_${cfg}.json" ]; then
    cp "results/results_${cfg}.json" "results/results_mistral_${cfg}.json"
    echo "[orch] Saved results/results_mistral_${cfg}.json ($(stat -c %s results/results_mistral_${cfg}.json) bytes)"
  else
    echo "[orch] WARNING: results/results_${cfg}.json missing"
  fi
}

for cfg in C A B D A_STAR; do
  echo ""
  echo "============================================"
  echo "[orch] === CONFIG $cfg ==="
  echo "============================================"
  run_config "$cfg"
done

# Restore CONFIG=C
set_config C
restart_stack

echo ""
echo "[orch] === ALL DONE ==="
ls -la results/results_mistral_*.json
