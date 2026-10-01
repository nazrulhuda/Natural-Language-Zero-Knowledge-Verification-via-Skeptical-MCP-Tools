from flask import Flask, render_template, request, send_file, session
from langchain_mcp_adapters.client import MultiServerMCPClient
from langgraph.prebuilt import create_react_agent
from langchain_openai import ChatOpenAI
import os
from dotenv import load_dotenv
import asyncio
import time
import httpx
import threading
import json
import re
import contextvars
from datetime import datetime, timezone
from functools import wraps

from account_store import (
    get_masked_hash_for_user,
    get_user_by_id,
    signin_dataset_user,
)
from context_manager import context_manager

import logging

load_dotenv()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

SYSTEM_CONFIG = os.getenv("CONFIG", "C")
EVAL_MODE = os.getenv("EVAL_MODE", "false").lower() == "true" or SYSTEM_CONFIG == "D"

logger.info(f"System configuration: {SYSTEM_CONFIG}")
logger.info(f"EVAL_MODE: {EVAL_MODE}")


def format_tool_response(tool_name: str, raw_response_text: str) -> str:
    """Format raw MCP tool response into deterministic human-readable text.

    Used by Config D to bypass LLM summarization and route tool output
    directly to the user in a consistent format.
    """
    if not raw_response_text:
        return "Tool returned an empty response."

    try:
        data = json.loads(raw_response_text)
    except (json.JSONDecodeError, TypeError):
        return raw_response_text

    if tool_name in ("prove_hash", "prove_my_data"):
        if isinstance(data, dict) and "job_id" in data:
            job_id = data["job_id"]
            proof_type = data.get("proof_type")
            if proof_type:
                return f"Your {proof_type} proof has been submitted. Job ID: {job_id}. You can check the status by asking 'check my status'."
            return f"Your proof has been submitted. Job ID: {job_id}. You can check the status by asking 'check my status'."
        return raw_response_text

    if tool_name == "check_status":
        if isinstance(data, dict) and "status" in data:
            job_id = data.get("job_id", "unknown")
            status = data["status"]
            proof_type = data.get("proof_type")
            if proof_type:
                return f"Job {job_id} ({proof_type}): Status is {status}."
            return f"Job {job_id}: Status is {status}."
        return raw_response_text

    if tool_name == "download_proof":
        if isinstance(data, dict) and data.get("success"):
            filename = data.get("filename", "unknown")
            file_size = data.get("file_size", "unknown")
            return f"Your proof file has been downloaded. Filename: {filename}, size: {file_size} bytes. Use the download button to save it."
        return raw_response_text

    if tool_name == "verify_proof":
        if isinstance(data, dict) and "ok" in data:
            return "Proof verification PASSED." if data["ok"] else "Proof verification FAILED."
        return raw_response_text

    if tool_name in ("check_hash_existence", "check_my_hash_existence"):
        return raw_response_text

    if tool_name == "get_session_context":
        if isinstance(data, dict):
            jobs = data.get("jobs", [])
            state = data.get("state", {})
            latest_completed = state.get("latest_completed_job_id")
            if not jobs:
                return "You have no jobs in this session yet."
            lines = [f"You have {len(jobs)} job(s) in this session:"]
            for job in jobs:
                jid = job.get("job_id", "unknown")
                jtype = job.get("proof_type", "unknown")
                jstatus = job.get("status", "unknown")
                lines.append(f"- Job {jid} ({jtype} proof): {jstatus}")
            if latest_completed:
                lines.append(f"Most recent completed: Job {latest_completed}.")
            return "\n".join(lines)
        return raw_response_text

    return raw_response_text


def compute_trust_label(mcp_touched: bool, tool_calls: list) -> str:
    """Deterministic trust label, owned by the backend (not model text).

    Strongest label wins across all tools called this turn:
      [Cryptographically verified] > [Cryptographically provable] > [Backed by COSMeTIC prover]

    verify_proof with top-level ok=true  -> cryptographically verified
    hash-existence check that found the hash -> cryptographically provable
    any other tool call -> backed by COSMeTIC prover
    no tool call -> not backed

    tool_calls is only populated when EVAL_MODE is on; when it is empty but a
    tool was touched, fall back to the coarse "[Backed by COSMeTIC prover]" label.
    """
    if not mcp_touched:
        return "[Not backed by COSMeTIC prover]"
    if not tool_calls:
        return "[Backed by COSMeTIC prover]"

    verified = False
    provable = False
    for tc in tool_calls:
        name = tc.get("tool_name", "")
        resp = tc.get("response", "") or ""
        if name == "verify_proof":
            try:
                data = json.loads(resp)
                if isinstance(data, dict) and data.get("ok") is True:
                    verified = True
            except (json.JSONDecodeError, TypeError):
                pass
        elif name in ("check_hash_existence", "check_my_hash_existence"):
            # Reliable signal is the Summary line: "...was found in..." => hash present.
            if "was found in" in resp.lower():
                provable = True

    if verified:
        return "[Cryptographically verified]"
    if provable:
        return "[Cryptographically provable]"
    return "[Backed by COSMeTIC prover]"


