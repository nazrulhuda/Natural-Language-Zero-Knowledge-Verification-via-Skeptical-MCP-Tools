#!/usr/bin/env python3
"""Test runner for CONFIG ablation study.

Executes queries from test_suite.json against a running backend for a given CONFIG.
Resumable: loads existing results/results_{CONFIG}.json and skips already-done queries.
Sequential scenarios are checked per-scenario, not per-step (if partial, re-run whole scenario).

Usage:
    python test_runner.py --config C --runs 5
    python test_runner.py --config C --runs 1          # pilot
    python test_runner.py --config C --runs 5 --fresh  # delete existing results

Assumes docker-compose is running with the requested CONFIG and EVAL_MODE=true.
Does NOT restart docker or modify source files.
"""

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
import redis
import psycopg2


# ------------------------ Configuration ------------------------

BACKEND_URL = "http://localhost:5001"
# Anchor paths to this script's location so the runner works regardless of cwd.
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(_SCRIPT_DIR)
TEST_SUITE_PATH = os.path.join(_SCRIPT_DIR, "test_suite.json")
DEFAULT_RESULTS_DIR = os.path.join(_REPO_ROOT, "results")
QUERY_DELAY_SECONDS = 5  # between queries; conservative buffer for provider soft rate limits
RATE_LIMIT_WAIT_SECONDS = 60
MAX_RATE_LIMIT_RETRIES = 3
REQUEST_TIMEOUT_SECONDS = 450  # 7.5 min (backend's own timeout is 7 min)

# Local research-stack defaults; they match docker-compose.yml. Override through the
# environment if your Postgres differs. None of these are production credentials.
PG_HOST = os.getenv("PG_HOST", "localhost")
PG_PORT = int(os.getenv("PG_PORT", "5432"))
PG_USER = os.getenv("PG_USER", "mcp_user")
PG_PASSWORD = os.getenv("PG_PASSWORD", "mcp_password")
PG_DB = os.getenv("PG_DB", "mcpdb")

REDIS_HOST = "localhost"
REDIS_PORT = 6379

# The synthetic dataset user the suite signs in as (seeded by dataset_sync.py).
USERNAME = os.getenv("EVAL_USERNAME", "User1")
PASSWORD = os.getenv("EVAL_PASSWORD", "password123")

REDIS_TTL_SECONDS = 604800  # 7 days

# Placeholder in S1_13 that gets replaced with a real job_id at runtime
S1_13_PLACEHOLDER = "1749284736251"


# ------------------------ Utilities ------------------------

def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_json(path: str):
    with open(path) as f:
        return json.load(f)


def save_results(path: Path, results: list) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w") as f:
        json.dump(results, f, indent=2, default=str)
    tmp.replace(path)


def load_results(path: Path) -> list:
    if not path.exists():
        return []
    with open(path) as f:
        return json.load(f)


def build_done_sets(results: list):
    """Return (isolated_done, scenario_steps_done).

    isolated_done: set of (query_id, run) pairs.
    scenario_steps_done: dict keyed by (scenario_num, run) -> set of step query_ids.
    """
    isolated_done: set = set()
    scenario_steps_done: dict = {}
    for r in results:
        qid = r["query_id"]
        run = r["run"]
        scenario = r.get("scenario")
        if scenario is not None:
            scenario_steps_done.setdefault((scenario, run), set()).add(qid)
        else:
            isolated_done.add((qid, run))
    return isolated_done, scenario_steps_done


def scenario_fully_done(scenario_num: int, steps: list, run: int, scenario_steps_done: dict) -> bool:
    required = {s["id"] for s in steps}
    done = scenario_steps_done.get((scenario_num, run), set())
    return required.issubset(done)


def remove_partial_scenario(results: list, scenario_num: int, run: int) -> list:
    return [r for r in results if not (r.get("scenario") == scenario_num and r["run"] == run)]


# ------------------------ Auth & state setup ------------------------

def login(session: requests.Session) -> str:
    """Log in as User1, return user UUID. Raises on failure."""
    r = session.post(
        f"{BACKEND_URL}/auth/signin",
        json={"username": USERNAME, "password": PASSWORD},
        timeout=30,
    )
    r.raise_for_status()
    data = r.json()
    if not data.get("success"):
        raise RuntimeError(f"Login failed: {data}")
    return data["user"]["id"]


