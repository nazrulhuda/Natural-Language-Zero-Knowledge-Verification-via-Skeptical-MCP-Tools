#!/bin/bash
# verify_proof orchestrator.
# Qwen 32B x A,B,C,D (5 runs each)
# Mistral 24B x C,D (5 runs each)
# Llama 3 8B x C,D (5 runs each)
# Total: 4 + 2 + 2 = 8 result files, 280 queries.

set -e
# Resolve this script's dir, then operate from the repo root (parent of eval/).
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR/.."

LOG_DIR="${LOG_DIR:-/tmp}"       # per-config run logs
PY="${PY:-python3}"           # interpreter with requests, redis, psycopg2 installed
SUITE="$SCRIPT_DIR/test_suite_verify.json"

set_env() {
  local cfg=$1
  local model=$2
  sed -i "s|^CONFIG=.*|CONFIG=$cfg|" .env
  sed -i "s|^LLM_MODEL=.*|LLM_MODEL=$model|" .env
}

restart_stack() {
  docker-compose up -d --force-recreate backend proverserver 2>&1 | tail -2
  sleep 15
  curl -s -c /tmp/cookies_orch.txt -X POST http://127.0.0.1:5001/auth/signin \
    -H "Content-Type: application/json" \
    -d '{"username":"User1","password":"password123"}' --max-time 30 \
    | grep -q '"success": true' && echo "[orch] Auth OK" || echo "[orch] Auth FAIL"
}

run_combo() {
  local model_label=$1
  local model_id=$2
  local cfg=$3
  echo ""
  echo "============================================"
  echo "[orch] === $model_label / CONFIG $cfg ==="
  echo "============================================"
  set_env "$cfg" "$model_id"
  restart_stack
  if [ -f "results/results_${cfg}.json" ]; then
    rm "results/results_${cfg}.json"
  fi
  echo "[orch] Starting test_runner ($model_label $cfg)"
  PYTHONUNBUFFERED=1 "$PY" "$SCRIPT_DIR/test_runner.py" --config "$cfg" --runs 5 --test-suite "$SUITE" \
    > "$LOG_DIR/verify_${model_label}_${cfg}.log" 2>&1
  local rc=$?
  echo "[orch] test_runner exited rc=$rc"
  if [ -f "results/results_${cfg}.json" ]; then
    cp "results/results_${cfg}.json" "results/results_verify_${model_label}_${cfg}.json"
    echo "[orch] Saved results/results_verify_${model_label}_${cfg}.json ($(stat -c %s results/results_verify_${model_label}_${cfg}.json) bytes)"
  else
    echo "[orch] WARNING: results/results_${cfg}.json missing for $model_label/$cfg"
  fi
}

# Qwen 32B — A, B, C, D
for cfg in C A B D; do
  run_combo "qwen" "Qwen/Qwen3-32B" "$cfg"
done

# Mistral 24B — C, D
for cfg in C D; do
  run_combo "mistral" "mistralai/Mistral-Small-3.2-24B-Instruct-2506" "$cfg"
done

# Llama 3 8B — C, D
for cfg in C D; do
  run_combo "llama" "meta-llama/Meta-Llama-3-8B-Instruct" "$cfg"
done

# Restore baseline CONFIG=C and Qwen for clean dev state
set_env "C" "Qwen/Qwen3-32B"
restart_stack

echo ""
echo "[orch] === ALL DONE ==="
ls -la results/results_verify_*.json