def _extract_tool_response_text(result) -> str:
    """Extract readable text from an MCP tool call result for eval logging.

    The interceptor receives raw MCPToolCallResult (CallToolResult) objects
    before the langchain adapter converts them. This extracts the text content.
    """
    # CallToolResult has .content list of TextContent/ImageContent/etc.
    if hasattr(result, "content") and isinstance(result.content, list):
        parts = []
        for item in result.content:
            if hasattr(item, "text"):
                parts.append(str(item.text))
        if parts:
            return "\n".join(parts)
    # ToolMessage (langchain) or plain string
    if hasattr(result, "content") and isinstance(result.content, str):
        return result.content
    return str(result)


app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "dev-secret-change-me")
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.getenv("SESSION_COOKIE_SECURE", "false").lower() == "true",
)

# Global variables to store the agent
agent = None
client = None
current_session_id_var = contextvars.ContextVar("current_session_id", default=None)
current_user_id_var = contextvars.ContextVar("current_user_id", default=None)
mcp_tracker_var = contextvars.ContextVar("mcp_tracker", default=None)

class SessionIdInjectorInterceptor:
    """Inject session and user context into MCP tool calls."""

    async def __call__(self, request_data, handler):
        # Any request that reaches this interceptor is an MCP tool call.
        tracker = mcp_tracker_var.get()
        if isinstance(tracker, dict):
            tracker["touched"] = True

        # Capture raw LLM-provided tool name and arguments BEFORE any correction.
        tool_call_record = None
        if EVAL_MODE and isinstance(tracker, dict):
            tool_call_record = {
                "tool_name": getattr(request_data, "name", "unknown"),
                "arguments": dict(request_data.args or {}),
                "arguments_after_correction": None,
                "response": None,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }

        session_id = current_session_id_var.get()
        user_id = current_user_id_var.get()
        if not session_id and not user_id:
            result = await handler(request_data)
            if tool_call_record is not None:
                tool_call_record["arguments_after_correction"] = dict(request_data.args or {})
                tool_call_record["response"] = _extract_tool_response_text(result)
                tracker.setdefault("tool_calls", []).append(tool_call_record)
            return result

        args = dict(request_data.args or {})
        headers = dict(request_data.headers or {})
        if session_id:
            args["session_id"] = session_id
            headers["x-session-id"] = session_id
        # A3: User identity stripping (disabled in Config A and A_STAR)
        if user_id and SYSTEM_CONFIG not in ("A", "A_STAR"):
            headers["x-user-id"] = user_id
            args.pop("user_id", None)
        # Config A: don't inject header, don't strip args — LLM's identity reaches the tool

        corrected_request = request_data.override(args=args, headers=headers)
        result = await handler(corrected_request)

        if tool_call_record is not None:
            tool_call_record["arguments_after_correction"] = dict(corrected_request.args or {})
            tool_call_record["response"] = _extract_tool_response_text(result)
            tracker.setdefault("tool_calls", []).append(tool_call_record)

        return result


def _request_field(name: str):
    if request.is_json:
        data = request.get_json(silent=True) or {}
        return data.get(name)
    return request.form.get(name)


def _current_user():
    user_id = session.get("user_id")
    if not user_id:
        return None
    username = session.get("username")
    if username:
        return {"id": user_id, "username": username}
    email = session.get("user_email")
    if email:
        return {"id": user_id, "email": email}
    db_user = get_user_by_id(user_id)
    if db_user:
        if db_user.get("username"):
            session["username"] = db_user["username"]
        if db_user.get("email"):
            session["user_email"] = db_user["email"]
    return db_user