def pg_connect():
    return psycopg2.connect(
        host=PG_HOST, port=PG_PORT, user=PG_USER, password=PG_PASSWORD, dbname=PG_DB
    )


def get_user_hash() -> str:
    """Read User1.raw_hash from Postgres."""
    conn = pg_connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT raw_hash FROM dataset_users WHERE username = %s", (USERNAME,))
            row = cur.fetchone()
            return row[0] if row else ""
    finally:
        conn.close()


def set_user_hash(raw_hash: str) -> None:
    """Set User1.raw_hash in Postgres."""
    conn = pg_connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE dataset_users SET raw_hash = %s WHERE username = %s",
                (raw_hash, USERNAME),
            )
        conn.commit()
    finally:
        conn.close()


def _redis_client():
    return redis.Redis(host=REDIS_HOST, port=REDIS_PORT, decode_responses=True)


def inject_job_into_session(
    session_id: str,
    user_uuid: str,
    job_id: str,
    proof_type: str = "KS",
    status: str = "done",
) -> None:
    """Inject a job record + state into Redis for the given session_id.

    If status is done/completed/success, sets latest_completed_job_id as well.
    """
    r = _redis_client()
    ts = now_iso()
    job_data = {
        "job_id": job_id,
        "proof_type": proof_type,
        "user_hash": "test",
        "nonce": "[[0.0, 0.0, 0.0]]",
        "status": status,
        "created_at": ts,
        "updated_at": ts,
    }
    r.hset(f"session:{session_id}:jobs", job_id, json.dumps(job_data))

    state = {
        "latest_job_id": job_id,
        "latest_proof_type": proof_type,
        "latest_user_hash": "test",
        "owner_user_id": user_uuid,
        "updated_at": ts,
    }
    if status.lower() in {"done", "completed", "success"}:
        state["latest_completed_job_id"] = job_id
    r.hset(f"session:{session_id}:state", mapping=state)
    r.expire(f"session:{session_id}:jobs", REDIS_TTL_SECONDS)
    r.expire(f"session:{session_id}:state", REDIS_TTL_SECONDS)


def create_reference_completed_job(session: requests.Session, user_uuid: str) -> str:
    """Submit a KS proof via /get, poll until it's completed, return the real job_id.

    Used as the canonical completed job for tests needing has_completed_job=true.
    Retries up to 5 times in case of provider rate limits or the LLM not calling the tool.
    """
    last_error = None
    for attempt in range(1, 6):
        setup_session = f"setup_ref_{int(time.time() * 1000)}_a{attempt}"
        print(f"[setup] Attempt {attempt}/5: creating reference KS job in {setup_session}...")
        try:
            r = session.post(
                f"{BACKEND_URL}/get",
                json={"msg": "Prove my data in KS", "session_id": setup_session},
                timeout=REQUEST_TIMEOUT_SECONDS,
            )
            data = r.json() if r.status_code < 500 else {}
        except Exception as e:
            last_error = f"request failed: {e}"
            print(f"[setup]   {last_error}; sleeping 60s")
            time.sleep(60)
            continue

        # Try to extract job_id from tool_calls
        job_id = None
        for tc in data.get("tool_calls", []) if isinstance(data, dict) else []:
            resp_text = tc.get("response", "")
            try:
                payload = json.loads(resp_text)
                if isinstance(payload, dict) and "job_id" in payload:
                    job_id = str(payload["job_id"])
                    break
            except Exception:
                continue

        # Fallback: read from Redis (state may update slightly after response)
        if not job_id:
            for _ in range(3):
                time.sleep(2)
                rc = _redis_client()
                state = rc.hgetall(f"session:{setup_session}:state")
                job_id = state.get("latest_job_id")
                if job_id:
                    break

        if not job_id:
            last_error = "no job_id captured (LLM may have skipped tool call under rate limit)"
            print(f"[setup]   {last_error}; sleeping 60s before retry")
            time.sleep(60)
            continue

        # Poll COSMeTIC DIRECTLY (host-exposed port 5013) for completion — Redis only
        # updates when check_status is called, which would consume provider quota.
        print(f"[setup]   submitted job {job_id}; polling COSMeTIC for completion...")
        for poll in range(200):
            time.sleep(5)
            try:
                sr = requests.get(f"http://localhost:5013/jobs/{job_id}", timeout=10)
                if sr.status_code == 200:
                    jd = sr.json()
                    if str(jd.get("status", "")).lower() in {"done", "completed", "success"}:
                        # Write latest_completed_job_id to Redis ourselves
                        rc = _redis_client()
                        rc.hset(f"session:{setup_session}:state", "latest_completed_job_id", job_id)
                        rc.expire(f"session:{setup_session}:state", REDIS_TTL_SECONDS)
                        print(f"[setup]   reference job {job_id} is done.")
                        return job_id
            except Exception:
                pass
            if poll % 12 == 0 and poll > 0:
                elapsed = poll * 5
                print(f"[setup] Still waiting... ({elapsed}s elapsed)")
        # HARD FAIL (was: warn and return anyway). If the reference job never reaches
        # "done", every has_completed_job/has_jobs fixture is false against the live API:
        # downloads return "Job is not done yet", status queries return queued, and the
        # run is not comparable to runs whose reference job completed. The May 2026
        # Mistral / Qwen2.5-72B / Qwen3-235B sweeps all proceeded past this point on the
        # old warning path (~17 min poll timeout per run), which invalidated their
        # per-source status/download numbers. See README, Deviations and known issues.
        raise RuntimeError(
            f"Reference job {job_id} did not reach status=done within "
            f"{200 * 5}s. Aborting: fixtures would be false for this run. "
            f"Check the COSMeTIC prover on port 5013."
        )

    raise RuntimeError(f"Failed to create reference job after 5 attempts. Last error: {last_error}")


