#!/bin/bash
# Llama 3 8B 5-config orchestrator.
# Assumes Config C is currently running (PID watched externally).
# After C finishes, runs A, B, D, A_STAR sequentially.
# Each config: update .env, recreate backend+proverserver, wait, run, cp result.

set -e
# Resolve this script's dir, then operate from the repo root (parent of eval/).
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR/.."

LOG_DIR="${LOG_DIR:-/tmp}"       # per-config run logs
LLM_MODEL="${LLM_MODEL:-meta-llama/Meta-Llama-3-8B-Instruct}"
PY="${PY:-python3}"           # interpreter with requests, redis, psycopg2 installed

wait_for_pid() {
  local pid=$1
  local label=$2
  echo "[orch] Waiting for $label (PID $pid) to finish..."
  while kill -0 "$pid" 2>/dev/null; do
    sleep 30
  done
  echo "[orch] $label finished."
}

set_config() {
  local cfg=$1
  echo "[orch] Setting CONFIG=$cfg in .env"
  sed -i "s|^CONFIG=.*|CONFIG=$cfg|" .env
  # Pin the model explicitly. The 2026 sweeps relied on whatever .env already held,
  # which is why no results file from them records which model ran.
  sed -i "s|^LLM_MODEL=.*|LLM_MODEL=$LLM_MODEL|" .env
  grep -E "^(CONFIG|LLM_MODEL)=" .env
  grep "^CONFIG=" .env
}

restart_stack() {
  local cfg=$1
  echo "[orch] Recreating backend + proverserver for CONFIG=$cfg"
  docker-compose up -d --force-recreate backend proverserver 2>&1 | tail -5
  echo "[orch] Waiting 15s for stack to come up..."
  sleep 15
  # Smoke check
  curl -s -c /tmp/cookies_orch.txt -X POST http://127.0.0.1:5001/auth/signin \
    -H "Content-Type: application/json" \
    -d '{"username":"User1","password":"password123"}' --max-time 30 \
    | grep -q '"success": true' && echo "[orch] Auth OK" || echo "[orch] Auth FAIL"
}

run_config() {
  local cfg=$1
  set_config "$cfg"
  restart_stack "$cfg"
  # Delete any leftover results/results_${cfg}.json so test_runner starts fresh
  if [ -f "results/results_${cfg}.json" ]; then
    echo "[orch] Removing stale results/results_${cfg}.json"
    rm "results/results_${cfg}.json"
  fi
  echo "[orch] Starting test_runner --config $cfg --runs 5"
  PYTHONUNBUFFERED=1 "$PY" "$SCRIPT_DIR/test_runner.py" --config "$cfg" --runs 5 \
    > "$LOG_DIR/llama_${cfg}.log" 2>&1
  local rc=$?
  echo "[orch] test_runner --config $cfg exited rc=$rc"
  if [ -f "results/results_${cfg}.json" ]; then
    cp "results/results_${cfg}.json" "results/results_llama_${cfg}.json"
    local size=$(stat -c %s "results/results_llama_${cfg}.json")
    echo "[orch] Saved results/results_llama_${cfg}.json (${size} bytes)"
  else
    echo "[orch] WARNING: results/results_${cfg}.json missing"
  fi
}

# 1) Wait for current Config C run
C_PID=913669
wait_for_pid "$C_PID" "Config C"

# 2) Save C results
if [ -f results/results_C.json ]; then
  cp results/results_C.json results/results_llama_C.json
  echo "[orch] Saved results/results_llama_C.json ($(stat -c %s results/results_llama_C.json) bytes)"
fi

# 3) Run remaining configs in order
for cfg in A B D A_STAR; do
  echo ""
  echo "============================================"
  echo "[orch] === CONFIG $cfg ==="
  echo "============================================"
  run_config "$cfg"
done

# 4) Restore CONFIG=C in .env so subsequent dev work isn't broken
set_config C
restart_stack C

echo ""
echo "[orch] === ALL DONE ==="
ls -la results/results_llama_*.json