def require_login(fn):
    @wraps(fn)
    def _wrapped(*args, **kwargs):
        user = _current_user()
        if not user:
            return json.dumps({"error": "Authentication required. Please sign in."}), 401
        return fn(*args, **kwargs)

    return _wrapped


def tool_result_to_text(result) -> str:
    """Normalize MCP tool results into a plain text payload."""
    if isinstance(result, str):
        return result
    if isinstance(result, list):
        parts = []
        for item in result:
            if isinstance(item, dict) and "text" in item:
                parts.append(str(item.get("text", "")))
            elif hasattr(item, "text"):
                parts.append(str(getattr(item, "text", "")))
            else:
                parts.append(str(item))
        return "\n".join(p for p in parts if p)
    return str(result)

def initialize_agent():
    """Initialize the MCP client and agent in a separate thread"""
    global agent, client
    async def _setup_once():
        """Perform one attempt to setup the client and agent.

        This function assumes dependent HTTP MCP servers are running and will
        raise on failure so the outer thread can retry with backoff.
        """
        global agent, client
        prover_mcp_url = os.getenv("PROVER_MCP_URL", "http://localhost:8003/mcp")

        async with httpx.AsyncClient(timeout=2.0) as hc:
            # Probe endpoint with retries
            for i in range(40):
                ok_prover = False
                try:
                    r = await hc.get(prover_mcp_url)
                    ok_prover = r.status_code in (200, 202, 406)
                except Exception:
                    ok_prover = False

                if ok_prover:
                    break
                if i % 5 == 0:
                    print(f"Waiting for MCP endpoint: prover={prover_mcp_url} ok={ok_prover}")
                await asyncio.sleep(0.5)
            else:
                raise ConnectionError(f"Dependent MCP server not reachable: prover={prover_mcp_url}")

        client = MultiServerMCPClient(
            {
                "prover": {
                    "url": prover_mcp_url,
                    "transport": "streamable_http",
                },
            },
            tool_interceptors=[SessionIdInjectorInterceptor()],
        )

        deepinfra_api_key = os.getenv("DEEPINFRA_API_KEY")
        if not deepinfra_api_key:
            raise ValueError("DEEPINFRA_API_KEY environment variable not set. Please set your DeepInfra API key.")

        # Try get_tools with a few retries; raise if still failing
        last_err = None
        for attempt in range(1, 11):
            try:
                tools = await client.get_tools()
                break
            except Exception as e:
                last_err = e
                print(f"get_tools attempt {attempt} failed: {e}")
                await asyncio.sleep(1.0)
        else:
            raise last_err

        llm_model_name = os.getenv("LLM_MODEL", "Qwen/Qwen3-32B")
        # Any OpenAI-compatible endpoint. Default unchanged from the evaluated build
        # (DeepInfra); the evaluation runner records this value in every result.
        llm_base_url = os.getenv("LLM_BASE_URL", "https://api.deepinfra.com/v1/openai")
        model = ChatOpenAI(
            model=llm_model_name,
            api_key=deepinfra_api_key,
            base_url=llm_base_url,
            temperature=0.0,
        )
        # Smaller models (e.g., Llama 3 8B) need an explicit system prompt to
        # engage tool calling when many tools are available. Qwen 3 32B figures
        # this out from the tool schemas alone and doesn't need scaffolding.
        if "llama" in llm_model_name.lower():
            system_prompt = (
                "You are a helpful assistant with access to tools for zero-knowledge "
                "proof operations. When the user asks about proofs, status checks, "
                "downloads, or hash existence, you MUST call the appropriate tool. "
                "Always call a tool when one matches the user's request — never "
                "reply in plain text for tool-eligible queries."
            )
            agent = create_react_agent(model, tools, prompt=system_prompt)
        else:
            agent = create_react_agent(model, tools)

    # Run setup in a loop so transient errors don't kill the thread.
    while True:
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(_setup_once())
            print("Agent initialized successfully")
            loop.close()
            break
        except Exception as e:
            try:
                loop.close()
            except Exception:
                pass
            print(f"Agent setup failed, will retry in 5s: {e}")
            time.sleep(5)

# Initialize agent in background thread
threading.Thread(target=initialize_agent, daemon=True).start()

@app.route('/')
def home():
    return render_template('index.html')


