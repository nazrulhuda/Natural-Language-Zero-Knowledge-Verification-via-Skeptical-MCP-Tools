## MCP Prover Chatbot – Full Architecture

This document describes the complete architecture of the MCP Prover Chatbot as it exists in this repository. It extends the information from `README.md` with code-verified detail for every subsystem.

---

## 1. High-Level System Overview

The system is a **dockerized proof assistant** that lets users:

- Chat with an LLM assistant (any OpenAI-compatible endpoint; DeepInfra by default) about proof jobs. The default model is Qwen3-32B; the model is env-driven via `LLM_MODEL` and has also been exercised with Llama 3 8B and Mistral-Small-3.2-24B for cross-model evaluation.
- Submit proofs against COSMeTIC prover APIs.
- Check job status.
- Download proof artifacts.
- **Verify that submitted proofs are cryptographically valid** (calls `/verify-job/` on the corresponding COSMeTIC port and surfaces the top-level `ok` result).
- Use dataset-backed accounts so users can say “my hash / my data” rather than pasting raw hashes.

It is composed of four main runtime services:

- **Backend (`backend`, container `mcp-backend`, port 5001)**  
  Flask web app + LangGraph/LangChain agent + MCP client. Handles HTTP, auth, chat, and file download. It is mostly a thin proxy, but it **does** perform one security-critical Redis write: binding a `session_id` to an `owner_user_id` (to prevent cross-user session access).

- **Prover MCP server (`proverserver`, container `mcp-prover-server`, port 8003)**  
  FastMCP HTTP server exposing tools for proof submission, status checks, downloads, and hash-existence queries. This service calls COSMeTIC prover APIs via `curl` and **owns all Redis job/session state**.

- **Redis (`redis`, container `mcp-redis`, port 6379)**  
  Stores per-session state and job records with a 7‑day TTL.

- **Postgres (`postgres`, container `mcp-postgres`, port 5432)**  
  Stores dataset-driven user accounts, raw hashes, and input data, plus metadata about each imported input‑files ZIP archive.

External dependency:

- **COSMeTIC prover services** reachable on `PROVER_BASE_HOST` (default `COSMeTICprover`) ports:
  - `5012` – logistic accuracy prover.
  - `5013` – KS (Kolmogorov–Smirnov).
  - `5014` – LRT (Likelihood Ratio Test).
  - Separate **input-files** service at `COSMeTICprover-input-files:5015/input-files/zip` for dataset sync.

**Core design rule:**  
Flask is a thin HTTP + agent shell. **Proof lifecycle and job-state Redis writes are owned by the MCP prover server**, with one backend security exception: Flask binds `session_id -> owner_user_id` in Redis to enforce cross-user session isolation.

**Ablation configurations:**
The system supports 4 runtime configurations (A/B/C/D) controlled by a single `CONFIG` env var. These are used for an ablation study that selectively disables architectural features to measure their contribution. Config C is the current/default system; A, B, D are derivations. See **Section 12 (Ablation Study CONFIG Mechanism)** for the full matrix and code-level details.

---

## 2. Runtime Services and Deployment

### 2.1 Docker Compose Topology

Defined in `docker-compose.yml`:

- **Service: proverserver**
  - Image: built from `Dockerfile.prover`.
  - Container: `mcp-prover-server`.
  - Port: `8003:8003`.
  - Environment (subset):
    - `PROVER_MCP_PORT=8003`, `PROVER_MCP_HOST=0.0.0.0`.
    - (The prover derives per‑type COSMeTIC URLs from `PROVER_BASE_HOST`; there is no `PROVER_BASE_URL` — it was dead config and has been removed.)
    - `DOWNLOADS_DIR=/app/downloads`.
    - `REDIS_HOST=redis`, `REDIS_PORT=6379`.
    - `DATABASE_URL=postgresql://mcp_user:mcp_password@postgres:5432/mcpdb`.
    - `INPUT_FILES_ZIP_URL=http://COSMeTICprover-input-files:5015/input-files/zip`.
    - `INPUT_ZIPS_DIR=/app/data/input_zips`.
    - `CONFIG=${CONFIG:-C}` – selects ablation configuration (A/B/C/D). **Must match backend's value.**
    - `EVAL_MODE=${EVAL_MODE:-true}` – set on this service by docker-compose for parity, but **`proverserver.py` never reads `EVAL_MODE`** (the tool-call interceptor lives only in the backend). Only `app.py` consumes it: it gates the tool-call log in the `/get` response.
  - Volumes:
    - `./downloads:/app/downloads` (shared with backend).
    - `./data/input_zips:/app/data/input_zips`.
  - Networks:
    - `mcp-network` (internal app network).
    - `regulatory_hypothesis_tests_default` (external — created by the COSMeTIC Compose project; the backend/prover join it to reach COSMeTICprover on 5012-5015).
  - Depends on: `redis`, `postgres`.

- **Service: backend**
  - Image: built from `Dockerfile.backend`.
  - Container: `mcp-backend`.
  - Port: `5001:5001`.
  - Environment (subset):
    - `FLASK_PORT=5001`.
    - `DEEPINFRA_API_KEY` (from `.env` or environment).
    - `SECRET_KEY` (Flask session secret).
    - `PROVER_MCP_URL=http://proverserver:8003/mcp`.
    - `DOWNLOADS_DIR=/app/downloads`.
    - `REDIS_HOST=redis`, `REDIS_PORT=6379`.
    - `DATABASE_URL=postgresql://mcp_user:mcp_password@postgres:5432/mcpdb`.
    - `INPUT_FILES_ZIP_URL` / `INPUT_ZIPS_DIR` (for dataset sync).
    - `CONFIG=${CONFIG:-C}` – selects ablation configuration (A/B/C/D). **Must match proverserver's value.**
    - `EVAL_MODE=${EVAL_MODE:-true}` – gates tool-call logging in interceptor and response format in `/get`. Config D automatically forces this to `true` regardless of env var.
  - Volumes:
    - `./downloads:/app/downloads` (shared with prover).
    - `./data/input_zips:/app/data/input_zips`.
  - Depends on: `proverserver`, `redis`, `postgres`.
  - Networks: same as `proverserver` (`mcp-network` + the external `regulatory_hypothesis_tests_default`).

- **Service: redis**
  - Image: `redis:7-alpine`.
  - Container: `mcp-redis`.
  - Port: `6379:6379`.
  - Volume: `redis-data:/data`.
  - Network: `mcp-network`.
  - Command: `redis-server --appendonly yes`.

- **Service: postgres**
  - Image: `postgres:16-alpine`.
  - Container: `mcp-postgres`.
  - Port: `5432:5432`.
  - Environment:
    - `POSTGRES_DB=mcpdb`, `POSTGRES_USER=mcp_user`, `POSTGRES_PASSWORD=mcp_password`.
  - Volumes:
    - `postgres-data:/var/lib/postgresql/data`.
    - `./sql:/docker-entrypoint-initdb.d:ro` (schema initialization).
  - Network: `mcp-network`.

### 2.2 Dockerfiles

- **`Dockerfile.backend`**
  - Base: `python:3.13-slim`.
  - Installs Python deps from `requirements.txt`.
  - Copies:
    - `app.py`, `context_manager.py`, `account_store.py`, `dataset_sync.py`.
    - `templates/`, `static/`.
  - Creates `/app/data/input_zips`.
  - Exposes port `5001`.
  - Sets `FLASK_PORT=5001`.
  - Command: `python app.py`.
  - **Flask runs in `debug=True` mode** (see `app.py` `__main__` block: `app.run(debug=True, host='0.0.0.0', port=flask_port)`). This is intentional for development/research use — it enables the interactive debugger (PIN printed on startup) and auto-reload. **Not suitable for production deployment without changes.** Setting `debug=False` or switching to a WSGI server (gunicorn/uwsgi) would be required for production.

- **`Dockerfile.prover`**
  - Base: `python:3.13-slim`.
  - Installs `curl` and `jq` via `apt-get`.
  - Installs Python deps from `requirements.txt`.
  - Copies:
    - `proverserver.py`, `context_manager.py`, `account_store.py`.
  - Exposes port `8003`.
  - Sets `PROVER_MCP_PORT=8003`, `PROVER_MCP_HOST=0.0.0.0`.
  - Command: `python proverserver.py`.

---

## 3. Backend Service (`app.py`)

### 3.1 Responsibilities

- Serve the chat UI (`/`).
- Handle auth and “my hash” APIs.
- Host a LangGraph/LangChain agent using DeepInfra's OpenAI-compatible Qwen3-32B endpoint.
- Act as an MCP HTTP client to the prover server.
- Proxy proof downloads and Redis-backed context requests through MCP tools.

### 3.2 Key Dependencies