def assert_reference_job_done(job_id: str) -> None:
    """Re-check the reference job's LIVE status immediately before a run starts.

    Creation-time completion is not sufficient on its own: a job can be created in one
    run and the prover restarted/cleared before a later run of the same sweep. Called at
    the top of every run so a drifted fixture aborts instead of silently producing
    uncomparable results.
    """
    if not job_id:
        return  # Config A creates no reference job by design
    try:
        r = requests.get(f"http://localhost:5013/jobs/{job_id}", timeout=15)
        status = str(r.json().get("status", "")).lower() if r.status_code == 200 else f"http_{r.status_code}"
    except Exception as e:
        raise RuntimeError(f"Could not verify reference job {job_id} before this run: {e}")
    if status not in {"done", "completed", "success"}:
        raise RuntimeError(
            f"Reference job {job_id} is '{status}', not done, at the start of this run. "
            f"Aborting: has_completed_job/has_jobs fixtures would be false."
        )


def setup_user_state_for_query(
    query: dict,
    session_id: str,
    user_uuid: str,
    reference_job_id: str,
) -> str | None:
    """Inject Redis state based on user_state. Returns job_id_substitute if runtime_substitution=true."""
    if reference_job_id is None:
        return None
    user_state = query.get("user_state", {}) or {}

    # S1_13 runtime_substitution: query text has a placeholder job_id we need to substitute
    if query.get("runtime_substitution"):
        inject_job_into_session(session_id, user_uuid, reference_job_id, "KS", "done")
        return reference_job_id

    has_completed = user_state.get("has_completed_job")
    has_jobs = user_state.get("has_jobs")

    if has_completed is True:
        inject_job_into_session(session_id, user_uuid, reference_job_id, "KS", "done")
    elif has_jobs is True and has_completed is False:
        # Jobs exist but NONE completed (S2_04)
        fake_id = f"fake_{int(time.time() * 1_000_000)}"
        inject_job_into_session(session_id, user_uuid, fake_id, "KS", "submitted")
    elif has_jobs is True:
        # has_jobs=true without completed spec — use reference (which is completed,
        # but that's fine for status-check tests)
        inject_job_into_session(session_id, user_uuid, reference_job_id, "KS", "done")

    return None


# ------------------------ Session ID generation ------------------------

def gen_isolated_session_id(config: str, query_id: str, run: int) -> str:
    return f"test_{config}_{query_id}_run{run}_{int(time.time() * 1000)}"


def gen_scenario_session_id(config: str, scenario_num: int, run: int) -> str:
    return f"test_{config}_S6_SC{scenario_num}_run{run}_{int(time.time() * 1000)}"


# ------------------------ Query execution ------------------------