@app.route('/auth/signin', methods=['POST'])
def auth_signin():
    try:
        username = (_request_field("username") or "").strip()
        password = _request_field("password") or ""
        if not username or not password:
            return json.dumps({"success": False, "error": "username and password are required"}), 400

        result = signin_dataset_user(username, password)
        if not result.get("success"):
            return json.dumps(result), 401

        user = result["user"]
        session["user_id"] = user["id"]
        session["username"] = user["username"]
        session.permanent = True
        return json.dumps({"success": True, "user": user})
    except Exception as e:
        return json.dumps({"success": False, "error": f"Sign in failed: {str(e)}"}), 500


@app.route('/auth/logout', methods=['POST'])
def auth_logout():
    session.clear()
    return json.dumps({"success": True})


@app.route('/auth/me', methods=['GET'])
def auth_me():
    user = _current_user()
    if not user:
        return json.dumps({"logged_in": False})
    return json.dumps({"logged_in": True, "user": user})


@app.route('/admin/sync-dataset', methods=['POST'])
def admin_sync_dataset():
    """Fetch input-files zip, save to INPUT_ZIPS_DIR, and upsert dataset_users. Protected by ADMIN_SYNC_SECRET."""
    secret = os.getenv("ADMIN_SYNC_SECRET", "")
    if secret:
        key = request.headers.get("X-Admin-Key") or _request_field("admin_key")
        if key != secret:
            return json.dumps({"error": "Unauthorized"}), 401
    try:
        from dataset_sync import run_dataset_sync
        result = run_dataset_sync()
        if result.get("success"):
            return json.dumps(result)
        return json.dumps({"error": result.get("message", "Sync failed"), "details": result}), 400
    except Exception as e:
        return json.dumps({"error": str(e)}), 500


@app.route('/api/me/hash', methods=['GET'])
@require_login
def get_my_hash():
    user = _current_user()
    masked = get_masked_hash_for_user(user["id"])
    return json.dumps({"success": True, "masked_hash": masked})


@app.route('/api/me/hash', methods=['POST'])
@require_login
def save_my_hash():
    try:
        user = _current_user()
        # All hashes are managed by the dataset sync pipeline; manual saving is disabled.
        return json.dumps(
            {
                "success": False,
                "error": "Your hash is managed by the dataset. Run the admin dataset sync to update hashes.",
            }
        ), 400
    except Exception as e:
        return json.dumps({"success": False, "error": f"Failed to save hash: {str(e)}"}), 500