- `flask` for HTTP routes and session cookies.
- `langchain_openai.ChatOpenAI` for LLM access (pointed at DeepInfra's OpenAI-compatible endpoint).
- `langgraph.prebuilt.create_react_agent` for a ReAct-style agent.
- `langchain_mcp_adapters.client.MultiServerMCPClient` for MCP over HTTP.
- `httpx` used only in `initialize_agent()` to probe the prover MCP endpoint at startup (up to 40 retries).
- `python-dotenv` for `.env` support.
- `bcrypt`, `psycopg2-binary`, `redis` (via shared modules).

### 3.3 Agent and MCP Client Initialization

- On import:
  - Loads `.env`.
  - Reads `SYSTEM_CONFIG = os.getenv("CONFIG", "C")` at module scope. Logged on startup as `"System configuration: {X}"`. Used throughout `app.py` to gate config-specific behavior.
  - Derives `EVAL_MODE = os.getenv("EVAL_MODE", "false").lower() == "true" or SYSTEM_CONFIG == "D"`. This means Config D **always** has eval logging on, regardless of explicit env var.
  - Creates `Flask` app and configures session cookies:
    - `SESSION_COOKIE_HTTPONLY=True` (prevents JavaScript access to session cookie).
    - `SESSION_COOKIE_SAMESITE="Lax"` (CSRF mitigation).
    - `SESSION_COOKIE_SECURE` configurable via env (defaults to `false`).
  - Defines `format_tool_response(tool_name, raw_response_text)` (used only in Config D):
    - Parses raw tool response as JSON, then produces a deterministic human-readable sentence per tool:
      - `prove_hash` / `prove_my_data` → `"Your {proof_type} proof has been submitted. Job ID: {job_id}..."` (proof_type comes only from response if present).
      - `check_status` → `"Job {job_id} ({proof_type}): Status is {status}."`.
      - `download_proof` → `"Your proof file has been downloaded. Filename: {filename}, size: {file_size} bytes..."`.
      - `verify_proof` → `"Proof verification PASSED."` if response JSON has top-level `ok=true`, else `"Proof verification FAILED."` (errors / wrong-port 404 / `ok=false` all fall to FAILED).
      - `check_hash_existence` / `check_my_hash_existence` → passed through unchanged (already formatted markdown).
      - `get_session_context` → manually formatted job list, **scrubbing internal fields** like `user_hash` (never shown to user).
      - Unknown tools / parse failure → raw text unchanged.
    - Intentional: does not use LLM summarization, does not call the model.
  - Defines `compute_trust_label(mcp_touched, tool_calls)` (used by `/get` regardless of config) — produces the 4-tier trust suffix appended to every response. Strongest-wins precedence across all tools called this turn: `verify_proof` ok=true → `[Cryptographically verified]`; `check_hash_existence` / `check_my_hash_existence` whose Summary contains `"was found in"` → `[Cryptographically provable]`; any other tool call (including hash-not-found, verify ok=false, wrong-port 404, prove/status/download/get_session_context success) → `[Backed by COSMeTIC prover]`; no tool called → `[Not backed by COSMeTIC prover]`. The function falls back to the coarse `[Backed by COSMeTIC prover]` whenever `mcp_touched=True` but `tool_calls` is empty — this happens when `EVAL_MODE` is off and the interceptor doesn't capture per-call records. See **§3.5 step 8** for the call site.
  - Defines `_extract_tool_response_text(result)` — walks `CallToolResult.content` (the raw MCP response shape before langchain's adapter converts it) and joins text fragments. Used by the interceptor's eval logging.
  - Defines `tool_result_to_text(result)` — a SEPARATE function that normalizes *already-converted* langchain tool results (strings or lists of dicts with `"text"` keys or objects with `.text` attributes). Used by the `/download_proof` and `/context/<session_id>` endpoints which call `tool.ainvoke()` directly and receive the converted output. This duplication exists because the interceptor runs *before* langchain's converter, while direct `ainvoke()` calls receive *after*-converter output. Keeping them separate avoids accidental double-conversion bugs.
  - Declares globals:
    - `agent`: LangGraph agent (initially `None`).
    - `client`: `MultiServerMCPClient` instance (initially `None`).
    - `current_session_id_var` / `current_user_id_var`: `contextvars.ContextVar` for per-request context.
    - `mcp_tracker_var`: request-scoped tracker dict used to mark whether any MCP tool was invoked.

- Defines `SessionIdInjectorInterceptor`:
  - On each MCP tool call:
    - Sets `mcp_tracker["touched"] = True` (used for the trust label, regardless of tool outcome).
    - **Eval logging (when `EVAL_MODE=true`):** before any arg mutation, captures a snapshot of the raw tool name (`request_data.name`) and the raw arguments the LLM provided (`request_data.args`) into a `tool_call_record` dict with a UTC timestamp. After the tool executes, captures `arguments_after_correction` (post-injection, post-stripping) and the tool's text response. The record is appended to `mcp_tracker["tool_calls"]`. When `EVAL_MODE` is not set or false, this logging is entirely skipped.
    - Reads the current contextvars and:
      - Adds `session_id` to tool args and injects `x-session-id` header (regardless of config).
      - **Identity stripping (Config B/C/D only):** injects `x-user-id` header and **strips any `user_id` from tool args** (prevents the LLM from impersonating other users).
      - **Config A (`SYSTEM_CONFIG == "A"`):** skips BOTH the `x-user-id` header injection AND the args strip. The LLM's fabricated identity (if any) flows through to the tool. This is the "trust the LLM" ablation — identity protection is off end-to-end.
    - **Short-circuit when no context:** if BOTH `session_id` AND `user_id` contextvars are `None`, the interceptor calls `handler(request_data)` unmodified (no args/headers mutation). In practice this path only triggers during `initialize_agent()`'s `client.get_tools()` probe at startup or from test harnesses. The `/download_proof` and `/context/<session_id>` routes **do** set both contextvars via `current_session_id_var.set(...)` / `current_user_id_var.set(...)` before calling `ainvoke()` and reset them in `finally`, so those routes take the normal (non-short-circuit) injection path. Eval logging is also gated on the tracker being present (`mcp_tracker_var` is only set by `/get`), so tool calls from non-`/get` paths are neither injected nor logged.

- `initialize_agent()` is run in a background daemon thread:
  - Probes `PROVER_MCP_URL` (default `http://localhost:8003/mcp`) using `httpx` (up to 40 tries, 0.5s each).
  - On success:
    - Instantiates `MultiServerMCPClient` with the prover server and the interceptor.
    - Ensures `DEEPINFRA_API_KEY` is set.
    - Calls `client.get_tools()` with up to 10 retries.
    - Creates `ChatOpenAI` with `model=os.getenv("LLM_MODEL", "Qwen/Qwen3-32B")`, `base_url=os.getenv("LLM_BASE_URL", "https://api.deepinfra.com/v1/openai")` (the environment variable was added at release; the evaluated build had the DeepInfra endpoint fixed), and **`temperature=0.0`** for reproducibility across ablation runs. The model is env-driven; the default and primary research model is Qwen3-32B, but cross-model experiments have used `meta-llama/Meta-Llama-3-8B-Instruct` and `mistralai/Mistral-Small-3.2-24B-Instruct-2506` via this env var.
    - **Conditional system prompt for Llama models (`"llama" in model name`):** when the configured model name contains `"llama"` (case-insensitive), `create_react_agent` is constructed with an explicit tool-use system prompt instructing the model to call tools for proof/status/download/hash queries rather than reply in plain text. Llama 3 8B does not reliably engage tool calling against an 8-tool surface without this scaffolding; Qwen models do, and fall through to the no-prompt branch (`create_react_agent(model, tools)`). This is documented as a noted asymmetry in the cross-model evaluation: it does not affect Qwen runs but is required for Llama runs to produce tool calls.
    - Creates a `create_react_agent(model, tools[, prompt=...])` instance and assigns it to `agent`.
  - On failure, logs and retries after 5 seconds.

### 3.4 Auth and User Context

- Session-based auth via Flask.
- `signin_dataset_user(username, password)` (from `account_store.py`) authenticates against `dataset_users` in Postgres.
- Auth API:
  - `POST /auth/signin` – sign in existing dataset user; stores `user_id` and `username` in session; sets `session.permanent = True` (Flask default: 31-day cookie lifetime).
  - `POST /auth/logout` – clears session.
  - `GET /auth/me` – returns `{logged_in: bool, user: {...}}` from session/DB.

- Hash APIs:
  - `GET /api/me/hash` – requires login; returns masked hash (`get_masked_hash_for_user`) or `null` if none.
  - `POST /api/me/hash` – always returns error; manual hash saving is disabled to keep hashes dataset-driven only.

### 3.5 Chat Endpoint (`POST /get`)

Flow:

1. Client sends `msg` and `session_id`.
2. Backend:
   - Resolves the logged-in user (if any) and extracts `user_id`.
   - Validates `msg`.
   - Fails fast if `agent` is still `None`.
   - **Session ownership enforcement (Risk B mitigation)**: if the user is logged in, calls  
     `context_manager.claim_or_verify_session_owner(session_id, user_id)` which:
     - First request claims the session by writing `owner_user_id` into Redis state.
     - Later requests must match the same `owner_user_id` or the backend returns `403`.
3. For each request:
   - Creates a fresh asyncio event loop.
   - Sets `current_session_id_var` and `current_user_id_var` for this request.
   - **Constructs the message list:**
     - **Config B/C/D (stateless, default):** `messages = [{"role": "user", "content": msg}]` — single message, no history.
     - **Config A (`SYSTEM_CONFIG == "A"`):** reads prior messages from Redis via `context_manager.get_recent_messages(session_id, limit=15)`, prepends them, appends the current user message. This uses the previously-dormant `save_message`/`get_recent_messages` pair in `context_manager.py`.
   - Calls:
     - `agent.ainvoke({"messages": messages})`
     - Wrapped in `asyncio.wait_for(..., timeout=420.0)` (7 minutes).
   - Resets the contextvars in a `finally` block.

**Stateless per-request (Configs B/C/D).** In the default configs, each chat request sends only a **single user message** to the agent — there is no conversation history accumulation. The LLM has no memory of previous turns.

**Config A enables conversation history.** `context_manager.save_message(session_id, role, content)` is called twice after the agent returns: once with the user message, once with the final assistant text (before the trust label is appended, so the saved history stays clean). `get_recent_messages` returns up to 15 prior `{role, content}` pairs on the next turn. Note: LangGraph's ReAct agent internally produces `HumanMessage`/`AIMessage(tool_calls=...)`/`ToolMessage` triples, but `save_message` stores only `{role: "user"|"assistant", content: str}` — so tool-call records are NOT replayed. The agent sees "I said X, assistant said Y" without knowing tools were invoked.

**Redis-backed session continuity replaces LLM memory (Configs B/C/D).** Instead of relying on conversation history, the system stores job identity and metadata in Redis. When the user asks "what's the status of my job?", the LLM does not need to remember the job ID from earlier messages. The `check_status` MCP tool reads `latest_job_id` and `proof_type` from Redis to know *which* job to check and *which* COSMeTIC port to hit, then calls the **live COSMeTIC API** (`GET /jobs/{job_id}`) for the real-time status, and writes the fresh status **back to Redis** as a side effect. For broader questions like "what jobs do I have?", the `get_session_context` tool reads **only from Redis**, returning whatever was previously stored — which may be stale if `check_status` was never called for those jobs. Redis cannot update itself; it only gets fresh data when MCP tools call COSMeTIC and write back.

4. It extracts the last message's `content` from `response["messages"]`. **Empty response recovery:** if the last message's content is empty or whitespace-only, the code walks backwards through `response["messages"]`:
   - If any earlier message has non-empty `content`, use that as `bot_response`.
   - Else if any message has `additional_kwargs["tool_calls"]` (meaning the LLM emitted a tool-call message but didn't produce a final text turn), set `bot_response = "Tool was called but no response received. Please check the prover server logs."`
   - If still empty after the walk, `bot_response = "I received your request but didn't get a response. Please check the server logs or try again."`
   
   This handles degenerate LangGraph agent states where the final message is an empty tool-call wrapper.

5. **Strip Qwen3 reasoning blocks.** Before any persistence or routing decision, `bot_response = re.sub(r'<think>.*?</think>', '', bot_response, flags=re.DOTALL).strip()`. Qwen3-32B emits `<think>…</think>` blocks in its completion text (it's a reasoning model); these must be removed *before* Config A save_message (so replayed history stays clean), *before* Config D formatter routing (so the `/response` sanity check is accurate), and *before* the trust label append (so `response_text` returned to the client is clean). The regex uses non-greedy `.*?` with `DOTALL` so it correctly handles multiline and multiple `<think>` pairs. Raw tool outputs (`tool_calls[*].response`) are unaffected — they come from the MCP prover, not the LLM.

6. **Config A: save to history.** If `SYSTEM_CONFIG == "A"`, persists `(user_message, bot_response)` via `context_manager.save_message`. Happens *after* the `<think>` strip (so replayed history is clean) and *before* the trust label append (so the saved history doesn't contain `[Backed by COSMeTIC prover]` markers).

7. **Config D: replace bot_response with formatted tool output.** If `SYSTEM_CONFIG == "D"` AND a tool was called (`mcp_tracker["touched"]` is true), reads the LAST tool call from `mcp_tracker["tool_calls"]` and replaces `bot_response` with `format_tool_response(tool_name, tool_response)`. The LLM's summary is discarded. If no tool was called, `bot_response` remains the LLM's text. This requires `EVAL_MODE=true` (forced by `SYSTEM_CONFIG == "D"` derivation at module load).

8. *(The reported evaluation ran an earlier two-tier version of this label; only `[Backed by COSMeTIC prover]` and `[Not backed by COSMeTIC prover]` appear in the logged results.)* Appends a deterministic trust suffix owned by backend logic via `compute_trust_label(mcp_touched, mcp_tracker.get("tool_calls", []))`. The label is **always-on across all configs** (A/B/C/D); it is not gated on `SYSTEM_CONFIG`. There are four tiers, with strongest-wins precedence across all tools called in the turn:
   - `[Cryptographically verified]` — `verify_proof` returned top-level `ok=true` (cryptographic verification was performed and passed). Detection: `json.loads(tool_response).get("ok") is True`.
   - `[Cryptographically provable]` — `check_hash_existence` or `check_my_hash_existence` returned a "found" result (hash is in the SMT; a zkSNARK proof of the claim can be requested). Detection: the Summary substring `"was found in"` appears in the tool response (case-insensitive). The wrapper tool `check_my_hash_existence` delegates to `check_hash_existence` internally so both produce the same response format.
   - `[Backed by COSMeTIC prover]` — any other tool call: prove_hash, prove_my_data, check_status, download_proof, get_session_context, hash-existence with hash-not-found, verify_proof with `ok=false`, verify_proof errored / wrong-port 404, or any error returned by a tool that was nonetheless invoked. Also the fallback whenever a tool was touched but `tool_calls` is empty (EVAL_MODE off).
   - `[Not backed by COSMeTIC prover]` — no MCP tool was called (LLM answered entirely on its own).
   
   The tracker (`mcp_tracker["touched"]`) is set by the interceptor the instant any tool call enters it, regardless of tool outcome. The `tool_calls` list is captured only when `EVAL_MODE=true` (auto-true for Config D). The implementation is `compute_trust_label()` in `app.py` (see §3.3).

9. Responds with JSON: `{"response": bot_response}` or `{"error": ...}`.

**Error surfacing convention — important for test grading.** The `/get` endpoint returns a JSON body with an `"error"` field on any failure. The HTTP status code depends on the failure mode — early validation / auth errors get proper non-200 codes, but most downstream failures (including LLM-provider rate limits) surface as HTTP 200 with an error body:
- `{"error": "msg is required"}` — missing input (**HTTP 400**).
- `{"error": "Session store is unavailable"}` (**HTTP 503**) / `{"error": "This session belongs to another user..."}` (**HTTP 403**) / `{"error": "Invalid session or user"}` (**HTTP 400**) — from `claim_or_verify_session_owner` outcomes.
- `{"error": "Agent is still initializing. Please wait a moment and try again."}` — agent not ready (**HTTP 200**, no explicit status code in the return). The background daemon thread that initializes the agent may still be retrying `client.get_tools()`; during that window this string is returned.
- `{"error": "The request timed out after 7 minutes..."}` — when `asyncio.wait_for(agent.ainvoke(...), timeout=420)` fires (**HTTP 200**).
- `{"error": "Error: {str(e)}"}` — catch-all for any other exception inside the try block (**HTTP 200**). **LLM-provider rate-limit errors (both hard 429 and soft 200-with-body-error) surface through this path**; the underlying OpenAI/DeepInfra SDK retries a few times internally and, if still failing, raises an exception that becomes this JSON. The test runner detects `429`, `rate_limit`, `rate limit`, `too_many_requests`, `rate limited`, and `throttled` substrings in the error string and retries with cooldown.

**Implication for the test runner:** checking `http_status == 200` is NOT sufficient to conclude success — the runner must also inspect the response body for an `"error"` field. Conversely, non-200 status codes (400/403/503) are unambiguous failures.
   - **When `EVAL_MODE=true`**, the response JSON also includes:
     - `backed_by_cosmetic` (bool): same value used for the trust label.
     - `tool_calls` (list): array of tool call records from `mcp_tracker["tool_calls"]`. Each record contains `tool_name`, `arguments` (raw LLM-provided, before correction), `arguments_after_correction` (post session_id injection / user_id stripping), `response` (tool output text), and `timestamp` (UTC ISO).
   - When `EVAL_MODE` is not set or false, the response format is unchanged — only `{"response": "..."}`.

The agent itself decides when to call tools such as `prove_hash`, `prove_my_data`, `check_status`, `download_proof`, `verify_proof`, `check_hash_existence`, `check_my_hash_existence`, or `get_session_context`. Thanks to the interceptor, tools always see the right `session_id` and `user_id` context.

### 3.6 Download Endpoint (`POST /download_proof`)

Purpose: Provide **reliable file downloads** for proof artifacts, decoupled from chat messages.

Flow:

1. Accepts JSON or form with:
   - `session_id` (required).
   - `job_id` (optional; if omitted, the backend passes no `job_id` to the MCP tool, which then falls back to the latest completed job in Redis).
2. Requires login (`@require_login`) and enforces session ownership (`claim_or_verify_session_owner`).
3. Checks that `agent` is initialized (returns error if `None`). Note: `client` is used but not explicitly null-checked — if `client` is `None`, the `client.get_tools()` call will raise an exception caught by the outer `try/except`.
4. Builds `downloads_dir` from `DOWNLOADS_DIR` (default `/app/downloads`) and ensures it exists.
5. Builds `desired_filename = "proofs-{job_id}.zip"` or `"proofs-session-{session_id}.zip"`.
6. Fetches tools from MCP client and finds tool named `"download_proof"`.
7. Sets contextvars (`session_id`, `user_id`) and calls:
   - `download_proof_tool.ainvoke({"output_filename": desired_filename, "job_id": job_id?})`
   - With `asyncio.wait_for(..., timeout=120.0)`.
8. Normalizes the result with `tool_result_to_text`, parses JSON expecting:
   - `{ "success": true, "filename": "<basename>.zip", ... }`.
9. Reconstructs the absolute path:
   - `output_filename = os.path.join(downloads_dir, os.path.basename(filename))`.
   - Validates `commonpath` to guard against path traversal.
10. If the file exists, uses `send_file` with `as_attachment=True, download_name=filename_only` and sets the MIME type based on suffix:
    - `.zip` filename → `Content-Type: application/zip`.
    - Otherwise → `Content-Type: application/octet-stream`.
    - Also explicitly sets `response.headers['Content-Disposition'] = f'attachment; filename="{filename_only}"'` (in addition to the implicit one set by `as_attachment=True`) to ensure browsers treat it as a download rather than navigating inline.
11. On any error (timeout, missing tool, missing file, bad JSON), it returns a JSON error message and logs a debug line to stdout with format: `Download error: Download failed. Result: {error_msg}, File exists: {file_exists}, Expected path: {expected_path}`.

### 3.7 Context Endpoint (`GET /context/<session_id>`)

Flow:
1. `@require_login` ensures an authenticated session.
2. Calls `claim_or_verify_session_owner(session_id, user_id)` — 503 on Redis down, 403 on owner mismatch, 400 on invalid.
3. Explicitly checks `if client is None` → returns HTTP 500 `"MCP client is not initialized"` (this endpoint is stricter than `/download_proof` which trusts agent-init implies client-init; because `/context` only needs the MCP client, it checks the client directly).
4. Creates a fresh asyncio event loop.
5. Fetches MCP tools via `client.get_tools()`, locates the one named `get_session_context`. Returns 500 if not found.
6. Sets `current_session_id_var` and `current_user_id_var` contextvars (same pattern as `/get`).
7. Invokes the tool with `{"session_id": session_id}` (no timeout wrapper — this tool is cheap and Redis-only).
8. Returns `tool_result_to_text(result)` directly — **raw JSON body, no trust label, no EVAL_MODE metadata**. Differs from `/get` in that this is an ops/observability endpoint rather than a chat response.

This route is primarily for debugging and observability. It requires login and enforces that
the caller is the owner of the `session_id` (Risk B mitigation).

---

## 4. MCP Prover Service (`proverserver.py`)

### 4.1 Responsibilities

- Expose all proof‑related tools to LangGraph via MCP over HTTP:
  - Proof submission (`prove_hash`, `prove_my_data`).
  - Job status (`check_status`).
  - Proof download (`download_proof`).
  - **Proof verification (`verify_proof`)** — calls COSMeTIC's `/verify-job/{id}` endpoint and returns its `{"ok": bool, "results": [...], "user_hash": ...}` JSON.
  - Hash existence checks (`check_hash_existence`, `check_my_hash_existence`).
  - Session inspection (`get_session_context`).
- Manage Redis session and job state via `context_manager`.
- Read user hashes from Postgres via `account_store`.
- Talk to external COSMeTIC prover and input‑files APIs via `curl`.

### 4.2 FastMCP Setup

- Declares a FastMCP app:
  - Name: `"Prover"`.
  - Transport security:
    - DNS rebinding protection enabled.
    - Allowed hosts: `proverserver`, `proverserver:*`, `localhost`, `127.0.0.1`, `0.0.0.0`.
- Entrypoint:
  - Reads `PROVER_MCP_HOST` (default `0.0.0.0`) and `PROVER_MCP_PORT` (default `8003`).
  - Binds FastMCP and runs with `transport="streamable-http"`.

### 4.3 Context and User Resolution

To keep tools clean, `proverserver.py` centralizes how `session_id` and `user_id` are determined:

- `_resolve_session_id(session_id, ctx)`:
  - If `session_id` arg is provided, returns it.
  - Else inspects `ctx.request_context.request.headers` for `x-session-id` or `X-Session-Id`, returning that if present.
  - Returns `None` if nothing is found.

- `_resolve_user_id(user_id, ctx)`:
  - Looks for `x-user-id` / `X-User-Id` in MCP HTTP headers.
  - **Unlike `_resolve_session_id`**, this function deliberately does **NOT** fall back to the `user_id` tool argument — except in Config A and Config A_STAR.
  - **Config A / A_STAR (`SYSTEM_CONFIG in ("A", "A_STAR")`):** if the header is absent AND a `user_id` argument was passed by the tool caller, falls back to `str(user_id)`. This completes the "trust the LLM" ablation: in Config A (and A_STAR), `app.py` doesn't inject the header AND `proverserver.py` accepts the LLM-provided argument. Without BOTH changes, the identity ablation would have zero effect (the tool ignores the argument entirely).
  - **Config B/C/D:** returns `None` if no header; the tool then returns "Please sign in first" for identity-dependent operations.

Because the backend injects both via the interceptor (Config B/C/D), most agent tool calls can **omit** `session_id`/`user_id` arguments and still get correct values. In Config A, tools would typically receive LLM-fabricated user_ids.

**Prover-side SYSTEM_CONFIG:** `proverserver.py` reads its own `SYSTEM_CONFIG = os.getenv("CONFIG", "C")` at module load and logs `"Prover server configuration: {X}"` on startup. This must match `app.py`'s `SYSTEM_CONFIG` (both services read from the same docker-compose env var).

### 4.4 COSMeTIC API Selection and `curl` Wrapper

- `_normalize_proof_type(proof_type)`:
  - Canonicalizes aliases:
    - `"ks"`, `"kolmogorov"`, `"kolmogorov-smirnov"`, `"kolmogorov_smirnov"` → `"KS"`.
    - `"lrt"`, `"likelihood_ratio_test"`, `"likelihood-ratio-test"` → `"LRT"`.
    - Everything else → `"logistic_accuracy"`.

- `_get_base_url(proof_type)`:
  - Reads `PROVER_BASE_HOST` (default `"COSMeTICprover"`).
  - `KS` → `http://{host}:5013`.
  - `LRT` → `http://{host}:5014`.
  - Logistic accuracy → `http://{host}:5012`.

- `_run_curl_command(url, extra_args="", json_data=None)`:
  - Chooses a timeout:
    - `/jobs/` (status) → 300s max.
    - `/download` → 120s max.
    - `/verify-job/` → 300s max (ezkl.verify can be slow over many sub-proofs; matches the upstream verification timeout).
    - `prove-hash` / `prove-raw` → 240s max.
    - `check-hash` / `check-existence` → 120s max.
    - Other URLs → 30s.
  - Constructs a `curl` command as a list:
    - Always uses `-s -L --max-time <timeout>`.
    - Adds extra args (`-X POST`, etc.).
    - Adds `-H "Content-Type: application/json" -d '<json>'` when `json_data` is provided.
  - Returns a `subprocess.CompletedProcess` with `stdout` and `stderr` captured.

Error handling in tools looks at `returncode` to distinguish timeouts, hostname resolution, and connection errors, returning descriptive, user‑facing messages.

### 4.5 Proof Submission Tools

#### 4.5.1 `prove_hash`

Signature (simplified):

- Args:
  - `user_hash: Optional[str]`.
  - `nonce: Optional[str]` (JSON or Python literal string).
  - `proof_type: str` (required; no default).
  - `session_id`, `user_id` (optional; usually injected).

Behavior:

1. Resolve `session_id` via `_resolve_session_id` and touch the session in Redis.
2. Resolve `user_id` via `_resolve_user_id`.
3. If `user_hash` is missing:
   - If no `user_id` → return instructional error stating that a hash is required when not signed in.
   - Else, use `get_primary_hash_for_user(user_id)` from `account_store`. If missing, instruct that no hash is saved.
4. Require `proof_type` explicitly; error if omitted (no default).
5. If `nonce` is `None`, use the default nonce:
   - `[[0.0, 0.0, 0.0]]`
   - This keeps submission behavior deterministic across COSMeTIC endpoints.
6. Normalize `proof_type` and build `base_url` / `url = base_url + "/prove-hash/"`.
7. Parse `nonce` as JSON or Python literal; return an error if parsing fails.
8. Build payload:
   - KS: `{user_hash, nonce}`.
   - LRT: `{smt_list: ["full", "reduced"], user_hash, nonce}`.
   - Logistic: `{smt_list: ["length", "acc"], user_hash, nonce}`.
9. Call `_run_curl_command(url, "-X POST", json_data=payload)`.
10. On common `curl` errors (timeout, bad host, connection failure), return a descriptive multi‑line error string that explains likely causes.
11. On success:
    - Parse `stdout` as JSON.
    - If `job_id` present, call:
      - `context_manager.add_job_id(session_id, job_id, proof_type_normalized, user_hash, nonce, status)`.
    - Return the JSON response pretty‑printed as a string.

This is the foundational tool for explicit hash proofs; the account‑aware `prove_my_data` wraps it.

#### 4.5.2 `prove_my_data`

Signature (simplified):

- Args:
  - `nonce: Optional[str]`.
  - `proof_type: str` (required).
  - `session_id`, `user_id` (optional; injected).

Behavior:

1. Resolve `user_id` via `_resolve_user_id`; if missing, return “Please sign in first.”
2. Resolve `session_id`; if missing, return error JSON requiring a session.
3. Fetch the saved user hash via `get_primary_hash_for_user(user_id)`; if missing, instruct that no hash is saved.
4. Require `proof_type` explicitly.
5. If `nonce` is `None`, passes `None` to `prove_hash`, which then applies the default nonce (`[[0.0, 0.0, 0.0]]`). If the LLM extracted a nonce from the user's prompt (e.g., *"prove my data in KS with nonce [[1.0, 2.0, 3.0]]"*), that value flows through unchanged. Note on `input_data`: COSMeTIC owns the user's input data on its side (loaded from `input_<hash>.json` files served by `COSMeTICprover-input-files:5015`). The chatbot's payload to `/prove-hash/` carries only `{user_hash, nonce}` (and `smt_list` for LRT/logistic) — COSMeTIC resolves the hash to the actual data internally and proves over it. The chatbot's Postgres `input_data` column is a local replica populated by dataset sync; it is currently unused at runtime (`get_input_data_for_user` has no callers) and exists for future UI/observability.
6. Delegate to `prove_hash` with:
   - `user_hash=saved_hash`.
   - The user-provided nonce (if any); otherwise default nonce behavior.
   - The chosen `proof_type`.

This is the **recommended tool** when the user is authenticated and uses phrases like “my hash” or “my data.”

Additional conversation behavior:

- The tool guidance explicitly avoids offering push notifications ("notify when complete"), because notification delivery is not implemented.
- For completion tracking, users should manually run status checks.

### 4.6 Status and Download Tools

#### 4.6.1 `check_status`

Signature (simplified):

- Args:
  - `job_id: Optional[str]`.
  - `proof_type: str = "logistic_accuracy"`.
  - `session_id: Optional[str]`.

Behavior:

1. Resolve `session_id`; if missing, return error JSON.
2. Touch the session in Redis.
3. **B4 gate — compute `resolved_job_id`:**
   - If `job_id` was explicitly provided → use it.
   - **Config B/C/D (`SYSTEM_CONFIG != "A"`):** if no `job_id`, fall back to `context_manager.get_latest_job_id(session_id)`.
   - **Config A:** no fallback; `resolved_job_id` stays `None`.
4. If `resolved_job_id` is still `None`, return `"job_id is required and no latest job found in session state"`.
5. **B5 gate — normalize proof_type:** call `_normalize_proof_type(proof_type)` first. If the result is `"logistic_accuracy"` (the default) AND `SYSTEM_CONFIG in ("C", "D")`, read the stored job's `proof_type` from Redis and use that instead. **Config A/B (`SYSTEM_CONFIG not in ("C", "D")`):** skip this Redis lookup entirely — use whatever `_normalize_proof_type` returned (which is the LLM's input or the default).
   - Subtlety: this is a *fallback-on-default*, not a true override. If the LLM explicitly passes `"KS"`, the LLM value wins even in Config C/D.
6. Build `url = "{base_url}/jobs/{resolved_job_id}"`, call `_run_curl_command`.
7. On timeout, return a detailed human‑readable explanation.
8. On success:
   - Parse JSON.
   - If dict has `"status"`:
     - `context_manager.update_job_status(session_id, job_id, status)`.
   - Else if the response dict has `"job_id"` but no `"status"` key:
     - `context_manager.add_job_id` with minimal metadata (proof type, empty hash/nonce, status `"unknown"`).
   - Return the JSON string to the agent.

**Tool description (LLM-facing docstring):** the tool's Python docstring was sharpened to help the LLM disambiguate from `get_session_context`. Explicit phrases like "Use this tool when the user asks about the status of a SINGLE job" and "Do NOT use `get_session_context` for single-job status questions" are present. Without this, the LLM tends to pick `get_session_context` for "check my status" queries.

This is a **single-shot** status query; the agent can call it repeatedly to poll if desired.

#### 4.6.2 `download_proof`

Signature (simplified):

- Args:
  - `job_id: Optional[str]`.
  - `output_filename: Optional[str]`.
  - `proof_type: str = "auto"`.
  - `session_id: Optional[str]`.

Behavior:

1. Resolve `session_id`; if missing, return error JSON.
2. Touch the session.
3. **B4 gate — resolve `resolved_job_id`:**
   - If `job_id` was explicitly provided → use it.
   - **Config B/C/D (`SYSTEM_CONFIG != "A"`):** if no `job_id`, try the three-tier Redis fallback:
     a. `get_latest_completed_job_id(session_id)` — strict completed-job pointer.
     b. If that's None, try `get_latest_job_id` + check if its status is `done/completed/success`.
     c. Otherwise stays `None`.
   - **Config A:** entire fallback block is skipped.
4. If still `None`, return `"job_id is required and no completed job found in session state"`.
5. **B5 gate — resolve `proof_type`:** read job data from Redis (`job_data = context_manager.get_job(...)`). Normalize the incoming `proof_type` arg to lowercase; default is `"auto"` if arg is empty.
   - **Config C/D (`SYSTEM_CONFIG in ("C", "D")`):** if `job_data` has a `proof_type`, **UNCONDITIONALLY OVERRIDE** — replace the LLM's input with the stored value. This is a true override (different from `check_status`'s fallback-on-default).
   - **Config A/B:** skip the Redis lookup; trust the LLM's input (or "auto").
   - Then call `_normalize_proof_type()` to canonicalize (lowercase → `"KS"`/`"LRT"`/`"logistic_accuracy"`).
6. **B6 gate — auto-detection:** if `proof_type_normalized == "auto"` after the step above:
   - **Config C/D:** probe all three COSMeTIC endpoints (`/jobs/{id}` on 5013 KS, 5014 LRT, 5012 logistic). If only one responds with a valid `job_id`, infer that proof type. If multiple respond, priority is **LRT > KS > logistic**. If none respond, default to logistic_accuracy.
   - **Config A/B:** return error JSON `"proof_type is required. Please specify KS, LRT, or logistic_accuracy. Auto-detection is not available in this configuration."` without probing.
7. Build `downloads_dir` from `DOWNLOADS_DIR` (default `/app/downloads`) and ensure it exists.
8. Compute basename:
   - If `output_filename` provided, use its basename and ensure `.zip` suffix.
   - Else `proofs-{job_id}.zip`.
9. Build absolute `output_filename = os.path.join(downloads_dir, filename_only)`.
10. Call `curl -L --max-time 120 -o output_filename {base_url}/jobs/{job_id}/download`.
11. Validate:
    - File exists and size > 0.
    - **File content does not look like a textual error/JSON error.** Reads the first 200 bytes as UTF-8; if the preview contains `"error"`, `"ok":false`, or `"unknown job"` (case-insensitive), COSMeTIC returned a JSON error page instead of a zip. The file is deleted and the tool returns `"API returned an error instead of proof file: {full_content_or_preview}"`. Binary zips fail the UTF-8 decode or don't match these substrings, so they pass through. This catches the common case where the prover API returns `{"error":"Unknown job id","ok":false}` with HTTP 200 and `Content-Disposition: attachment; filename=*.zip`, tricking `curl -o` into saving the error body as if it were a real zip.
12. Mark the job as downloaded in Redis via `context_manager.mark_job_downloaded`.
13. Return JSON string:
    - `{ "success": true, "job_id": ..., "filename": filename_only, "file_size": ..., "proof_type": ... }`.

**Historical bug fix (recorded for posterity):** An earlier version had a redundant normalization block in the `else` branch after the "auto" probe that re-matched lowercase strings. When the Redis override set `proof_type_normalized = "KS"` (uppercase), the `else` branch's lowercase-only matching failed and fell through to `"logistic_accuracy"`. This caused KS job downloads to hit port 5012 instead of 5013 and get "Unknown job id" errors. The `else` branch was replaced with `pass` since the normalization at [proverserver.py:524](proverserver.py#L524) already canonicalizes correctly via `_normalize_proof_type()`.

The backend's `/download_proof` route trusts this JSON, reconstructs the absolute path, and serves the file to the browser.

#### 4.6.3 `verify_proof`

Signature (simplified):

- Args:
  - `job_id: Optional[str]`.
  - `proof_type: str = "auto"`.
  - `session_id: Optional[str]`.
  - `user_id: Optional[str]`.

Purpose: confirm that a previously-completed proof job is cryptographically valid. Calls COSMeTIC's `/verify-job/{id}` endpoint, which runs `ezkl.verify` against every sub-proof for the job's `user_hash` and returns an aggregate result.

Behavior:

1. Resolve `session_id`; if missing, return `{"error": "session_id is required"}`.
2. Touch the session in Redis.
3. **B4 gate — resolve `resolved_job_id`** (same three-tier fallback as `download_proof`):
   - If `job_id` was explicitly provided → use it.
   - **Config B/C/D:** try `get_latest_completed_job_id(session_id)` → then `get_latest_job_id(session_id)` filtered by `status in {done, completed, success}` → otherwise `None`.
   - **Config A:** entire fallback block is skipped.
4. If still `None`, return `{"error": "job_id is required and no completed job found in session state"}`.
5. **B5 gate — resolve `proof_type`** (same logic as `download_proof`'s B5):
   - **Config C/D:** if Redis `job_data.proof_type` is set, **unconditionally override** the LLM's input.
   - **Config A/B:** trust the LLM's input (or `"auto"`).
6. **B6 gate — auto-detection:** if `proof_type_normalized == "auto"` after B5:
   - **Config C/D:** probe `/jobs/{id}` on all three COSMeTIC ports (5013 KS, 5014 LRT, 5012 logistic), apply priority **LRT > KS > logistic** when multiple match; default to `logistic_accuracy` if none match.
   - **Config A/B:** return error `{"error": "proof_type is required. Please specify KS, LRT, or logistic_accuracy. Auto-detection is not available in this configuration."}` without probing.
7. Build `url = "{base_url}/verify-job/{resolved_job_id}"` and `POST` with `_run_curl_command(url, "-X POST")`.
8. Response handling:
   - On `curl` non-zero exit → return `{"ok": false, "error": "verify request failed: ..."}`.
   - On JSON parse success → pass-through the upstream JSON unchanged (`{"ok": bool, "results": [{"kind": "ltr"|"mrp", "ok": bool, "proof": "...pf", "smt": "..."}, ...], "user_hash": "..."}`).
   - On JSON parse failure (e.g., upstream returned HTML 404 because the LLM specified the wrong port and `auto`-detection was disabled in Config A/B) → return the literal string `"Invalid JSON response: {stdout}"`. This raw surface is preserved on purpose for faithfulness-trap measurement — wrong-port 404 is the same shape as `check_status`'s wrong-port 404 surface.

**Upstream response shape note:** `/verify-job/` returns only `ok`, `results`, and `user_hash`. It does **not** echo back the `job_id` or `proof_type` that the tool sent. Downstream callers (Config D's `format_tool_response` template, `compute_trust_label`) read only the top-level `ok` field — they do not need the omitted fields. If a future caller needs `job_id` in the output, the tool layer would have to inject it into the parsed dict before serializing (currently it does not).

**Auth-bound by design.** Although `_resolve_user_id` is called implicitly via the `B4`/`B5`/`B6` stack's reliance on Redis state (populated by previous identity-bound submissions), `verify_proof` itself does not return an early "Please sign in first" error — it returns the more specific "no completed job" error when there is no resolvable job. The auth-required nature is enforced by the fact that there is no session state to fall back to without prior authenticated tool calls.

**Tool description (LLM-facing docstring) carefully written to avoid prior failure modes:** the docstring spells out the precondition ("Use after a proof job has completed; for in-progress jobs call `check_status` first"), the relationship to `download_proof` ("verification confirms cryptographic validity; download retrieves the artifact — these are independent operations"), and the auth-bound default behavior ("resolves the current user's latest completed job"). No copy-pasteable example values (which previously caused smaller LLMs to copy unquoted arrays into JSON-string fields).

### 4.7 Hash Existence Tools

#### 4.7.1 `check_hash_existence(user_hash, application=None)`

- Directly hits the COSMeTIC `check-hash` endpoint(s) to answer questions like:
  - “Is hash X used anywhere?”
  - “Is hash X used in KS / logistic accuracy / LRT?”
- Normalizes `application` into one of `logistic_accuracy`, `KS`, `LRT` or checks all three.
- For each app:
  - POSTs `{ "user_hash": user_hash }` to its `check-hash` URL.
  - On success:
    - Parses JSON, extracts `exists` and additional detail flags, and records them.
  - On error:
    - Stores a concise error reason.
- Returns a formatted markdown‑ish string summarizing:
  - Which applications the hash exists in.
  - Which ones it does not.
  - Any errors encountered while checking.
- Includes deterministic **"Proof follow-up options:"** lines per checked application, each containing a concrete user-facing command template (e.g. *"say: submit proof for my data in KS"*):
  - If exists: offer proving **was used** in that application with the explicit command.
  - If not found: offer proving **was not used** in that application with the explicit command.
  - If check errored/timed out: suggest direct proof submission for that application.
  - A final trailing line spells out the default nonce behavior (*"if you don't provide nonce, we use a default nonce automatically"*) and gives an explicit nonce-override prompt format. These deterministic suggestions are produced server-side in `check_hash_existence` (proverserver.py:1119-1152) and are part of C2 (deterministic guidance). The trust-label detector keys off the Summary line, not these follow-up lines — so adding/changing the follow-up wording does not affect `[Cryptographically provable]` detection.

#### 4.7.2 `check_my_hash_existence(application=None, session_id=None, user_id=None)`

- Preferred when the user is signed in and says “my hash / my data.”
- Behavior:
  - Resolves `user_id` and, if missing, asks the user to sign in.
  - Optionally touches Redis state using `session_id`.
  - Fetches the user’s raw hash via `get_primary_hash_for_user(user_id)`.
  - If present, delegates to `check_hash_existence` with that hash and optional application.

### 4.8 Session Inspection Tool: `get_session_context`

- Args:
  - `session_id` (string).
- Behavior:
  - Resolves session id and touches Redis.
  - Returns a pretty‑printed JSON string with:
    - `state` – aggregated state hash from Redis:
      - `latest_job_id`, `latest_completed_job_id`.
      - `latest_proof_type`, `latest_user_hash`.
      - `updated_at`.
    - `jobs` – array of job metadata for the session, sorted newest first.
    - `summary` – short textual summary string.
    - `recent_messages` – up to 10 stored chat messages. **In Config A this contains the actual conversation** (user/assistant turns written by `app.py`'s `save_message` calls). In Config B/C/D it's always empty.

The agent can call this when the user asks "what jobs do I have" or "tell me about my session."

**Tool description (LLM-facing docstring):** sharpened to explicitly say "Use this tool ONLY when the user explicitly asks about ALL their jobs" and "Do NOT use this tool for single-job status questions — use `check_status` instead." Without this clarification, the LLM conflates "check my status" (single job) with "show me all my jobs" (session dump) and picks the wrong tool, leading to verbose output containing hash values and internal state.

**Config D formatter scrubbing:** in Config D, `format_tool_response` for this tool reads the `jobs` array and `state.latest_completed_job_id` directly, building a clean `"You have N job(s): — Job X (KS proof): done"` list. It explicitly does NOT use the tool's `summary` field (which contains the raw `user_hash`) nor the `latest_user_hash` state field. This prevents user-hash leakage through Config D's direct routing.

---

## 5. Redis Context Layer (`context_manager.py`)

### 5.1 Purpose

`ContextManager` encapsulates all Redis interactions and enforces a simple data model:

- **State hash** – one per session (`session:{session_id}:state`).
- **Jobs hash** – one per session (`session:{session_id}:jobs`).
- **Messages list** – optional, one per session (`session:{session_id}:messages`).

All keys for a session share a configurable TTL (7 days).

### 5.2 Connection and Health

- Reads `REDIS_HOST` (default `localhost`) and `REDIS_PORT` (default `6379`).
- `decode_responses=True` — all values come back as `str` rather than `bytes` (simplifies JSON parsing).
- `socket_connect_timeout=5` — fail fast if Redis is unreachable instead of hanging the request thread.
- Attempts `ping()` at initialization:
  - On success, logs the connection.
  - On failure, logs a warning and sets `redis_client = None`, causing all methods to no‑op safely if Redis is down.
- `_is_connected()` health check — called at the start of every public method (`get_state`, `update_state`, `get_job`, etc.). If `redis_client is None` OR a fresh `ping()` fails, returns `False` and the caller short-circuits. This means **Redis going down mid-session** produces graceful degradation rather than exceptions: state reads return empty dicts, writes silently no-op. Flask continues to serve tool calls (which may then fail for different reasons when they can't find state).

### 5.3 Session Lifecycle

- `touch_session(session_id)`:
  - Deletes a set of **legacy keys** from a prior design (`hashes`, `proof_types`, `jobs:timeline`, `tool_calls`).
  - Applies `EXPIRE` with `TTL_SECONDS` (7 days) to state, jobs, and messages keys.
  - Called on any meaningful tool call to keep active sessions alive.

### 5.4 State Model

- `get_state(session_id)`:
  - Returns `hgetall("session:{sid}:state")`.

- `update_state(session_id, updates)`:
  - Filters out `None` values from the update dict.
  - Automatically adds/updates `updated_at` with the current timestamp.
  - Writes via `HSET`.
  - Refreshes TTL via `touch_session`.

- `get_latest_job_id(session_id)`:
  - Reads `latest_job_id` from state.

- `get_latest_completed_job_id(session_id)`:
  - Reads `latest_completed_job_id` from state.

- **Session ownership (Risk B mitigation)**:
  - `get_session_owner(session_id)` reads `owner_user_id` from the state hash.
  - `claim_or_verify_session_owner(session_id, user_id)` ensures the state hash contains
    `owner_user_id=user_id`. If the session is unclaimed, it claims it; if already claimed
    by a different user, it returns `(False, "mismatch")`.

### 5.5 Job Model

Jobs are stored in a Redis hash:

- Key: `session:{session_id}:jobs`.
- Field: `job_id`.
- Value: JSON serialized dict:
  - `job_id`, `proof_type`, `user_hash`, `nonce`.
  - `status` (e.g. `submitted`, `running`, `done`, `error`).
  - `created_at`, `updated_at`, and optionally `downloaded_at`.

Operations:

- `get_job(session_id, job_id)`:
  - Returns parsed JSON job dict or `None`.

- `get_job_ids(session_id)`:
  - Reads all job entries and returns them as an array, sorted by `created_at` descending.

- `add_job_id(session_id, job_id, proof_type, user_hash, nonce, status)`:
  - Upserts a job:
    - Preserves `created_at` if the job already exists.
    - Updates `status`, `nonce`, and `updated_at`.
  - Updates state:
    - `latest_job_id`, `latest_proof_type`, `latest_user_hash`.
    - If status is a terminal success (`done/completed/success`), also `latest_completed_job_id`.
  - Refreshes TTL via `touch_session`.

- `update_job_status(session_id, job_id, status)`:
  - Updates an existing job’s status and `updated_at`.
  - If the job is missing, creates a minimal “unknown” job record and then returns.
  - Updates state, and if the new status is success, updates `latest_completed_job_id`.

- `mark_job_downloaded(session_id, job_id)`:
  - Sets `downloaded_at` and refreshes `updated_at` for the job.
  - Refreshes TTL via `touch_session`.

### 5.6 Messages and Summary

**Usage:**
- **Config A:** `save_message` and `get_recent_messages` are actively used by `app.py`'s `/get` endpoint to build and replay conversation history across turns. Each `/get` request first reads history via `get_recent_messages(session_id, limit=15)`, then saves both the user message and the final assistant response via `save_message`.
- **Config B/C/D:** no code calls `save_message`. `get_recent_messages` is still called by `get_session_context` but always returns an empty list since nothing writes to the messages key.

- `save_message(session_id, role, content)`:
  - Saves a short chat message (role + content + timestamp) in a Redis list, trimming to the latest `MESSAGE_LIMIT` (15).

- `get_recent_messages(session_id, limit)`:
  - Retrieves the last `limit` messages from the list and parses them as JSON.

- `get_context_summary(session_id)`:
  - Returns a deterministic string summarizing:
    - Latest job id, proof type, hash, and status.
    - Latest completed job id (or “none”).
  - Used inside the `get_session_context` tool.

### 5.7 Cleanup

- `cleanup_session(session_id)`:
  - Deletes the state, jobs, and messages keys for a session.
  - Not currently called by any route or tool, but available for future admin utilities.

---

## 6. Account and Dataset Layer

### 6.1 Database Schema (`sql/002_dataset_and_zips.sql`)

Two main tables:

- `input_zip_archives`:
  - `id` (UUID primary key, default `gen_random_uuid()`).
  - `source_url` – origin of the zip.
  - `stored_path` – local path of the zip file.
  - `file_size_bytes` – size in bytes.
  - `fetched_at` – timestamp of ingestion (defaults to `NOW()`).

- `dataset_users`:
  - `id` (UUID primary key).
  - `username` (`TEXT UNIQUE NOT NULL`) – generated as `User1`, `User2`, ...
  - `password_hash` (`TEXT NOT NULL`, bcrypt).
  - `raw_hash` (`TEXT NOT NULL`) – main per‑user hash.
  - `input_data` (`JSONB`) – the dataset’s `input_data` for this hash.
  - `source_file` (`TEXT NOT NULL`) – original JSON file name inside the zip.
  - `zip_archive_id` (`UUID` FK to `input_zip_archives(id)`).
  - `created_at` / `updated_at` timestamps.
  - Indices on `username`, `raw_hash`, and `zip_archive_id`.

### 6.2 `account_store.py`

Purpose: small Postgres helper module for auth and user‑centric operations.

Key functions:

- `_get_conn()`:
  - Reads `DATABASE_URL`; raises if missing.
  - Returns a `psycopg2` connection.

- `signin_dataset_user(username, password_plaintext)`:
  - Validates the username and password.
  - Looks up `dataset_users` by `username`.
  - Verifies `password_hash` with `bcrypt.checkpw`.
  - Returns:
    - `{"success": True, "user": {"id": "<uuid>", "username": "<name>"}}` on success.
    - `{"success": False, "error": "Invalid username or password"}` on failure.

**Empty-string `raw_hash` convention:** the schema requires `raw_hash TEXT NOT NULL`, so "no hash" is represented by **empty string `''`**, not NULL. `get_primary_hash_for_user` returns `None` in both cases ("row missing" and "row present but raw_hash falsy"), because the return expression is `str(row["raw_hash"]) if row and row["raw_hash"] else None` — the empty-string check collapses into the same `None` branch as the missing-row case. Downstream callers (`prove_my_data`'s `if not saved_hash:` at [proverserver.py:950](proverserver.py#L950), `prove_hash`'s `if not saved:` at [proverserver.py:232](proverserver.py#L232), etc.) use a falsy check so both `None` and `""` produce the same "No hash saved" error string. This convention is what the test runner uses for `has_hash: false` setup (`UPDATE dataset_users SET raw_hash = '' WHERE username = 'User1'`).

- `get_user_by_id(user_id)`:
  - Fetches `id` and `username` from `dataset_users`.

- `get_primary_hash_for_user(user_id)`:
  - Returns `raw_hash` or `None` if missing.

- `get_input_data_for_user(user_id)`:
  - Returns `input_data` JSON, or `None` if missing.
  - **Note:** This function is currently unused — no code in `app.py` or `proverserver.py` calls it. It exists for potential future use.

- `get_masked_hash_for_user(user_id)`:
  - Fetches `raw_hash` and converts to a masked preview via `mask_hash`:
    - `<= 4` characters: fully masked with `*`.
    - `5–12` characters: shows first 2 and last 2 (e.g., `ab...cd`).
    - `> 12` characters: shows first 6 and last 6 (e.g., `abcdef...uvwxyz`).

These helpers are used by both the backend (for UI) and the prover (for account‑aware tools).

### 6.3 Dataset Sync (`dataset_sync.py`)

Purpose: fetch the COSMeTIC **input‑files zip** and reconcile it into `dataset_users` and `input_zip_archives`.

Flow of `run_dataset_sync()`:

1. Read and validate `INPUT_FILES_ZIP_URL`.
2. Determine `INPUT_ZIPS_DIR` (default `/app/data/input_zips`), create if needed.
3. Determine a shared dataset password:
   - `DATASET_DEFAULT_PASSWORD` env or `"password123"`.
   - Bcrypt‑hash it once for reuse.
4. Download the zip (with a custom User-Agent and a 120s timeout).
5. Write it to disk under `INPUT_ZIPS_DIR` as `input_files_<timestamp>.zip`.
6. Insert a row into `input_zip_archives` with source URL, path, file size, and `fetched_at`, capturing `zip_archive_id`.
7. Iterate zip contents:
   - For each file whose basename matches `input_<raw_hash>.json`:
     - Load JSON and read `input_data`.
     - Add `(source_file, raw_hash, input_data)` to an in‑memory list.
8. Upsert into `dataset_users`:
   - For each tuple, query `dataset_users` by `raw_hash`:
     - If a row exists:
       - Update `input_data`, `source_file`, `zip_archive_id`, `updated_at`.
     - If no row:
       - Compute `next_num` as `MAX(username suffix) + 1` for `UserN` pattern.
       - Insert a new row with:
         - `username="User{next_num}"`.
         - `password_hash` = shared bcrypt password.
         - `raw_hash` = extracted hash.
         - `input_data` = JSON.
         - `source_file`, `zip_archive_id`.
9. Commit and return a summary dict:
   - `{"success": True, "created": <int>, "updated": <int>, "zip_path": "<path>", "message": "...", "errors": [...]}`.

This pipeline is invoked via the backend route `POST /admin/sync-dataset`, which is protected by the `ADMIN_SYNC_SECRET` header or form param.

---

## 7. Frontend (`templates/index.html` + `static/style.css`)

### 7.1 UI Overview

The frontend is a single‑page, Bootstrap‑based UI. All styling is **inline** in a `<style>` block inside `templates/index.html`; the repo also ships `static/style.css`, but `index.html` does **not** link it, so that file is currently unused/dead:

- A header with:
  - Assistant avatar and intro text.
  - An auth panel (username/password form, sign‑in status, masked hash display).
- A single `Chat` tab:
  - Scrollable message history (`#messageFormeight`).
  - Initial welcome message from the assistant.
- A footer input area:
  - Text input for the message.
  - Send button.

All JavaScript is inline and uses jQuery.

### 7.2 Session ID

On page load, the client generates a unique session id:

- Format: `session_<timestamp>_<random-9-char-string>`.
- This `sessionId` is:
  - Sent with **every** chat request to `/get`.
  - Sent with proof download requests to `/download_proof`.
  - Used by Redis and the prover server as the **primary key** to group jobs and state.

### 7.3 Auth UX

- Sign‑in form:
  - AJAX `POST /auth/signin` with JSON `{username, password}`.
  - On success:
    - Stores `authUser` in JS.
    - Hides the form and shows a “signed in as” badge and logout button.
    - Calls `GET /api/me/hash` to display `Saved hash: <masked>`.
  - On failure:
    - Shows an error message in the header.

- Logout:
  - `POST /auth/logout`, then:
    - Clears `authUser`.
    - Hides the signed‑in panel and restores the sign‑in form.

The client also calls `GET /auth/me` on page load to initialize auth state, so existing login sessions are recognized.

### 7.4 Chat Flow

When the user submits a message:

1. The code:
   - Validates the text is not empty.
   - Stores the raw text into a global `lastUserMessageText` for downstream download button logic.
   - Appends a right‑aligned “user” bubble with the message and timestamp.
2. It appends a left‑aligned temporary “Thinking...” bubble with a spinner.
3. It sends an AJAX `POST /get` with:
   - `msg` = user text.
   - `session_id` = generated sessionId.
4. On success:
   - Removes the spinner.
   - Parses the response as JSON (`{"response": "...", "error": "...", ...}`) or falls back to treating it as plain text.
   - Calls `addBotMessage()` with the final bot text.
5. On failure:
   - Removes the spinner.
   - Displays a generic error or error text from the response.

### 7.5 Download Button and Job ID Detection

The frontend tries to make downloads user‑friendly:

- `extractJobId(text)`:
  - First looks for patterns like `job id: 123`, `jobid 123`, `job ID is 123`.
  - Fallback: any 13+ digit number (job IDs tend to look like timestamps).

- `isExplicitDownloadRequest(msg)` and related regexes:
  - Look for phrases like “download it”, “download the proof”, “get the proof file”, etc.

- `addBotMessage(msg)`:
  - Adds the bot bubble for the given `msg`.
  - Attempts to extract a `jobId` from the text.
  - If a job id is found **and** the previous user message was an explicit download request:
    - Adds a “Download Now” button inside a “download‑container” region under that message.

- `downloadProofFile(jobId, buttonEl)`:
  - Sends a `POST /download_proof` with:
    - `job_id` (from the detected job).
    - `session_id` (same as chat).
  - Uses `xhrFields.responseType = 'blob'` to receive a binary blob.
  - Checks `Content-Type`:
    - If JSON → error; parse and display the `error` field if present.
    - If non‑JSON blob and size > 0:
      - Constructs a filename from the `Content-Disposition` header or defaults to `proofs-{jobId}.zip`.
      - Uses `URL.createObjectURL` to trigger browser download.
  - Updates the button label and color to indicate success or error.

The backend/unified `DOWNLOADS_DIR` contract ensures the prover’s downloaded file is visible to Flask, so this flow works across containers.

---

## 8. Local Development Helper

Removed from the release; run the stack with `docker compose up -d` (see README).

---

## 9. Configuration Reference

Key environment variables (non‑exhaustive):

- **LLM / agent**
  - `DEEPINFRA_API_KEY` – required for DeepInfra access in `app.py` via the OpenAI-compatible endpoint at `https://api.deepinfra.com/v1/openai`.
  - `LLM_BASE_URL` – OpenAI-compatible endpoint (default `https://api.deepinfra.com/v1/openai`); recorded by the test runner in every result.
  - `LLM_MODEL` – HuggingFace-style model id passed to `ChatOpenAI` (read via `os.getenv("LLM_MODEL", "Qwen/Qwen3-32B")`). Default is `Qwen/Qwen3-32B` — the primary research model. Cross-model evaluation runs have used `meta-llama/Meta-Llama-3-8B-Instruct` and `mistralai/Mistral-Small-3.2-24B-Instruct-2506`. When the value contains `"llama"` (case-insensitive) the backend wires an additional tool-use system prompt into the ReAct agent; otherwise no system prompt is set (see §3.3). The docker-compose backend service declares the default as `LLM_MODEL=${LLM_MODEL:-Qwen/Qwen3-32B}`.

- **Flask / backend**
  - `FLASK_PORT` – port for Flask to listen on (default `5001`).
  - `SECRET_KEY` – Flask session signing key.

- **MCP / prover**
  - `PROVER_MCP_HOST` – FastMCP bind host (default `0.0.0.0`).
  - `PROVER_MCP_PORT` – FastMCP bind port (default `8003`).
  - `PROVER_MCP_URL` – URL the backend uses to reach the prover (e.g. `http://proverserver:8003/mcp`).

- **COSMeTIC APIs**
  - `PROVER_BASE_HOST` – hostname for COSMeTIC prover services (default `COSMeTICprover`). This is the only env var used by code to derive COSMeTIC API URLs.
  - `PROVER_BASE_URL` – **removed.** This was previously set in docker-compose but read by no Python code; the prover derives COSMeTIC URLs from `PROVER_BASE_HOST` instead.
  - `INPUT_FILES_ZIP_URL` – URL of the zip containing dataset input files (e.g. `http://COSMeTICprover-input-files:5015/input-files/zip`).

- **Storage / data**
  - `DOWNLOADS_DIR` – local path where the prover writes zip files and the backend reads them (default `/app/downloads`).
  - `INPUT_ZIPS_DIR` – local directory where input‑files zips are stored (default `/app/data/input_zips`).

- **Redis**
  - `REDIS_HOST` – Redis hostname (`redis` in Docker, `localhost` locally).
  - `REDIS_PORT` – Redis port (`6379`).

- **Postgres**
  - `DATABASE_URL` – DSN for Postgres (e.g. `postgresql://mcp_user:mcp_password@postgres:5432/mcpdb`).

- **Dataset / auth**
  - `DATASET_DEFAULT_PASSWORD` – default dataset user password; used once per dataset sync to generate bcrypt hashes (default `"password123"`).
  - `ADMIN_SYNC_SECRET` – optional secret to protect the dataset sync admin endpoint.

- **Evaluation**
  - `EVAL_MODE` – when `"true"`, enables detailed tool call logging in the interceptor and extends the `/get` response with `backed_by_cosmetic` and `tool_calls` fields. **App default is `"false"`** (`os.getenv("EVAL_MODE", "false")` in `app.py`), but **docker-compose default is `"true"`** (`EVAL_MODE=${EVAL_MODE:-true}`). So: running via docker-compose enables eval mode unless the `.env` file explicitly sets `EVAL_MODE=false`. **Automatically forced `true` when `CONFIG=D`** regardless of env var (Config D needs tool responses captured to implement its routing).

- **Ablation Study**
  - `CONFIG` – selects one of 4 ablation configurations: `A` (naive, most features off), `B` (stateless + Redis, smart fallbacks off), `C` (current default, all features on), `D` (deterministic tool-response routing instead of LLM summarization). Default `"C"`. Plus one **supplementary variant**: `A_STAR` (identity-in-prompt; same A3-off + conversation-history-on as Config A, with an added system message naming the authenticated `user_id` to test whether prompt-level identity hints can substitute for header-injected identity protection). **MUST be set identically on both `backend` and `proverserver` services** — both read it via `SYSTEM_CONFIG = os.getenv("CONFIG", "C")` at module load. See Section 12 for the full feature matrix.

---

## 10. End-to-End Flows

### 10.1 User Submits a Proof Job

1. User types a request in the chat UI, e.g.:
   - “Submit a proof for hash X in KS.”
   - “Prove my data in LRT.”
2. Frontend:
   - Sends `POST /get` with `msg` and the current `session_id`.
3. Backend (`app.py`):
   - Resolves the current user from session (if logged in).
   - Sets `current_session_id_var` and `current_user_id_var`.
   - Calls the LangGraph agent with a single user message.
4. Agent:
   - Interprets the request and chooses the appropriate tool:
     - `prove_my_data` if the user is signed in and used “my” language.
     - `prove_hash` if the user gave an explicit hash.
   - Calls the tool via MCP; the interceptor injects session/user IDs.
5. Prover (`proverserver.py`):
   - Resolves `session_id` and `user_id`.
   - Loads user hash from Postgres as needed (via `get_primary_hash_for_user`).
   - Normalizes `proof_type`, builds the correct COSMeTIC endpoint.
   - Calls the `prove-hash` API via `curl`.
   - If a `job_id` is returned:
     - Records job metadata in Redis via `context_manager.add_job_id`.
6. Tool returns a JSON string with job id and status; the agent formats a natural-
   language answer.
7. Backend converts the agent’s final message to JSON and sends it to the browser.
8. The frontend renders the response; if the user also asked to “download” and a job ID is present in the bot text, it shows a “Download Now” button under that message.

### 10.2 User Checks Job Status

1. User asks:
   - “What’s the status of my job?”
   - “Check the status of job 123.”
2. Agent chooses `check_status`:
   - Without a `job_id` argument to check the latest job for the current session.
   - Or with a specific `job_id` if provided by the user.
3. Prover:
   - Resolves `session_id` and warms Redis.
   - Determines the job id and proof type (from args or job metadata).
   - Calls `GET /jobs/{job_id}` on the right COSMeTIC port.
   - Parses the JSON response and updates `context_manager.update_job_status`.
   - Returns the full JSON back to the agent.
4. Agent summarizes the status and returns it as a chat response.

### 10.3 User Downloads a Proof

1. Typical path:
   - User first asks to submit a proof.
   - Later says “download the proof” or clicks the “Download Now” button.
2. If the user uses the **button**:
   - The frontend:
     - Extracts `jobId` from the bot message text.
     - Sends a `POST /download_proof` with `job_id` and `session_id`.
3. Backend (`/download_proof`):
   - Locates MCP tool `download_proof`.
   - Sets `current_session_id_var` and `current_user_id_var`.
   - Calls `download_proof.ainvoke({"output_filename": "proofs-{jobId}.zip", "job_id": jobId})`.
4. Prover:
   - Resolves session and job id (falling back to latest completed job if `job_id` is absent).
   - Determines the appropriate COSMeTIC endpoint (auto‑detect if necessary).
   - Downloads the proof file to `DOWNLOADS_DIR`, validates it, and marks the job as downloaded.
   - Returns a JSON success payload with the relative `filename`.
5. Backend:
   - Rebuilds the absolute path in the shared downloads volume.
   - Validates the path and existence.
   - Streams the file to the browser via `send_file`.
6. Frontend:
   - Receives the blob and triggers a browser download.
   - Updates the button to indicate success or failure.

### 10.3b Cross-cutting Runtime Flow (low-level)

For an observer who wants to trace exactly what happens when a user types a message and hits Send, here's the full stack trace in order:

1. **Browser** — JS captures the input, POSTs to `/get` with JSON `{msg, session_id}`. Cookie sent (Flask session) carries `user_id` if signed in.
2. **Flask `/get` handler (`app.py:469`)** — reads msg, resolves user from session via `_current_user()`, defaults `session_id` to `"default"` if missing.
3. **Session ownership claim** — `claim_or_verify_session_owner` writes `owner_user_id` to Redis (first time) or verifies it matches (subsequent times).
4. **Fresh asyncio event loop created** — `asyncio.new_event_loop()` + `asyncio.set_event_loop(loop)`. Flask is synchronous; each request gets its own loop. **Do not parallelize requests against the same Flask worker — contextvars are shared.**
5. **Message list construction** — Config A reads history from Redis via `get_recent_messages`; others use `[{user msg}]`.
6. **Contextvars set** — `current_session_id_var`, `current_user_id_var`, `mcp_tracker_var = {"touched": False}`.
7. **`agent.ainvoke(...)` wrapped in `asyncio.wait_for(timeout=420.0)`** — LangGraph ReAct loop runs; may invoke 0-N MCP tools.
8. **Per-tool-invocation (inside the agent loop):**
   a. Agent decides to call a tool with arguments.
   b. LangChain MCP adapter invokes `SessionIdInjectorInterceptor.__call__(request_data, handler)`.
   c. Interceptor sets `tracker["touched"] = True`, captures `tool_call_record` (EVAL_MODE only).
   d. If no session_id AND no user_id in contextvars → calls `handler(request_data)` unmodified.
   e. Else: mutates args/headers per config (strip `user_id`, inject `x-session-id`, inject `x-user-id`), calls `handler(corrected_request)`.
   f. `handler` calls `MultiServerMCPClient.session("prover").call_tool(name, args)` which HTTP-POSTs to `proverserver:8003/mcp`.
   g. FastMCP on prover dispatches to the matching `@mcp.tool()` function.
   h. Tool reads `session_id` from header/arg, resolves `user_id` from header (or arg in Config A), touches Redis, does its work (may `curl` COSMeTIC API), writes Redis, returns text.
   i. Interceptor receives raw `CallToolResult`, extracts text via `_extract_tool_response_text`, captures into `tool_call_record["response"]`.
   j. Agent receives text as tool output, LLM decides next step.
9. **Agent produces final assistant message.**
10. **`bot_response = response['messages'][-1].content`** — last message extracted. If empty, walks backward for fallback text.
11. **Strip `<think>…</think>` blocks** — Qwen3 reasoning markers removed via `re.sub` before anything downstream touches the text.
12. **Config A: save history** — `save_message(user)` + `save_message(assistant)` to Redis (post-strip, so history is clean).
13. **Config D: replace bot_response** — if tool was called, run `format_tool_response(last_tool.name, last_tool.response)` over the last captured tool response.
14. **Trust label appended** — `[Backed by COSMeTIC prover]` iff `mcp_tracker["touched"]`.
15. **Response payload built** — `{response}` or `{response, backed_by_cosmetic, tool_calls}` if EVAL_MODE.
16. **Contextvars reset in finally block** — `token.reset()` for all three.
17. **Event loop closed.**
18. **Flask returns JSON string as HTTP 200** (always 200 in the normal path; error paths return 400/403/503/500 as noted).

### 10.4 User Asks About "My Hash"

1. User signs in with dataset credentials (`UserN` + shared password).
2. UI shows a masked preview of their saved `raw_hash` upon calling `/api/me/hash`.
3. Later, user asks:
   - “Where is my hash used?”
   - “Check if my hash is used in LRT.”
4. Agent:
   - Recognizes “my hash / my data” semantics with an authenticated user.
   - Calls `check_my_hash_existence(application=...)` rather than asking the user to paste their hash.
5. Prover:
   - Resolves `user_id` from MCP headers.
   - Loads the user’s `raw_hash` from Postgres.
   - Delegates to `check_hash_existence` with that hash and optional application.
6. Results are returned and summarized to the user in the chat.

Current response behavior:

- The hash-existence response also includes deterministic proof-next-step examples for
  `logistic_accuracy`, `KS`, and `LRT`, including the "not used" proof path when an app is not found.
- Proof submissions no longer offer unsupported notification workflows; users are guided to manual status checks.

Performance note:

- `check_hash_existence` checks up to **three** COSMeTIC `check-hash` endpoints (logistic, KS, LRT).
  These calls are performed **sequentially**, and each one can wait up to the configured curl timeout
  (currently 120 seconds for `check-hash`). If one backend (commonly KS on port 5013) is slow, the
  overall chat response can be noticeably delayed even if the final answer is correct.

---

## 11. Mental Model Summary

- **Flask backend**: routes + agent host + MCP client; primarily keeps HTTP state and user sessions, and enforces session ownership by binding `session_id -> owner_user_id` in Redis.
- **MCP prover**: the "brain" that interacts with COSMeTIC APIs and own all Redis state.
- **Redis**: authoritative state for jobs and per‑chat session context.
- **Postgres**: authoritative state for dataset users, their hashes, and their input data.
- **Frontend**: a rich but simple chat UI that glues everything together, including a one‑click "download proof" UX.

---

## 12. Ablation Study CONFIG Mechanism

The system supports an ablation study via a single `CONFIG` env var (values: `A`, `B`, `C`, `D`) that toggles specific architectural features on and off. Config C is the default/current system; A, B, D are subtractive or substitutive variants. Implementation lives in `app.py` (`SYSTEM_CONFIG = os.getenv("CONFIG", "C")`) and `proverserver.py` (same read). **Both services must see the same value** (set via docker-compose env block or `.env`).

### 12.1 Configuration Philosophies

Each config represents a coherent architectural stance, not an arbitrary feature subset:

- **Config A — "Trust the LLM."** Conversation history is persisted and replayed; identity protection is off (LLM-supplied `user_id` reaches the tool); no Redis fallbacks for job id or proof type; download auto-detection is off. This is how most chatbot tutorials and starter templates work.
- **Config B — "Distrust the LLM's memory."** Stateless (no conversation history), identity protection on, Redis fallback job id on, but **no smart Redis features**: `check_status` doesn't consult stored proof_type on default, `download_proof` doesn't unconditionally override with Redis proof_type, and auto-detection probing is off.
- **Config C — "Full skeptical tools."** Current system. All 22 design decisions active.
- **Config D — "Remove LLM from response path."** Same as C for tool invocation, but the user-facing text comes from a deterministic formatter (`format_tool_response`) applied to the last tool's raw response instead of the LLM's summary. LLM still runs (tokens still paid) but its final text is discarded.
- **Config A_STAR (supplementary) — "Identity in the prompt, not the header."** Same toggles as Config A (A3 off, conversation history on, no Redis fallbacks). The extra change: when constructing the message list for the agent, the backend prepends a `system` message of the form *"The currently authenticated user has user_id: {uuid}. When calling any tool that accepts a user_id parameter, you MUST pass this exact value."* (`app.py:502-510`). This tests the hypothesis that *prompt-level identity scaffolding* can recover the auth safety lost by turning A3 off — without re-enabling the header-injection mechanism. Used in a supplementary cross-cut in the paper, not in the main A/B/C/D matrix.

### 12.2 Feature Matrix

| Feature | Where | Config A | Config B | Config C | Config D | Config A_STAR |
|---|---|---|---|---|---|---|
| Conversation history (save + replay via Redis messages) | `app.py` `/get` | ON | off | off | off | ON |
| Identity stripping (`user_id` args removed, `x-user-id` header injected) | `app.py` interceptor + `proverserver.py` `_resolve_user_id` | off | ON | ON | ON | off |
| Identity-in-prompt system message ("the authenticated user_id is {uuid}…") | `app.py` `/get` message construction | off | off | off | off | **ON** |
| B4: Fallback `job_id` from Redis (`check_status` + `download_proof` + `verify_proof`) | `proverserver.py` | off | ON | ON | ON | **ON** |
| B5: Stored `proof_type` from Redis (both `check_status` fallback-on-default AND `download_proof`/`verify_proof` unconditional override) | `proverserver.py` | off | off | ON | ON | off |
| B6: Auto-detection probing (three-port probe in `download_proof` and `verify_proof`) | `proverserver.py` | off | off | ON | ON | off |
| LLM summarizes response | `app.py` `/get` | ON | ON | ON | off (deterministic formatter) | ON |
| `EVAL_MODE` tool_call logging | `app.py` interceptor + `/get` | env-gated | env-gated | env-gated | **forced true** | env-gated |

### 12.3 Code-level Gate Locations

Quick reference for where each gate lives in code:

| Gate | File | Behavior |
|---|---|---|
| Identity header injection + args strip | `app.py` `SessionIdInjectorInterceptor.__call__` | Skipped when `SYSTEM_CONFIG in ("A", "A_STAR")` |
| `_resolve_user_id` fallback to argument | `proverserver.py` `_resolve_user_id` | Only when `SYSTEM_CONFIG in ("A", "A_STAR")` |
| Conversation history read | `app.py` `/get` message construction | Only when `SYSTEM_CONFIG in ("A", "A_STAR")` |
| Conversation history write | `app.py` `/get` after bot_response extracted | Only when `SYSTEM_CONFIG in ("A", "A_STAR")` |
| Identity-in-prompt system message insertion | `app.py` `/get` message construction | **Only when `SYSTEM_CONFIG == "A_STAR"`** (Config A_STAR exclusive) |
| B4 `check_status` job_id fallback | `proverserver.py` `check_status` | Skipped when `SYSTEM_CONFIG == "A"` (and A_STAR inherits this — `check_status`/`download_proof`/`verify_proof` gate on `SYSTEM_CONFIG != "A"`, treating A_STAR like B/C/D for fallback purposes)* |
| B4 `download_proof` / `verify_proof` job_id fallback | `proverserver.py` `download_proof`, `verify_proof` | Skipped when `SYSTEM_CONFIG == "A"` |
| B5 `check_status` proof_type fallback-on-default | `proverserver.py` `check_status` | Only when `SYSTEM_CONFIG in ("C", "D")` |
| B5 `download_proof` / `verify_proof` proof_type unconditional override | `proverserver.py` | Only when `SYSTEM_CONFIG in ("C", "D")` |
| B6 auto-detection probing | `proverserver.py` `download_proof`, `verify_proof` | Only when `SYSTEM_CONFIG in ("C", "D")`; otherwise returns error |
| Config D tool-response routing | `app.py` `/get` after bot_response extracted | Only when `SYSTEM_CONFIG == "D"` AND a tool was called |
| `EVAL_MODE` auto-force | `app.py` module load | `EVAL_MODE = env or SYSTEM_CONFIG == "D"` |

*Note: B4/B5/B6 in the proverserver gate on `SYSTEM_CONFIG != "A"` (or `in ("C","D")`), which means Config A_STAR is treated like Config A for identity (no header injection, A3 off) but is NOT in the proverserver gates that look for `"A"` specifically. The supplementary A_STAR design is a hybrid: A's identity behavior + B's-or-greater fallback behavior + a prompt-level identity hint. This is intentional — the paper measures whether prompt scaffolding alone can substitute for header-injected identity protection.

### 12.4 What Stays Unchanged in ALL Configs

The paper's design describes 22 decisions (A1–A4, B1–B7, C1–C5, D1–D4). `CONFIG` only toggles 6 of those (the matrix above). The remaining 16 stay active in every config because disabling them would either break the system or be measured differently. **Do NOT gate these on CONFIG:**

- **A1 (proof-type normalization)** — `_normalize_proof_type()` always runs.
- **A2 (default nonce injection `[[0.0, 0.0, 0.0]]`)** — applied when the LLM omits a nonce; user-supplied nonces flow through unchanged. Disabling the default would break unguided proof submission.
- **A4 (per-proof-type payload construction, e.g. `smt_list` for LRT)** — always applied.
- **B2 (Redis session state)** — always used; even Config A writes jobs to Redis (just doesn't read them back for fallbacks).
- **B3 (two-tier freshness: live COSMeTIC API vs cached Redis state)** — always enforced by `check_status` calling live API.
- **B7 (`prove_my_data` abstraction — resolving hash from Postgres)** — always active; not toggleable. This means even Config A's `prove_my_data` tries to fetch the hash from Postgres *using the LLM-supplied user_id fallback*.
- **C1–C5 (output integrity: trust labels — 4 tiers `[Cryptographically verified]` / `[Cryptographically provable]` / `[Backed by COSMeTIC prover]` / `[Not backed by COSMeTIC prover]` via `compute_trust_label()` in `app.py` — deterministic guidance in `check_hash_existence`, capability suppression e.g. no notification promises, structured error messages, path-traversal protection in downloads)** — always on across all configs. The trust label is **not** ablated; it is a fixed UX/integrity feature, not a measured architectural decision.
- **D1–D4 (security boundaries: session ownership claim, dataset-driven hash storage, masked hash display via `mask_hash`, download path validation via `commonpath`)** — always on.

### 12.5 Operational Notes

- **Switching configs requires a docker restart** because `SYSTEM_CONFIG` is read at module load.
- **Config D implies EVAL_MODE=true.** This is enforced at module load in `app.py`: `EVAL_MODE = os.getenv("EVAL_MODE", "false").lower() == "true" or SYSTEM_CONFIG == "D"`.
- **Config A conversation history bypasses tool-call awareness.** `save_message` only stores plain `{role, content}` pairs, so when history is replayed, the LangGraph agent doesn't know tools were invoked in prior turns. This is an intentional limitation — the ablation is "give the LLM its memory back", not "replay the full agent graph."
- **Config D formatter for `get_session_context` deliberately scrubs internal state** (hashes, timestamps, internal keys). Only shows job ids, proof types, and statuses.
- **`prove_hash`'s implicit hash resolution from Postgres** (at `proverserver.py:221-234`, inside the `prove_hash` body's "Resolve user_hash: prefer explicit value, otherwise fall back to signed-in user's saved hash" block) is NOT gated on CONFIG. It stays on in all configs as part of B7. This means Config A's `prove_hash` still loads saved hashes from Postgres when the LLM omits the `user_hash` argument.
- **Config A_STAR design intent:** A_STAR's gate behavior is asymmetric by design. For identity (A3) it behaves like Config A — no header injection, no args strip, `_resolve_user_id` falls back to the LLM-supplied argument. For server-side fallbacks (B4 in particular) it behaves like Config B/C/D — `SYSTEM_CONFIG != "A"` is true, so Redis-backed `latest_job_id` / `latest_completed_job_id` lookups fire normally. B5/B6 stay off (those gate on `in ("C", "D")`). The supplementary system prompt instructing the LLM to pass the authenticated `user_id` is inserted only in A_STAR (`app.py:502-510`). Net effect: A_STAR isolates the question *"if we tell the LLM its identity via prompt instead of injecting it via header, does the system stay safe?"* — without re-introducing the full skeptical-tools stack.

---

## 13. Test Suite (`eval/test_suite.json`)

Static test corpus used for ablation grading. The file contains **80 gradable entries**, of which **75 are executable across 7 source types** (1, 2, 3b, 4, 5, 6, 7). The Source 3a queries ("run it again", "do the same thing", etc.) appear in the JSON **both** as five standalone entries (`S3a_01`–`S3a_05`) **and** embedded inside Source 6 Scenario 5 (`S6_SC5_02` is the "run it again" step). The five standalone `S3a_*` entries are `mode: "sequential"` but carry **no `scenario` field**, so the test runner's `categorize()` treats them as orphans and never executes them (see §14.7). Only the embedded `S6_SC5_02` version is actually run — which is why the executed total is 75 even though the file holds 80 gradable entries.

### 13.1 Structure

Each entry is one of:
- A `_comment` marker (skipped by the runner).
- A test case with fields:
  - `id` (e.g. `S1_01`, `S6_SC1_02`).
  - `source` (int or string: `1`, `2`, `"3a"`, `"3b"`, `4`, `5`, `6`, `7`).
  - `mode`: `"isolated"` (fresh session_id per test) or `"sequential"` (shared session_id across all steps of a scenario).
  - `query` (the user-visible prompt string).
  - `user_state` (dict describing setup: `logged_in`, `has_hash`, `has_jobs`, `has_completed_job`).
  - `expected_tool` (string or `"NONE"` if no tool should be called).
  - Optional: `expected_params`, `should_contain` (substrings), `should_not_contain`, `should_contain_any_redirect`, `acceptable_tools` (for ambiguous queries), `must_call_tool`, `check_nonce_injected`, `normalization_test`, `runtime_substitution`, `scenario`, `step`.

### 13.2 Source Type Purposes

| Source | Count | Mode | Purpose |
|---|---|---|---|
| 1 | 23 | Isolated | Normal operations — the right tool is picked for clear requests |
| 2 | 10 | Isolated | Graceful failures — helpful errors vs crashes/hallucinations |
| 3a | 5 in file / 0 run | — | Context-dependent ambiguous ("run it again"). Present in the JSON as standalone `S3a_01`–`S3a_05` but **not executed** (orphans — no `scenario` field); the only run instance is the embedded step 2 of Source 6 Scenario 5 (`S6_SC5_02`) |
| 3b | 5 | Isolated | Context-free ambiguous ("check it") — any tool in `acceptable_tools` counts |
| 4 | 5 | Isolated | Impossible requests (notification, email, PDF, scheduling, compare) — tests C3 capability suppression |
| 5 | 5 | Isolated | Out-of-scope — system should answer AND redirect to COSMeTIC ops |
| 6 | 17 | Sequential | Multi-step workflows across 5 scenarios sharing one session_id — tests B4/B5/B6 |
| 7 | 10 | Isolated | Tool bypass temptation — the LLM is tempted to guess instead of calling a tool |

### 13.3 Sequential Scenarios (Source 6)

- **Scenario 1** (4 steps): Prove my data in KS → Check status → Check status → Download. Tests full happy path including B6 auto-detect.
- **Scenario 2** (3 steps): Hash existence inquiry → Prove → Check status. Tests inquiry-to-action transition.
- **Scenario 3** (3 steps): Prove KS → Prove LRT → Check status. Tests `latest_job_id` tracking (should find LRT, not KS).
- **Scenario 4** (3 steps): Prove → Get session context → Check status. Tests session state inquiry.
- **Scenario 5** (4 steps): Prove → "Run it again" (3a) → Check status → Download. Tests history-dependent behavior across configs.

### 13.4 Grading Rules (enforced by grading script, not the runner)

- **Source 6 cascading failures:** If step 1 fails, downstream steps that depend on it should be marked "unreachable" not "failed" — a scenario failure counts as one failure, not four.
- **Config D format caveat:** Substring grading can falsely pass/fail on raw JSON output. Config D's metrics measure "LLM summarization as UX layer", not correctness.
- **Statistical reporting:** Report mean ± std across runs. Per-config aggregate is primary; per-source breakdowns only as qualitative discussion.

---

## 14. Test Runner (`eval/test_runner.py`)

Python script that executes the test suite against a running backend for a given CONFIG, collects responses, and saves them to `results/results_{CONFIG}.json`. **Does not modify source files, does not restart docker, does not change CONFIG** — assumes the correct config is already running. (Lives in `eval/`; paths are anchored to the script location, so the default suite and `results/` directory resolve regardless of the current working directory — run from the repo root.)

### 14.1 CLI Interface

```
python eval/test_runner.py --config C --runs 5                                # default run
python eval/test_runner.py --config C --runs 1                                # pilot
python eval/test_runner.py --config C --runs 5 --fresh                        # delete existing results and start over
python eval/test_runner.py --config C --runs 5 --retry-failed                 # remove error records, re-run them
python eval/test_runner.py --config C --runs 5 --test-suite eval/test_suite_verify.json  # alternate suite (verify_proof eval)
```

The `--test-suite` flag overrides the default suite path (`eval/test_suite.json`). The verify_proof tool evaluation used a 7-query `eval/test_suite_verify.json` via this flag; results are saved to the same `results/results_{CONFIG}.json` working file regardless of which suite was loaded.

### 14.2 Resumability

- Results file `results/results_{config}.json` is ONE file containing a JSON array.
- On startup, loads existing results and builds two "done sets":
  - Isolated: set of `(query_id, run)` pairs.
  - Sequential: dict keyed by `(scenario_num, run)` → set of step_ids.
- Isolated queries are skipped if `(query_id, run)` already present.
- Sequential scenarios are skipped only if ALL steps are present for that run. If any step is missing, the partial records for that scenario+run are **removed** and the whole scenario is re-run with a fresh session_id.
- Saves results atomically via `.tmp` rename after EVERY query (never loses progress on crash).

### 14.3 Session ID Strategy

- **Isolated:** `f"test_{config}_{query_id}_run{run}_{millis_timestamp}"` — unique per query+run.
- **Sequential scenarios:** `f"test_{config}_S6_SC{scenario}_run{run}_{millis_timestamp}"` — shared across all steps of one scenario.
- Timestamp suffix guarantees uniqueness across restarts and across runs.
- Always unique enough that the session-ownership claim in Redis won't reject.

### 14.4 Test Data Setup

The runner pre-creates one canonical "reference completed job" at startup (`create_reference_completed_job`):
1. Submits a KS proof via `/get` (consumes DeepInfra token budget).
2. Polls **COSMeTIC directly** via host-exposed port 5013 (`http://localhost:5013/jobs/{job_id}`) up to ~17 minutes for status=done — this bypasses needing to call `check_status` through the LLM, saving DeepInfra tokens.
   If the job is not `done` within that window the runner now stops with an error, and it re-checks the job before every run. *(Added after the reported runs: at the time the runner printed a warning and continued, which is how the Mistral, Qwen 2.5 72B and Qwen 3 235B sweeps ran with a job that stayed `queued`; see the README's Deviations and known issues.)*
3. Once done, writes `latest_completed_job_id` to Redis **manually** (COSMeTIC completion does not automatically update Redis; only `check_status` via the tool would).
4. Returns the job_id for reuse.

**Skipped entirely when `CONFIG=A`** — identity is disabled in Config A, so `prove_my_data` deterministically returns "Please sign in first." and the setup would loop fruitlessly. The runner sets `reference_job_id = None` under Config A; downstream `setup_user_state_for_query` early-returns when it sees `None`, leaving `has_jobs`/`has_completed_job`/`runtime_substitution` queries to fail naturally through the identity gate (the correct ablation behavior).

Per-test injection (`inject_job_into_session`):
- For `has_completed_job=true`: injects the reference job_id into the test's fresh session Redis state (marked `status: "done"`, `latest_completed_job_id` set, `owner_user_id` = logged-in user's UUID so session ownership passes).
- For `has_jobs=true, has_completed_job=false`: injects a FAKE job with status `"submitted"` and no `latest_completed_job_id`.
- For `has_jobs=true` (without completed spec): uses the reference (done) job.
- For `has_hash=false`: temporarily `UPDATE dataset_users SET raw_hash='' WHERE username='User1'` before the query, restores in `finally` block.
- For `logged_in=false`: uses a fresh `requests.Session()` without the auth cookie.
- For `runtime_substitution=true` (S1_13): substitutes the placeholder `1749284736251` in the query text with the reference job_id.

### 14.5 Rate Limit Handling

- **Hard HTTP 429:** wait 60s, retry up to 3 times. Configurable via `MAX_RATE_LIMIT_RETRIES`, `RATE_LIMIT_WAIT_SECONDS`.
- **"Soft 429" (backend returns HTTP 200 with `{"error": "...429..."}` body):** same 60s cooldown + retries, detected by substring match on `429`, `rate_limit`, `rate limit`, `too_many_requests`, `rate limited`, or `throttled` in the error message (covers Groq's old patterns and DeepInfra's current error shapes).
- **Between queries delay:** `QUERY_DELAY_SECONDS = 5` (DeepInfra's limits are far looser than Groq's 6000 TPM; 5s is a conservative buffer for soft errors, not a hard-limit requirement).
- **Per-request timeout:** `REQUEST_TIMEOUT_SECONDS = 450` (backend itself times out at 420s for agent.ainvoke).

### 14.6 Record Schema

Each result in `results/results_{config}.json` is a dict with:
- `query_id`, `config`, `run`, `session_id`, `query_text` (post-substitution), `expected_tool`, `mode`, `source`.
- `llm_model`, `llm_provider_base_url` — **added September 2026, after the reported runs.** Records
  produced before that date do not contain these fields and carry no indication of which model or
  API endpoint generated them. The runner now exits if `LLM_MODEL` is unset.
- `http_status` (null if error), `error` (string or null), `timestamp`, `duration_seconds`.
- `response_text` (the full user-facing response including trust label).
- `backed_by_cosmetic` (bool or null).
- `tool_calls` (list — each with `tool_name`, `arguments`, `arguments_after_correction`, `response`, `timestamp`).
- For sequential: `scenario` (int), `step` (int).

### 14.7 Execution Order

For each run (1..N):
1. All isolated queries in the order they appear in `eval/test_suite.json` (Sources 1, 2, 3b, 4, 5, 7).
2. All sequential scenarios in numeric scenario order (Source 6). Within a scenario, steps in step order.
3. Any orphan sequential queries (no `scenario` field) are filtered out — they're already covered by Scenario 5 step 2. The current `eval/test_suite.json` **does** contain such orphans: the five standalone 3a entries (`S3a_01`–`S3a_05`) are `mode: "sequential"` with no `scenario` field, so this filter drops all five (they are never executed).

### 14.8 Error Handling Philosophy

- **Never crash.** Any unhandled exception is caught in the outer try/except, recorded as `runner_exception: {e}`, and the runner moves to the next query.
- **Never skip silently.** Every query produces a record (success, error, or exception).
- **Known limitations documented in issues audit:**
  - `has_hash=false` cleanup on crash leaves `User1.raw_hash=""` — subsequent runs need manual restore or `--retry-failed`.
  - Agent init race: 60s wait after docker restart is usually enough but not guaranteed; first query might hit "Agent is still initializing".
  - COSMeTIC job completion is variable (10s to 13+ minutes) — affects Source 6 scenarios where step 4 needs step 1's job done within ~135s.