def execute_query(
    session: requests.Session,
    msg: str,
    session_id: str,
    retries: int = 0,
) -> tuple[dict | None, float, str | None]:
    """POST to /get with retries on 429. Returns (result_dict, duration_seconds, error)."""
    start = time.time()
    try:
        r = session.post(
            f"{BACKEND_URL}/get",
            json={"msg": msg, "session_id": session_id},
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
        duration = time.time() - start
    except requests.Timeout:
        return None, time.time() - start, "timeout"
    except Exception as e:
        return None, time.time() - start, f"request_error: {e}"

    if r.status_code == 429:
        if retries < MAX_RATE_LIMIT_RETRIES:
            print(f"    [rate_limit] waiting {RATE_LIMIT_WAIT_SECONDS}s then retrying ({retries+1}/{MAX_RATE_LIMIT_RETRIES})")
            time.sleep(RATE_LIMIT_WAIT_SECONDS)
            return execute_query(session, msg, session_id, retries + 1)
        return None, duration, f"rate_limit_exceeded after {retries} retries"

    try:
        data = r.json()
    except Exception as e:
        return None, duration, f"bad_json: {e} | body: {r.text[:200]}"

    # The backend sometimes returns {"error": "..."} with HTTP 200 (see /get)
    if isinstance(data, dict) and "error" in data and "response" not in data:
        err_msg = str(data.get("error", ""))
        # Detect wrapped rate limit from the provider — retry with long cooldown
        err_lower = err_msg.lower()
        if ("429" in err_msg
                or "rate_limit" in err_lower
                or "rate limit" in err_lower
                or "too_many_requests" in err_lower
                or "rate limited" in err_lower
                or "throttled" in err_lower):
            if retries < MAX_RATE_LIMIT_RETRIES:
                print(f"    [soft_rate_limit] waiting {RATE_LIMIT_WAIT_SECONDS}s then retrying ({retries+1}/{MAX_RATE_LIMIT_RETRIES})")
                time.sleep(RATE_LIMIT_WAIT_SECONDS)
                return execute_query(session, msg, session_id, retries + 1)
        return None, duration, f"backend_error: {err_msg}"

    return data, duration, None


def make_result_record(
    query: dict,
    config: str,
    run: int,
    session_id: str,
    query_text: str,
    result_data: dict | None,
    duration: float,
    error: str | None,
) -> dict:
    rec = {
        "query_id": query["id"],
        "config": config,
        # Provenance: the 2026 sweeps did not record these, so no results file identifies
        # the model or provider that produced it. Captured per record from here on.
        "llm_model": os.getenv("LLM_MODEL", "UNSET"),
        "llm_provider_base_url": os.getenv("LLM_BASE_URL", "https://api.deepinfra.com/v1/openai"),
        "run": run,
        "session_id": session_id,
        "query_text": query_text,
        "expected_tool": query.get("expected_tool"),
        "mode": query.get("mode"),
        "source": query.get("source"),
        "http_status": None if error else 200,
        "error": error,
        "timestamp": now_iso(),
        "duration_seconds": round(duration, 2),
    }
    if query.get("scenario") is not None:
        rec["scenario"] = query["scenario"]
        rec["step"] = query.get("step")
    if result_data:
        rec["response_text"] = result_data.get("response", "")
        rec["backed_by_cosmetic"] = result_data.get("backed_by_cosmetic")
        rec["tool_calls"] = result_data.get("tool_calls", [])
    else:
        rec["response_text"] = ""
        rec["backed_by_cosmetic"] = None
        rec["tool_calls"] = []
    return rec


def format_progress(
    run_num: int,
    total_runs: int,
    idx: int,
    total: int,
    query_id: str,
    result: dict,
) -> str:
    if result.get("error"):
        return f"[Run {run_num}/{total_runs}] [{idx}/{total}] {query_id} | ERROR: {result['error']}"
    tool = "NONE"
    if result.get("tool_calls"):
        tool = result["tool_calls"][0].get("tool_name", "NONE")
    return f"[Run {run_num}/{total_runs}] [{idx}/{total}] {query_id} | tool: {tool} | {result['duration_seconds']}s | ✓"


# ------------------------ Main flow ------------------------

def categorize(cases: list):
    """Split test cases into (isolated, [(scenario_num, [steps])...]) sorted by scenario number.

    Orphan sequential queries (no 'scenario' field, e.g. S3a standalone) are skipped.
    """
    isolated = [c for c in cases if c.get("mode") == "isolated"]
    scenarios_map: dict = {}
    for c in cases:
        if c.get("mode") == "sequential" and "scenario" in c:
            scenarios_map.setdefault(c["scenario"], []).append(c)
    for steps in scenarios_map.values():
        steps.sort(key=lambda x: x.get("step", 0))
    scenarios = sorted(scenarios_map.items())
    return isolated, scenarios


def run_isolated_query(
    query: dict,
    config: str,
    run: int,
    auth_session: requests.Session,
    user_uuid: str,
    reference_job_id: str,
) -> dict:
    """Execute a single isolated query. Handles temporary DB changes (has_hash=false) safely."""
    query_id = query["id"]
    session_id = gen_isolated_session_id(config, query_id, run)
    query_text = query["query"]
    user_state = query.get("user_state", {}) or {}

    # Pick transport: authenticated session if logged_in, else a fresh anon session
    if user_state.get("logged_in") is False:
        http_session = requests.Session()
    else:
        http_session = auth_session

    # Temporarily null raw_hash for has_hash=false tests
    original_hash = None
    if user_state.get("has_hash") is False:
        original_hash = get_user_hash()
        set_user_hash("")

    try:
        job_id_subst = setup_user_state_for_query(query, session_id, user_uuid, reference_job_id)
        if job_id_subst:
            query_text = query_text.replace(S1_13_PLACEHOLDER, job_id_subst)

        result_data, duration, error = execute_query(http_session, query_text, session_id)
        rec = make_result_record(
            query, config, run, session_id, query_text, result_data, duration, error
        )
        return rec
    finally:
        if original_hash is not None:
            try:
                set_user_hash(original_hash)
            except Exception as e:
                print(f"    [warn] failed to restore hash: {e}")


def run_sequential_scenario(
    scenario_num: int,
    steps: list,
    config: str,
    run: int,
    auth_session: requests.Session,
    user_uuid: str,
) -> list:
    """Execute all steps of one sequential scenario sharing one session_id."""
    session_id = gen_scenario_session_id(config, scenario_num, run)
    print(f"  [Scenario {scenario_num} run {run}] session_id={session_id}")
    records = []
    for j, step in enumerate(steps, start=1):
        # Sequential scenarios don't need Redis injection — steps build state naturally
        # via prior step's tool calls. (All scenarios start with a prove_my_data.)
        # But we still honor any has_hash=false if ever present (none in current suite).
        query_text = step["query"]

        # Step 1 may set user_state; subsequent steps inherit session state
        user_state = step.get("user_state", {}) or {}
        original_hash = None
        if user_state.get("has_hash") is False:
            original_hash = get_user_hash()
            set_user_hash("")

        try:
            result_data, duration, error = execute_query(auth_session, query_text, session_id)
            rec = make_result_record(
                step, config, run, session_id, query_text, result_data, duration, error
            )
            records.append(rec)
            print(f"    " + format_progress(run, 0, j, len(steps), step["id"], rec).split("] ", 1)[-1])
        finally:
            if original_hash is not None:
                try:
                    set_user_hash(original_hash)
                except Exception:
                    pass

        # Intra-scenario delay (same as between queries)
        if j < len(steps):
            time.sleep(QUERY_DELAY_SECONDS)

    return records


def parse_args():
    p = argparse.ArgumentParser(description="Ablation study test runner")
    p.add_argument("--config", required=True, choices=["A", "B", "C", "D", "A_STAR", "a", "b", "c", "d", "a_star"])
    p.add_argument("--runs", type=int, default=5)
    p.add_argument("--fresh", action="store_true", help="Delete existing results and start over")
    p.add_argument("--resume", action="store_true", help="Explicit resume flag (default behavior anyway)")
    p.add_argument("--retry-failed", action="store_true", help="Re-run queries that previously failed with errors")
    p.add_argument("--results-dir", default=DEFAULT_RESULTS_DIR, help="Directory for results files")
    p.add_argument("--test-suite", default=TEST_SUITE_PATH, help="Path to test_suite JSON")
    return p.parse_args()


def main():
    args = parse_args()
    config = args.config.upper()
    num_runs = args.runs
    results_file = Path(args.results_dir) / f"results_{config}.json"
    results_file.parent.mkdir(parents=True, exist_ok=True)

    if args.fresh and results_file.exists():
        print(f"[--fresh] Deleting {results_file}")
        results_file.unlink()

    if not os.getenv("LLM_MODEL"):
        print("ERROR: LLM_MODEL is not set. Refusing to run: results would not record which "
              "model produced them. Export LLM_MODEL (e.g. Qwen/Qwen3-32B) and re-run.")
        sys.exit(2)
    print(f"Model: {os.getenv('LLM_MODEL')} via "
          f"{os.getenv('LLM_BASE_URL', 'https://api.deepinfra.com/v1/openai')}")

    results = load_results(results_file)
    if args.retry_failed:
        before = len(results)
        results = [r for r in results if not r.get("error")]
        after = len(results)
        if before != after:
            print(f"[--retry-failed] Removed {before - after} failed records for re-run")
            save_results(results_file, results)
    isolated_done, scenario_steps_done = build_done_sets(results)

    # Load and categorize test suite
    cases = [c for c in load_json(args.test_suite) if "_comment" not in c]
    isolated, scenarios = categorize(cases)

    total_per_run = len(isolated) + sum(len(steps) for _, steps in scenarios)
    total_queries = total_per_run * num_runs

    print(f"Config: {config} | Runs: {num_runs} | Queries per run: {total_per_run} | "
          f"Total queries: {total_queries} | Already done: {len(results)} | "
          f"Remaining: {total_queries - len(results)}")

    # Verify backend is up
    try:
        r = requests.get(BACKEND_URL, timeout=10)
        if r.status_code != 200:
            print(f"WARNING: backend returned {r.status_code}")
    except Exception as e:
        print(f"ERROR: backend not reachable at {BACKEND_URL}: {e}")
        sys.exit(1)

    # Login and create reference completed job
    auth_session = requests.Session()
    user_uuid = login(auth_session)
    print(f"Logged in as {USERNAME} (uuid: {user_uuid[:8]}...)")

    if config == "A":
        reference_job_id = None
        print(f"[setup] Config A: skipping reference job creation (identity disabled, auth-dependent tools will fail as expected)")
    else:
        reference_job_id = create_reference_completed_job(auth_session, user_uuid)

    start_time = time.time()
    success = 0
    failed = 0

    for run in range(1, num_runs + 1):
        print(f"\n===== Run {run}/{num_runs} =====")
        # Guard against fixture drift mid-sweep (see assert_reference_job_done).
        assert_reference_job_done(reference_job_id)

        # --- Isolated queries ---
        for i, query in enumerate(isolated, start=1):
            query_id = query["id"]
            if (query_id, run) in isolated_done:
                continue
            try:
                rec = run_isolated_query(query, config, run, auth_session, user_uuid, reference_job_id)
            except Exception as e:
                # Catch everything; never crash
                rec = make_result_record(
                    query, config, run,
                    gen_isolated_session_id(config, query_id, run),
                    query.get("query", ""), None, 0.0, f"runner_exception: {e}",
                )
            results.append(rec)
            save_results(results_file, results)
            isolated_done.add((query_id, run))

            if rec.get("error"):
                failed += 1
            else:
                success += 1
            print(format_progress(run, num_runs, i, len(isolated), query_id, rec))
            time.sleep(QUERY_DELAY_SECONDS)

        # --- Sequential scenarios ---
        for scenario_num, steps in scenarios:
            if scenario_fully_done(scenario_num, steps, run, scenario_steps_done):
                continue
            # Partial scenario — delete existing partial and re-run whole thing
            results = remove_partial_scenario(results, scenario_num, run)
            scenario_steps_done.pop((scenario_num, run), None)
            save_results(results_file, results)

            try:
                new_records = run_sequential_scenario(
                    scenario_num, steps, config, run, auth_session, user_uuid
                )
            except Exception as e:
                new_records = []
                print(f"  [Scenario {scenario_num}] runner_exception: {e}")

            results.extend(new_records)
            save_results(results_file, results)
            done_set = scenario_steps_done.setdefault((scenario_num, run), set())
            for rec in new_records:
                done_set.add(rec["query_id"])
                if rec.get("error"):
                    failed += 1
                else:
                    success += 1

            time.sleep(QUERY_DELAY_SECONDS)

    elapsed = time.time() - start_time
    h, rem = divmod(int(elapsed), 3600)
    m, s = divmod(rem, 60)
    print(f"\nDone. Total records: {len(results)} | Success: {success} | Failed: {failed} | "
          f"Time: {h}h {m}m {s}s")


if __name__ == "__main__":
    main()