@app.route('/get', methods=['POST'])
def get_bot_response():
    try:
        user_message = _request_field('msg')
        session_id = _request_field('session_id') or 'default'
        user = _current_user()
        current_user_id = user["id"] if user else None

        if not user_message:
            return json.dumps({"error": "msg is required"}), 400
        
        if agent is None:
            return json.dumps({"error": "Agent is still initializing. Please wait a moment and try again."})

        # If a user is logged in, claim or verify ownership of this session_id.
        if current_user_id:
            ok, err = context_manager.claim_or_verify_session_owner(session_id, current_user_id)
            if not ok:
                if err == "redis_down":
                    return json.dumps({"error": "Session store is unavailable"}), 503
                if err == "mismatch":
                    return json.dumps({"error": "This session belongs to another user. Please refresh the page or use a new session id."}), 403
                return json.dumps({"error": "Invalid session or user"}), 400

        # Create a new event loop for this request
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        
        try:
            # Config A / A_STAR: conversation history enabled
            if SYSTEM_CONFIG in ("A", "A_STAR"):
                history = context_manager.get_recent_messages(session_id, limit=15)
                messages = [{"role": m["role"], "content": m["content"]} for m in history]
                if SYSTEM_CONFIG == "A_STAR" and current_user_id:
                    messages.insert(0, {
                        "role": "system",
                        "content": (
                            f"The currently authenticated user has user_id: {current_user_id}. "
                            f"When calling any tool that accepts a user_id parameter, you MUST "
                            f"pass this exact value."
                        ),
                    })
                messages.append({"role": "user", "content": user_message})
            else:
                # Flask remains a thin proxy; MCP tools own Redis state.
                messages = [{"role": "user", "content": user_message}]
            
            # Get response from agent with timeout, using conversation history
            # Status checks can take up to 5 minutes, so we use a longer timeout (7 minutes to account for overhead)
            token_session = current_session_id_var.set(session_id)
            token_user = current_user_id_var.set(current_user_id)
            mcp_tracker = {"touched": False}
            token_mcp = mcp_tracker_var.set(mcp_tracker)
            try:
                response = loop.run_until_complete(
                    asyncio.wait_for(
                        agent.ainvoke({"messages": messages}),
                        timeout=420.0  # 7 minute timeout (420 seconds) to allow for 5-minute status checks plus overhead
                    )
                )
            except asyncio.TimeoutError:
                return json.dumps({"error": "The request timed out after 7 minutes. Status checks can take up to 5 minutes. Please try again or check the server logs."})

            mcp_touched = bool(mcp_tracker.get("touched"))
            
            # Extract the last message content
            bot_response = response['messages'][-1].content
            
            # Handle empty responses
            if not bot_response or bot_response.strip() == "":
                # Check if there are any tool calls or intermediate steps
                if len(response['messages']) > 1:
                    # Try to get information from earlier messages
                    for msg in reversed(response['messages']):
                        if hasattr(msg, 'content') and msg.content:
                            bot_response = msg.content
                            break
                        elif hasattr(msg, 'additional_kwargs') and 'tool_calls' in msg.additional_kwargs:
                            bot_response = "Tool was called but no response received. Please check the prover server logs."
                            break
                
                if not bot_response or bot_response.strip() == "":
                    bot_response = "I received your request but didn't get a response. Please check the server logs or try again."

            # Strip Qwen3 reasoning blocks before persistence (must happen before Config A save_message
            # so replayed history stays clean, and before trust label so response_text is clean).
            bot_response = re.sub(r'<think>.*?</think>', '', bot_response, flags=re.DOTALL).strip()

            # Config A / A_STAR: save conversation for history (replayed on next turn)
            if SYSTEM_CONFIG in ("A", "A_STAR"):
                context_manager.save_message(session_id, "user", user_message)
                context_manager.save_message(session_id, "assistant", bot_response)

            # Config D: deterministic formatted response, bypassing LLM summarization
            if SYSTEM_CONFIG == "D" and mcp_tracker.get("touched"):
                tool_calls_list = mcp_tracker.get("tool_calls", [])
                if tool_calls_list:
                    last_call = tool_calls_list[-1]
                    bot_response = format_tool_response(
                        last_call.get("tool_name", "unknown"),
                        last_call.get("response", ""),
                    )

            # Deterministic trust label, owned by backend (not model text).
            trust_label = compute_trust_label(mcp_touched, mcp_tracker.get("tool_calls", []))
            bot_response = f"{bot_response}\n\n{trust_label}"

            response_payload = {
                "response": bot_response
            }

            if EVAL_MODE:
                response_payload["backed_by_cosmetic"] = mcp_touched
                response_payload["tool_calls"] = mcp_tracker.get("tool_calls", [])

            return json.dumps(response_payload)
            
        finally:
            try:
                current_session_id_var.reset(token_session)
                current_user_id_var.reset(token_user)
                mcp_tracker_var.reset(token_mcp)
            except Exception:
                pass
            loop.close()
            
    except Exception as e:
        return json.dumps({"error": f"Error: {str(e)}"})


@app.route('/download_proof', methods=['POST'])
@require_login
def download_proof():
    """Download a proof file."""
    try:
        # Accept both JSON and form data
        if request.is_json:
            data = request.get_json()
            job_id = data.get('job_id')
            session_id = data.get('session_id')
        else:
            job_id = request.form.get('job_id')
            session_id = request.form.get('session_id')
        user = _current_user()
        current_user_id = user["id"] if user else None

        if not session_id:
            return json.dumps({"error": "session_id is required"})

        ok, err = context_manager.claim_or_verify_session_owner(session_id, current_user_id)
        if not ok:
            if err == "redis_down":
                return json.dumps({"error": "Session store is unavailable"}), 503
            if err == "mismatch":
                return json.dumps({"error": "Not authorized for this session_id"}), 403
            return json.dumps({"error": "Invalid session or user"}), 400
        
        if agent is None:
            return json.dumps({"error": "Agent is not initialized"})

        # Create a new event loop for this request
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        
        try:
            # Call the download_proof tool
            tools = loop.run_until_complete(client.get_tools())
            download_proof_tool = None
            for tool in tools:
                if tool.name == "download_proof":
                    download_proof_tool = tool
                    break
            
            if not download_proof_tool:
                return json.dumps({"error": "download_proof tool not found"})
            
            # Canonical downloads contract shared with MCP.
            downloads_dir = os.path.abspath(os.getenv("DOWNLOADS_DIR", "/app/downloads"))
            os.makedirs(downloads_dir, exist_ok=True)
            filename_job = str(job_id) if job_id else f"session-{session_id}"
            desired_filename = f"proofs-{filename_job}.zip"
            
            # Call download with timeout
            try:
                token_session = current_session_id_var.set(session_id)
                token_user = current_user_id_var.set(current_user_id)
                try:
                    invoke_payload = {
                        "output_filename": desired_filename,
                    }
                    if job_id:
                        invoke_payload["job_id"] = str(job_id)
                    result = loop.run_until_complete(
                        asyncio.wait_for(
                            download_proof_tool.ainvoke(invoke_payload),
                            timeout=120.0  # 2 minute timeout for download
                        )
                    )
                finally:
                    current_session_id_var.reset(token_session)
                    current_user_id_var.reset(token_user)
            except asyncio.TimeoutError:
                return json.dumps({
                    "success": False,
                    "error": "Download timed out after 2 minutes. The proof file may be large or the server is slow."
                }), 400
            
            # Handle result - it might be a string or a list
            result_str = tool_result_to_text(result)
            
            result_json = None
            try:
                result_json = json.loads(result_str) if isinstance(result_str, str) else None
            except Exception:
                result_json = None

            # Serve only if MCP returns success + relative filename metadata.
            if isinstance(result_json, dict) and result_json.get("success"):
                filename_only = os.path.basename(str(result_json.get("filename", "")))
                if not filename_only:
                    return json.dumps({"success": False, "error": "MCP download response missing filename"}), 400
                output_filename = os.path.abspath(os.path.join(downloads_dir, filename_only))
                if os.path.commonpath([downloads_dir, output_filename]) != downloads_dir:
                    return json.dumps({"success": False, "error": "Invalid filename returned by MCP"}), 400
                if os.path.exists(output_filename):
                    mimetype = 'application/zip' if filename_only.endswith('.zip') else 'application/octet-stream'
                    response = send_file(
                        output_filename,
                        as_attachment=True,
                        download_name=filename_only,
                        mimetype=mimetype
                    )
                    response.headers['Content-Disposition'] = f'attachment; filename="{filename_only}"'
                    return response
            
            # Log the issue for debugging
            error_msg = result_str if result_str else "Unknown error"
            expected_path = os.path.abspath(os.path.join(downloads_dir, desired_filename))
            file_exists = os.path.exists(expected_path)
            error_details = f"Download failed. Result: {error_msg}, File exists: {file_exists}, Expected path: {expected_path}"
            print(f"Download error: {error_details}")
            return json.dumps({
                "success": False,
                "error": error_details
            }), 400
            
        finally:
            loop.close()
            
    except Exception as e:
        import traceback
        error_trace = traceback.format_exc()
        print(f"Download exception: {error_trace}")
        return json.dumps({"error": f"Error downloading proof: {str(e)}"}), 500


@app.route('/context/<session_id>', methods=['GET'])
@require_login
def get_context(session_id):
    """Proxy session context from MCP server."""
    try:
        user = _current_user()
        current_user_id = user["id"] if user else None
        ok, err = context_manager.claim_or_verify_session_owner(session_id, current_user_id)
        if not ok:
            if err == "redis_down":
                return json.dumps({"error": "Session store is unavailable"}), 503
            if err == "mismatch":
                return json.dumps({"error": "Not authorized for this session_id"}), 403
            return json.dumps({"error": "Invalid session or user"}), 400
        if client is None:
            return json.dumps({"error": "MCP client is not initialized"}), 500
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            tools = loop.run_until_complete(client.get_tools())
            context_tool = None
            for tool in tools:
                if tool.name == "get_session_context":
                    context_tool = tool
                    break
            if not context_tool:
                return json.dumps({"error": "get_session_context tool not found"}), 500

            token_session = current_session_id_var.set(session_id)
            token_user = current_user_id_var.set(current_user_id)
            try:
                result = loop.run_until_complete(context_tool.ainvoke({"session_id": session_id}))
            finally:
                current_session_id_var.reset(token_session)
                current_user_id_var.reset(token_user)
            return tool_result_to_text(result)
        finally:
            loop.close()
    except Exception as e:
        return json.dumps({"error": str(e)}), 500


if __name__ == '__main__':
    flask_port = int(os.getenv("FLASK_PORT", "5001"))
    app.run(debug=True, host='0.0.0.0', port=flask_port)
