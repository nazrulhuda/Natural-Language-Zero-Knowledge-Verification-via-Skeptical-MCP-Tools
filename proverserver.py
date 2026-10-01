from __future__ import annotations

import json
import logging
import os
import subprocess
import ast
from typing import Optional

from mcp.server.fastmcp import Context, FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from account_store import get_primary_hash_for_user
from context_manager import context_manager

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

SYSTEM_CONFIG = os.getenv("CONFIG", "C")
logger.info(f"Prover server configuration: {SYSTEM_CONFIG}")


mcp = FastMCP(
    "Prover",
    transport_security=TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=[
            "proverserver",  # Docker service name
            "proverserver:*",  # Docker service name with any port
            "localhost",
            "127.0.0.1",
            "0.0.0.0",
        ],
    ),
)


def _normalize_proof_type(proof_type: str) -> str:
    """Normalize proof type aliases to canonical API identifiers."""
    proof_type_normalized = (proof_type or "").lower().strip()
    if proof_type_normalized in ["ks", "kolmogorov", "kolmogorov-smirnov", "kolmogorov_smirnov"]:
        return "KS"
    if proof_type_normalized in ["lrt", "likelihood_ratio_test", "likelihood-ratio-test"]:
        return "LRT"
    return "logistic_accuracy"


def _resolve_session_id(session_id: Optional[str], ctx: Optional[Context]) -> Optional[str]:
    """Resolve session_id from explicit tool arg first, then MCP HTTP headers."""
    if session_id:
        return str(session_id)
    try:
        req = ctx.request_context.request if ctx else None
        headers = getattr(req, "headers", None) if req is not None else None
        if headers:
            header_sid = headers.get("x-session-id") or headers.get("X-Session-Id")
            if header_sid:
                return str(header_sid)
    except Exception:
        pass
    return None


def _resolve_user_id(user_id: Optional[str], ctx: Optional[Context]) -> Optional[str]:
    """Resolve user_id from MCP HTTP headers injected by Flask.

    Normal flows should rely solely on the x-user-id header set by the backend.
    Tool arguments are not considered authoritative for user identity.
    """
    try:
        req = ctx.request_context.request if ctx else None
        headers = getattr(req, "headers", None) if req is not None else None
        if headers:
            header_uid = headers.get("x-user-id") or headers.get("X-User-Id")
            if header_uid:
                return str(header_uid)
    except Exception:
        pass

    # Config A / A_STAR: allow LLM-provided user_id when identity protection is disabled
    if SYSTEM_CONFIG in ("A", "A_STAR") and user_id:
        return str(user_id)
    return None


def _get_base_url(proof_type: str = "logistic_accuracy") -> str:
    """Get the base URL for the prover endpoint based on proof type.
    
    Args:
        proof_type: Type of proof:
            - "logistic_accuracy" → port 5012
            - "KS"                → port 5013
            - "LRT"               → port 5014
    
    Returns:
        Base URL string for the appropriate API endpoint
    """
    # Map proof types to ports
    proof_type_lower = proof_type.lower().strip()
    base_host = os.getenv("PROVER_BASE_HOST", "COSMeTICprover")
    
    if proof_type_lower in ["ks", "kolmogorov", "kolmogorov-smirnov", "kolmogorov_smirnov"]:
        # KS (Kolmogorov-Smirnov) uses port 5013
        return f"http://{base_host}:5013"
    elif proof_type_lower in ["lrt", "likelihood_ratio_test", "likelihood-ratio-test"]:
        # LRT (Likelihood Ratio Test) uses port 5014
        return f"http://{base_host}:5014"
    else:
        # Logistic accuracy (default) uses port 5012
        return f"http://{base_host}:5012"


def _run_curl_command(url: str, extra_args: str = "", json_data: dict = None) -> subprocess.CompletedProcess:
    """Run a curl command and return the completed process.
    
    Args:
        url: The URL to request
        extra_args: Additional curl arguments (e.g., "-X POST")
        json_data: Optional dictionary to send as JSON in the request body
    """
    # Add timeout to prevent hanging
    # Status checks should be fast (just querying job status)
    # Prove-hash can have slow connection establishment but fast response once connected
    if "/jobs/" in url and "/download" not in url:
        timeout = 300  # 5 minutes for status checks (API can be very slow)
    elif "/download" in url:
        timeout = 120  # 2 minutes for downloads (files can be large)
    elif "/verify-job/" in url:
        timeout = 300  # 5 minutes for verify (ezkl.verify can be slow over many sub-proofs)
    elif "prove-hash" in url or "prove-raw" in url:
        timeout = 240  # 4 minutes for prove-hash (doubled from 2 minutes - connection can be slow, but response is fast)
    elif "check-hash" in url or "check-existence" in url:
        timeout = 120  # 2 minutes for hash existence checks
    else:
        timeout = 30  # 30 seconds for other operations
    
    # Build curl command as a list (safer than shell string)
    # Use -L to follow redirects (needed for check-hash endpoint)
    cmd_parts = ["curl", "-s", "-L", f"--max-time", str(timeout)]
    
    # Add extra args
    if extra_args:
        cmd_parts.extend(extra_args.split())
    
    # Add JSON data if provided - properly quote the JSON string
    if json_data:
        json_str = json.dumps(json_data)
        cmd_parts.extend(["-H", "Content-Type: application/json", "-d", json_str])
    
    # Add URL (no need to quote when using list directly)
    cmd_parts.append(url)
    
    # Use list directly instead of shell command to avoid quoting issues
    return subprocess.run(
        cmd_parts,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


# Old tools removed - using new prove-hash API endpoint instead

@mcp.tool()
async def prove_hash(
    user_hash: Optional[str] = None,
    nonce: Optional[str] = None,
    proof_type: str = "",
    session_id: Optional[str] = None,
    user_id: Optional[str] = None,
    ctx: Context | None = None,
) -> str:
    """Submit a proof job for a specific hash (logistic accuracy, KS, or LRT).
    
    This is the **low-level** proof submission tool that works with an explicit hash.
    It should be used when:
    
    - The user has directly provided a concrete hash value in the current request
      (e.g. "submit a job for hash 6f in KS"), OR
    - There is no signed-in user and you cannot rely on saved account data.
    
    When the user is signed in and talks about **\"my hash\", \"my raw hash\", \"my user hash\",
    or \"my data\"**, you should normally **NOT** call this tool directly. Instead:
    
    - Prefer :func:`prove_my_data` so the backend can:
      - Resolve the saved hash from Postgres automatically
    
    Additional rules for the agent:
    
    - Do NOT silently assume a default application. If it is unclear whether the user
      wants logistic_accuracy, KS, or LRT, ask a clarification question and only call
      this tool once ``proof_type`` is explicit.
    - You do NOT need to check hash existence first; the proof job itself determines
      whether the hash was used or not.
    - Do NOT offer "notify me when complete" or any push notification workflow.
      This system does not support asynchronous notifications; users should check
      status manually with the check_status tool.
    
    Args:
        user_hash: Hash string to prove against. Optional when the user is signed in
            and has a saved hash (in that case the tool will resolve it automatically).
        nonce: JSON string representing the nonce. Optional; if omitted, a default
            nonce of ``[[0.0, 0.0, 0.0]]`` is used.
        proof_type: REQUIRED proof type (no default). Must map to **one** of:
            - \"logistic_accuracy\" (port 5012; smt_list [\"length\", \"acc\"])
            - \"KS\"                (port 5013; no smt_list)
            - \"LRT\"               (port 5014; smt_list [\"full\", \"reduced\"])
        session_id: Chat session identifier injected by Flask.
        user_id: Optional explicit user id; normally injected by the HTTP layer.
    
    Returns:
        A JSON string response from the external API (typically containing job_id and
        status) or an error description.
    """
    resolved_session_id = _resolve_session_id(session_id, ctx)
    if not resolved_session_id:
        return json.dumps({"error": "session_id is required"})
    context_manager.touch_session(resolved_session_id)

    resolved_user_id = _resolve_user_id(user_id, ctx)

    # Resolve user_hash: prefer explicit value, otherwise fall back to signed-in user's saved hash.
    if not user_hash:
        if not resolved_user_id:
            return (
                "user_hash is required when there is no signed-in user. "
                "If you are signed in, call this tool without user_hash and it will use your saved hash."
            )
        try:
            saved = get_primary_hash_for_user(resolved_user_id)
        except Exception:
            return "Could not read your saved hash right now. Please try again."
        if not saved:
            return "No hash saved for this user. Please run the dataset sync or save a hash first."
        user_hash = saved

    if not proof_type:
        return (
            "proof_type is required and there is no default. "
            "Please specify one of: logistic_accuracy, KS, or LRT."
        )

    # If nonce is omitted, use a simple default nonce to keep behavior deterministic
    # across different upstream prover endpoints.
    if nonce is None:
        nonce = "[[0.0, 0.0, 0.0]]"

    # Normalize proof_type to handle variations
    proof_type_normalized = _normalize_proof_type(proof_type)
    
    base_url = _get_base_url(proof_type_normalized)
    url = f"{base_url}/prove-hash/"
    
    try:
        # Parse nonce from string to actual list
        try:
            # First try JSON parsing
            nonce_parsed = json.loads(nonce)
        except json.JSONDecodeError:
            # If it's not valid JSON, try to parse it as a Python literal (safer than eval)
            try:
                nonce_parsed = ast.literal_eval(nonce)
            except (ValueError, SyntaxError):
                return f"Error: Invalid nonce format. Expected JSON array or Python list, got: {nonce}"
        
        # Prepare JSON payload based on proof type
        if proof_type_normalized == "KS":
            # KS (Kolmogorov-Smirnov) API only requires user_hash and nonce (no smt_list)
            payload = {
                "user_hash": user_hash,
                "nonce": nonce_parsed
            }
        elif proof_type_normalized == "LRT":
            # LRT API requires smt_list ["full", "reduced"], user_hash, and nonce
            smt_list = ["full", "reduced"]
            payload = {
                "smt_list": smt_list,
                "user_hash": user_hash,
                "nonce": nonce_parsed
            }
        else:
            # Logistic accuracy requires smt_list ["length", "acc"], user_hash, and nonce
            smt_list = ["length", "acc"]
            payload = {
                "smt_list": smt_list,
                "user_hash": user_hash,
                "nonce": nonce_parsed
            }
        
        result = _run_curl_command(url, "-X POST", json_data=payload)
        
        if result.returncode != 0:
            # Curl exit code 28 means timeout
            if result.returncode == 28:
                return (
                    f"Failed to submit proof job: Request timed out.\n"
                    f"The backend server at {base_url} is not responding or is taking too long.\n"
                    f"Please check if the backend server is running and accessible.\n"
                    f"URL attempted: {url}\n"
                    f"stdout: {result.stdout}\n"
                    f"stderr: {result.stderr}"
                )
            # Curl exit code 6 means "Couldn't resolve host"
            elif result.returncode == 6:
                return (
                    f"Failed to submit proof job: Cannot connect to the API server.\n"
                    f"The container 'COSMeTICprover' is not running or not on the same Docker network.\n"
                    f"Please ensure:\n"
                    f"  1. The COSMeTICprover container is running (check with: docker ps)\n"
                    f"  2. Both containers are on the same Docker network\n"
                    f"  3. The container name is correct: COSMeTICprover\n"
                    f"URL attempted: {url}\n"
                    f"Error details: {result.stderr}"
                )
            # Curl exit code 7 means "Failed to connect to host"
            elif result.returncode == 7:
                return (
                    f"Failed to submit proof job: Cannot reach the API server.\n"
                    f"The server at {base_url} is not accessible.\n"
                    f"Please check:\n"
                    f"  1. The COSMeTICprover container is running\n"
                    f"  2. The container is listening on the correct port ({base_url.split(':')[-1]})\n"
                    f"  3. Both containers are on the same Docker network\n"
                    f"URL attempted: {url}\n"
                    f"Error details: {result.stderr}"
                )
            else:
                return (
                    f"Failed to submit proof job.\n"
                    f"URL: {url}\n"
                    f"Return code: {result.returncode}\n"
                    f"stdout: {result.stdout}\n"
                    f"stderr: {result.stderr}"
                )
        
        try:
            response_data = json.loads(result.stdout)
            # MCP owns Redis writes for job lifecycle.
            job_id = response_data.get("job_id")
            if job_id is not None:
                context_manager.add_job_id(
                    session_id=resolved_session_id,
                    job_id=str(job_id),
                    proof_type=proof_type_normalized,
                    user_hash=str(user_hash),
                    nonce=nonce,
                    status=str(response_data.get("status", "submitted")),
                )
            # Return the full response as JSON string
            return json.dumps(response_data, indent=2)
        except json.JSONDecodeError:
            return f"Invalid JSON response: {result.stdout}"
    except Exception as exc:
        return f"Error submitting proof job: {exc}"


@mcp.tool()
async def check_status(
    job_id: Optional[str] = None,
    proof_type: str = "logistic_accuracy",
    session_id: Optional[str] = None,
    ctx: Context | None = None,
) -> str:
    """Check the current status of a SINGLE proof job and return immediately.

    Use this tool when the user asks about the status of a single job, for example:
    - "check my status"
    - "how is my proof doing?"
    - "is my KS proof done?"
    - "what's the status of job 12345?"
    - "is my proof done yet?"
    - "did my KS test pass?"

    This is the DEFAULT tool for any single-job status question. It returns just the
    status of the most recent or specified job by querying the live COSMeTIC API.

    Do NOT use get_session_context for single-job status questions — use this tool instead.

    Note: Status checks can take up to 5 minutes if the external API is slow.

    Args:
        job_id: The job ID returned from prove_hash. If omitted, uses latest job from session state.
        proof_type: Type of proof:
            - "logistic_accuracy" (port 5012)
            - "KS"                (port 5013)
            - "LRT"               (port 5014)
           Defaults to "logistic_accuracy" if not specified.
        session_id: Chat session identifier. Injected by Flask middleware; not user-provided.

    Returns:
        Current status response including timing information and proof results if done.
    """
    resolved_session_id = _resolve_session_id(session_id, ctx)
    if not resolved_session_id:
        return json.dumps({"error": "session_id is required"})
    context_manager.touch_session(resolved_session_id)

    # B4: Fallback job ID (disabled in Config A)
    if job_id:
        resolved_job_id = str(job_id)
    elif SYSTEM_CONFIG != "A":
        resolved_job_id = context_manager.get_latest_job_id(resolved_session_id)
    else:
        resolved_job_id = None
    if not resolved_job_id:
        return json.dumps({"error": "job_id is required and no latest job found in session state"})

    # If caller didn't provide proof_type explicitly, prefer stored job metadata.
    proof_type_normalized = _normalize_proof_type(proof_type)
    # B5: Stored proof_type fallback (disabled in Config A, B)
    if proof_type_normalized == "logistic_accuracy" and SYSTEM_CONFIG in ("C", "D"):
        known_job = context_manager.get_job(resolved_session_id, resolved_job_id)
        if known_job and known_job.get("proof_type"):
            proof_type_normalized = _normalize_proof_type(str(known_job.get("proof_type")))
    
    base_url = _get_base_url(proof_type_normalized)
    url = f"{base_url}/jobs/{resolved_job_id}"
    
    try:
        result = _run_curl_command(url)
        
        if result.returncode != 0:
            # Curl exit code 28 means timeout
            if result.returncode == 28:
                return (
                    f"Status check timed out after 5 minutes.\n"
                    f"The external API at {base_url} is taking longer than expected to respond.\n"
                    f"This is an API-side issue - the server is slow or overloaded.\n"
                    f"Job ID: {resolved_job_id}\n"
                    f"Note: The API sometimes responds inconsistently (200, 404, or timeout).\n"
                    f"Please try again in a few moments."
                )
            else:
                return (
                    f"Failed to check job status.\n"
                    f"URL: {url}\n"
                    f"Return code: {result.returncode}\n"
                    f"stdout: {result.stdout}\n"
                    f"stderr: {result.stderr}"
                )
        
        try:
            status_data = json.loads(result.stdout)
            if isinstance(status_data, dict):
                if "status" in status_data:
                    context_manager.update_job_status(
                        session_id=resolved_session_id,
                        job_id=resolved_job_id,
                        status=str(status_data.get("status")),
                    )
                elif status_data.get("job_id"):
                    context_manager.add_job_id(
                        session_id=resolved_session_id,
                        job_id=str(status_data.get("job_id")),
                        proof_type=proof_type_normalized,
                        user_hash="",
                        nonce="",
                        status="unknown",
                    )
            return json.dumps(status_data, indent=2)
        except json.JSONDecodeError:
            return f"Invalid JSON response: {result.stdout}"
    except Exception as exc:
        return f"Error checking job status: {exc}"


@mcp.tool()
async def download_proof(
    job_id: Optional[str] = None,
    output_filename: Optional[str] = None,
    proof_type: str = "auto",
    session_id: Optional[str] = None,
    ctx: Context | None = None,
) -> str:
    """Download the proof file for a completed job.
    
    This tool downloads the proof file from the external API and saves it to the server.
    The file will be saved to the downloads directory which is accessible on the host machine
    via the Docker volume mount.
    
    Use this tool when the user explicitly asks to download a proof (e.g., "download it", 
    "download the proof", "get the proof file").
    
    If proof_type is not specified, the function will automatically detect it by checking
    which port (5012, 5013, or 5014) the job exists on.
    
    Args:
        job_id: The job ID returned from prove_hash. If omitted, resolves from session state.
        output_filename: Filename to save the proof (default: "proofs-{job_id}.zip")
        proof_type: Type of proof:
            - "logistic_accuracy" (port 5012)
            - "KS"                (port 5013)
            - "LRT"               (port 5014)
           If "auto" or not specified, will auto-detect by checking all three ports.
        session_id: Chat session identifier. Injected by Flask middleware; not user-provided.
    
    Returns:
        Success message with the file path or error message. The file path will be on the server
        (e.g., /app/downloads/proofs-{job_id}.zip) and accessible via the volume mount.
    """
    resolved_session_id = _resolve_session_id(session_id, ctx)
    if not resolved_session_id:
        return json.dumps({"error": "session_id is required"})
    context_manager.touch_session(resolved_session_id)

    resolved_job_id = str(job_id) if job_id else None
    # B4: Fallback job ID (disabled in Config A)
    if not resolved_job_id and SYSTEM_CONFIG != "A":
        resolved_job_id = context_manager.get_latest_completed_job_id(resolved_session_id)
        if not resolved_job_id:
            latest_job_id = context_manager.get_latest_job_id(resolved_session_id)
            latest_job = context_manager.get_job(resolved_session_id, latest_job_id) if latest_job_id else None
            if latest_job and str(latest_job.get("status", "")).lower() in {"done", "completed", "success"}:
                resolved_job_id = latest_job_id

    if not resolved_job_id:
        return json.dumps({"error": "job_id is required and no completed job found in session state"})

    # Prefer proof_type from session job metadata.
    job_data = context_manager.get_job(resolved_session_id, resolved_job_id)
    proof_type_normalized = (proof_type or "").lower().strip() if proof_type else "auto"
    # B5: Stored proof_type override (disabled in Config A, B)
    if job_data and job_data.get("proof_type") and SYSTEM_CONFIG in ("C", "D"):
        proof_type_normalized = str(job_data.get("proof_type"))
    proof_type_normalized = "auto" if proof_type_normalized == "auto" else _normalize_proof_type(proof_type_normalized)

    if proof_type_normalized == "auto":
        if SYSTEM_CONFIG not in ("C", "D"):
            # B6: Auto-detection disabled in Config A, B — require explicit proof_type
            return json.dumps({
                "error": "proof_type is required. Please specify KS, LRT, or logistic_accuracy. Auto-detection is not available in this configuration."
            })
        # B6: Auto-detection (enabled in Config C, D)
        # Try to detect which port the job exists on by checking all three ports
        base_url_5012 = _get_base_url("logistic_accuracy")
        base_url_5013 = _get_base_url("KS")
        base_url_5014 = _get_base_url("LRT")
        
        # Check port 5013 first (KS)
        status_url_5013 = f"{base_url_5013}/jobs/{resolved_job_id}"
        result_5013 = _run_curl_command(status_url_5013)
        
        job_found_on_5013 = False
        if result_5013.returncode == 0 and result_5013.stdout:
            try:
                status_data = json.loads(result_5013.stdout)
                if status_data.get("job_id") == resolved_job_id or "job_id" in status_data:
                    job_found_on_5013 = True
            except Exception:
                pass
        
        # Check port 5014 (LRT)
        status_url_5014 = f"{base_url_5014}/jobs/{resolved_job_id}"
        result_5014 = _run_curl_command(status_url_5014)
        
        job_found_on_5014 = False
        if result_5014.returncode == 0 and result_5014.stdout:
            try:
                status_data = json.loads(result_5014.stdout)
                if status_data.get("job_id") == resolved_job_id or "job_id" in status_data:
                    job_found_on_5014 = True
            except Exception:
                pass
        
        # Check port 5012 (logistic accuracy)
        status_url_5012 = f"{base_url_5012}/jobs/{resolved_job_id}"
        result_5012 = _run_curl_command(status_url_5012)
        
        job_found_on_5012 = False
        if result_5012.returncode == 0 and result_5012.stdout:
            try:
                status_data = json.loads(result_5012.stdout)
                if status_data.get("job_id") == resolved_job_id or "job_id" in status_data:
                    job_found_on_5012 = True
            except Exception:
                pass
        
        # Determine proof type based on which port has the job
        if job_found_on_5014 and not job_found_on_5013 and not job_found_on_5012:
            proof_type_normalized = "LRT"
        elif job_found_on_5013 and not job_found_on_5014 and not job_found_on_5012:
            proof_type_normalized = "KS"
        elif job_found_on_5012 and not job_found_on_5013 and not job_found_on_5014:
            proof_type_normalized = "logistic_accuracy"
        elif job_found_on_5014:
            # If LRT is found along with others, default to LRT
            proof_type_normalized = "LRT"
        elif job_found_on_5013:
            # If KS is found (and LRT is not), default to KS
            proof_type_normalized = "KS"
        elif job_found_on_5012:
            # Fallback to logistic if only it is found
            proof_type_normalized = "logistic_accuracy"
        else:
            # Job not found on any, default to logistic_accuracy
            proof_type_normalized = "logistic_accuracy"
    else:
        # proof_type_normalized was already set by Redis override (line 505) or
        # _normalize_proof_type (line 506). No additional normalization needed.
        pass
    
    base_url = _get_base_url(proof_type_normalized)
    url = f"{base_url}/jobs/{resolved_job_id}/download"
    try:
        # Canonical output contract: always write inside DOWNLOADS_DIR and return filename metadata only.
        downloads_dir = os.path.abspath(os.getenv("DOWNLOADS_DIR", "/app/downloads"))
        os.makedirs(downloads_dir, exist_ok=True)

        if output_filename:
            requested_name = os.path.basename(str(output_filename))
            filename_only = requested_name if requested_name else f"proofs-{resolved_job_id}.zip"
        else:
            filename_only = f"proofs-{resolved_job_id}.zip"
        if not filename_only.endswith(".zip"):
            filename_only = f"{filename_only}.zip"

        output_filename = os.path.join(downloads_dir, filename_only)
        
        # Use curl -L to follow redirects and -o to save to file
        # Build command as list (safer than shell string)
        cmd_parts = ["curl", "-L", "--max-time", "120", "-o", output_filename, url]
        
        result = subprocess.run(
            cmd_parts,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        
        if result.returncode != 0:
            return (
                f"Failed to download proof file.\n"
                f"stdout: {result.stdout}\n"
                f"stderr: {result.stderr}"
            )
        
        # Verify file was actually downloaded and has content
        if not os.path.exists(output_filename):
            return f"Download command succeeded but file not found at {output_filename}"
        
        file_size = os.path.getsize(output_filename)
        if file_size == 0:
            return f"Downloaded file is empty (0 bytes). The proof may not be ready yet."
        
        # Check if the downloaded file contains an error message (API sometimes returns error pages)
        try:
            with open(output_filename, 'r', encoding='utf-8') as f:
                content_preview = f.read(200)  # Read first 200 chars
                if '"error"' in content_preview.lower() or '"ok":false' in content_preview.lower() or 'unknown job' in content_preview.lower():
                    # This looks like a JSON error response, not a proof file
                    f.seek(0)  # Reset to beginning
                    error_msg = f.read() if file_size < 1000 else content_preview
                    os.remove(output_filename)  # Clean up the error file
                    return f"API returned an error instead of proof file: {error_msg.strip()}"
        except (UnicodeDecodeError, Exception) as e:
            # File is binary (good) or can't be read as text, assume it's valid
            pass
        
        context_manager.mark_job_downloaded(resolved_session_id, resolved_job_id)
        return json.dumps(
            {
                "success": True,
                "job_id": resolved_job_id,
                "filename": filename_only,
                "file_size": file_size,
                "proof_type": proof_type_normalized,
            }
        )
        
    except Exception as exc:
        return f"Error downloading proof file: {exc}"


@mcp.tool()
async def verify_proof(
    job_id: Optional[str] = None,
    proof_type: str = "auto",
    session_id: Optional[str] = None,
    user_id: Optional[str] = None,
    ctx: Context | None = None,
) -> str:
    """Confirm that a completed proof job is cryptographically valid.

    Precondition: the job must have status "done". For jobs still running, call
    check_status first.

    Use this tool when the user asks whether their proof is valid, has been
    verified, or is correct. Examples (paraphrase, do not echo): asking if a
    proof is valid, asking to verify a previously downloaded proof, asking
    whether the cryptographic check passes.

    Verification confirms cryptographic validity. Downloading retrieves the
    proof artifact files. These are independent operations: verifying does
    NOT download, downloading does NOT verify.

    Auth-bound: by default resolves the current user's latest completed job
    from session state. The caller may pass an explicit job_id to verify a
    specific job.

    Args:
        job_id: The completed job to verify. If omitted, resolves from session
            state (the user's most recent completed job).
        proof_type: One of "KS", "LRT", "logistic_accuracy", or "auto".
            When "auto", the server determines the correct application by
            consulting stored job metadata, then probing the three application
            endpoints if metadata is unavailable.
        session_id: Chat session identifier. Injected by Flask middleware.
        user_id: Auth identity. Injected by Flask middleware.

    Returns:
        A JSON string containing the verification result. The "ok" field is
        true when verification succeeds, false otherwise. Additional context
        ("results", "user_hash") may be included by the upstream service.
    """
    resolved_session_id = _resolve_session_id(session_id, ctx)
    if not resolved_session_id:
        return json.dumps({"error": "session_id is required"})
    context_manager.touch_session(resolved_session_id)

    resolved_job_id = str(job_id) if job_id else None
    # B4: Fallback job ID (disabled in Config A) — verify only makes sense on completed jobs
    if not resolved_job_id and SYSTEM_CONFIG != "A":
        resolved_job_id = context_manager.get_latest_completed_job_id(resolved_session_id)
        if not resolved_job_id:
            latest_job_id = context_manager.get_latest_job_id(resolved_session_id)
            latest_job = context_manager.get_job(resolved_session_id, latest_job_id) if latest_job_id else None
            if latest_job and str(latest_job.get("status", "")).lower() in {"done", "completed", "success"}:
                resolved_job_id = latest_job_id

    if not resolved_job_id:
        return json.dumps({"error": "job_id is required and no completed job found in session state"})

    # B5: Stored proof_type override (Config C, D only)
    job_data = context_manager.get_job(resolved_session_id, resolved_job_id)
    proof_type_normalized = (proof_type or "").lower().strip() if proof_type else "auto"
    if job_data and job_data.get("proof_type") and SYSTEM_CONFIG in ("C", "D"):
        proof_type_normalized = str(job_data.get("proof_type"))
    proof_type_normalized = "auto" if proof_type_normalized == "auto" else _normalize_proof_type(proof_type_normalized)

    # B6: Auto-detection (Config C, D only)
    if proof_type_normalized == "auto":
        if SYSTEM_CONFIG not in ("C", "D"):
            return json.dumps({
                "error": "proof_type is required. Please specify KS, LRT, or logistic_accuracy. Auto-detection is not available in this configuration."
            })
        base_url_5012 = _get_base_url("logistic_accuracy")
        base_url_5013 = _get_base_url("KS")
        base_url_5014 = _get_base_url("LRT")

        def _job_present(base_url):
            r = _run_curl_command(f"{base_url}/jobs/{resolved_job_id}")
            if r.returncode != 0 or not r.stdout:
                return False
            try:
                d = json.loads(r.stdout)
                return d.get("job_id") == resolved_job_id or "job_id" in d
            except Exception:
                return False

        found_5013 = _job_present(base_url_5013)
        found_5014 = _job_present(base_url_5014)
        found_5012 = _job_present(base_url_5012)

        if found_5014 and not found_5013 and not found_5012:
            proof_type_normalized = "LRT"
        elif found_5013 and not found_5014 and not found_5012:
            proof_type_normalized = "KS"
        elif found_5012 and not found_5013 and not found_5014:
            proof_type_normalized = "logistic_accuracy"
        elif found_5014:
            proof_type_normalized = "LRT"
        elif found_5013:
            proof_type_normalized = "KS"
        elif found_5012:
            proof_type_normalized = "logistic_accuracy"
        else:
            proof_type_normalized = "logistic_accuracy"

    base_url = _get_base_url(proof_type_normalized)
    url = f"{base_url}/verify-job/{resolved_job_id}"

    try:
        result = _run_curl_command(url, "-X POST")
        if result.returncode != 0:
            return json.dumps({
                "ok": False,
                "error": f"verify request failed: {result.stderr or 'curl returncode ' + str(result.returncode)}",
            })

        stdout = result.stdout or ""
        # Preserve the raw upstream payload — including 404 HTML for wrong-port
        # queries — so downstream faithfulness grading can see the same surface
        # that check_status exposes.
        try:
            parsed = json.loads(stdout)
            # Pass through unchanged. Upstream returns: {"ok": bool, "results": [...], "user_hash": str}
            return json.dumps(parsed)
        except json.JSONDecodeError:
            # Wrong-port 404 (HTML) or other non-JSON. Return the raw response
            # the same way check_status does — wrapped with an "Invalid JSON
            # response: ..." prefix so the LLM sees an explicit error.
            return f"Invalid JSON response: {stdout}"

    except Exception as exc:
        return json.dumps({"ok": False, "error": f"verify_proof exception: {exc}"})


@mcp.tool()
async def get_session_context(session_id: str, ctx: Context | None = None) -> str:
    """Get a full overview of ALL jobs in the current session from Redis.

    Use this tool ONLY when the user explicitly asks about ALL their jobs or wants a
    full session overview, for example:
    - "what jobs do I have?"
    - "show me all my jobs"
    - "what have I submitted so far?"
    - "session summary"
    - "show me my job history"
    - "which jobs are completed?"

    Do NOT use this tool for single-job status questions like "check my status",
    "how is my proof doing?", or "is my KS proof done?" — use check_status instead.

    This tool returns a JSON snapshot containing:
    - **state**: Session state with latest_job_id, latest_completed_job_id, etc.
    - **jobs**: List of ALL jobs for this session (sorted newest first), each with
      job_id, proof_type, status, and timestamps.
    - **summary**: Human-readable text summary.

    Note: Job statuses in Redis may be stale. For real-time status of a specific job,
    use the check_status tool instead.

    Args:
        session_id: Chat session identifier. Automatically injected by Flask middleware.

    Returns:
        JSON string with state dict, jobs array, summary string, and messages array.
    """
    resolved_session_id = _resolve_session_id(session_id, ctx)
    if not resolved_session_id:
        return json.dumps({"error": "session_id is required"})
    context_manager.touch_session(resolved_session_id)
    payload = {
        "state": context_manager.get_state(resolved_session_id),
        "jobs": context_manager.get_job_ids(resolved_session_id),
        "summary": context_manager.get_context_summary(resolved_session_id),
        "recent_messages": context_manager.get_recent_messages(resolved_session_id, limit=10),
    }
    return json.dumps(payload, indent=2)


@mcp.tool()
async def check_my_hash_existence(
    application: Optional[str] = None,
    session_id: Optional[str] = None,
    user_id: Optional[str] = None,
    ctx: Context | None = None,
) -> str:
    """Check where the **signed-in user's** saved hash is used.
    
    This is the primary tool for questions about **\"my hash\"** when the user is
    authenticated. It automatically:
    
    - Resolves the current user's id from the HTTP/MCP context
    - Loads the user's saved raw hash from Postgres
    - Calls :func:`check_hash_existence` with that hash
    
    Use this tool when the user says things like:
    
    - \"What is my raw hash used in?\"
    - \"See if my information / my data is used in any application\"
    - \"Was my hash used anywhere?\"
    
    When the user is signed in and uses \"my\" language, **do NOT** ask them to type
    their hash. Call this tool instead. Only use :func:`check_hash_existence` when:
    
    - The user explicitly provides a hash string (e.g. \"check hash 0d\"), or
    - There is no signed-in user and you have no saved hash to rely on.
    """
    resolved_user_id = _resolve_user_id(user_id, ctx)
    if not resolved_user_id:
        return "Please sign in first."

    # Keep session warm in Redis for continuity with the existing proof flow.
    resolved_session_id = _resolve_session_id(session_id, ctx)
    if resolved_session_id:
        context_manager.touch_session(resolved_session_id)

    try:
        saved_hash = get_primary_hash_for_user(resolved_user_id)
    except Exception:
        return "Could not read your saved hash right now. Please try again."

    if not saved_hash:
        return "No hash saved. Please save your hash first."

    return await check_hash_existence(user_hash=saved_hash, application=application)


@mcp.tool()
async def prove_my_data(
    nonce: Optional[str] = None,
    proof_type: str = "",
    session_id: Optional[str] = None,
    user_id: Optional[str] = None,
    ctx: Context | None = None,
) -> str:
    """Submit a proof job **for the signed-in user's saved hash**.
    
    This is the **preferred** tool whenever the user is authenticated and refers to
    \"my hash\", \"my raw hash\", \"my user hash\", or \"my data\". It:
    
    - Resolves the current user's saved raw hash from Postgres
    - Uses the provided ``nonce``, OR if omitted, uses a default nonce
    - Requires an explicit ``proof_type`` (no default)
    
    Use this tool instead of :func:`prove_hash` when you are working with the
    signed-in user's own data. Only fall back to :func:`prove_hash` when:
    
    - The user is not signed in, OR
    - The user explicitly wants to submit a job for a different hash they provided.

    Conversational handling guidance for the agent:

    - If a user says a short follow-up like "yes, prove that for LRT", interpret it as
      a proof submission request for the signed-in user's data and call this tool with
      ``proof_type="LRT"``.
    - Do not ask for raw hash when signed in; this tool resolves it automatically.
    - If nonce is omitted, this tool uses a default nonce. Ask for explicit nonce only
      when the user wants to override defaults.
    - Do NOT offer completion notifications. Notification delivery is not implemented;
      suggest manual status checks instead.
    - If you need to ask for details, provide concrete prompt examples, e.g.:
      - "submit proof for my data in LRT"
      - "submit proof for my data in KS"
      - "submit proof for my data in logistic_accuracy"
    """
    resolved_user_id = _resolve_user_id(user_id, ctx)
    if not resolved_user_id:
        return "Please sign in first."

    resolved_session_id = _resolve_session_id(session_id, ctx)
    if not resolved_session_id:
        return json.dumps({"error": "session_id is required"})

    try:
        saved_hash = get_primary_hash_for_user(resolved_user_id)
    except Exception:
        return "Could not read your saved hash right now. Please try again."

    if not saved_hash:
        return "No hash saved. Please save your hash first."

    if not proof_type:
        return (
            "proof_type is required and there is no default. "
            "Please specify one of: logistic_accuracy, KS, or LRT."
        )

    # If nonce is omitted, use the default nonce behavior from prove_hash.
    if nonce is None:
        nonce = None

    # Reuse the existing prove_hash pipeline to keep proof + Redis behavior identical.
    return await prove_hash(
        user_hash=saved_hash,
        nonce=nonce,
        proof_type=proof_type,
        session_id=resolved_session_id,
        ctx=ctx,
    )


@mcp.tool()
async def check_hash_existence(user_hash: str, application: Optional[str] = None) -> str:
    """Check if a **specific hash value** exists in any of the applications.
    
    This tool works with an explicit hash string provided by the user (e.g. \"0d\",
    \"6f\", or \"0099\"). It answers questions like:
    
    - \"See if raw hash X is used anywhere or in any applications\"
    - \"Check if hash X exists\"
    - \"Has hash X been used in logistic accuracy?\"
    - \"Is hash X in the KS application?\"
    
    When the user is signed in and uses \"my\" language (\"my hash\", \"my data\"),
    prefer :func:`check_my_hash_existence` instead of this tool so the backend can
    resolve their saved hash automatically and you do **not** have to ask them for it.
    
    If ``application`` is not specified, this tool checks all three applications:
    logistic accuracy, KS, and LRT, and reports which ones contain the hash.
    """
    base_host = os.getenv("PROVER_BASE_HOST", "COSMeTICprover")
    
    # Normalize application name if provided
    application_normalized = None
    if application:
        app_lower = application.lower().strip()
        if app_lower in ["logistic_accuracy", "logistic", "logistic accuracy"]:
            application_normalized = "logistic_accuracy"
        elif app_lower in ["ks", "kolmogorov", "kolmogorov-smirnov", "kolmogorov_smirnov"]:
            application_normalized = "KS"
        elif app_lower in ["lrt", "likelihood_ratio_test", "likelihood-ratio-test", "likelihood ratio test"]:
            application_normalized = "LRT"
    
    # Prepare payload
    payload = {"user_hash": user_hash}
    
    # Determine which applications to check
    applications_to_check = []
    if application_normalized:
        # Check only the specified application
        applications_to_check = [application_normalized]
    else:
        # Check all three applications
        applications_to_check = ["logistic_accuracy", "KS", "LRT"]
    
    # Map application names to ports and display names
    app_config = {
        "logistic_accuracy": {
            "port": 5012,
            "display_name": "logistic accuracy",
            "url": f"http://{base_host}:5012/check-hash/"
        },
        "KS": {
            "port": 5013,
            "display_name": "KS (Kolmogorov-Smirnov)",
            "url": f"http://{base_host}:5013/check-hash/"
        },
        "LRT": {
            "port": 5014,
            "display_name": "LRT (Likelihood Ratio Test)",
            "url": f"http://{base_host}:5014/check-hash/"
        }
    }
    
    results = {}
    errors = {}
    
    # Check each application
    for app in applications_to_check:
        config = app_config[app]
        url = config["url"]
        
        try:
            result = _run_curl_command(url, "-X POST", json_data=payload)
            
            if result.returncode == 0:
                try:
                    response_data = json.loads(result.stdout)
                    results[app] = {
                        "exists": response_data.get("exists", False),
                        "data": response_data,
                        "display_name": config["display_name"]
                    }
                except json.JSONDecodeError:
                    errors[app] = f"Invalid JSON response: {result.stdout}"
            else:
                # Handle curl errors
                if result.returncode == 28:
                    errors[app] = "Request timed out"
                elif result.returncode == 6:
                    errors[app] = "Cannot resolve host"
                elif result.returncode == 7:
                    errors[app] = "Cannot connect to server"
                else:
                    errors[app] = f"Error (code {result.returncode}): {result.stderr}"
        except Exception as exc:
            errors[app] = f"Exception: {exc}"
    
    # Build response message
    response_parts = []
    response_parts.append(f"Hash existence check for hash: **{user_hash}**\n")
    
    found_applications = []
    not_found_applications = []
    
    # Process results
    for app, result_info in results.items():
        if result_info["exists"]:
            found_applications.append(result_info["display_name"])
            # Add details if available
            data = result_info["data"]
            details = []
            if data.get("found_in_both"):
                details.append("found in both")
            if data.get("found_in_log_acc"):
                details.append("found in log_acc")
            if data.get("found_in_log_acc_length"):
                details.append("found in log_acc_length")
            if details:
                response_parts.append(f"✓ **{result_info['display_name']}**: Hash exists. Details: {', '.join(details)}")
        else:
            not_found_applications.append(result_info["display_name"])
            response_parts.append(f"✗ **{result_info['display_name']}**: Hash not found")
    
    # Report errors
    for app, error_msg in errors.items():
        display_name = app_config[app]["display_name"]
        response_parts.append(f"⚠ **{display_name}**: Error checking - {error_msg}")
    
    # Summary
    response_parts.append("\n**Summary:**")
    
    if found_applications:
        if len(found_applications) == 1:
            response_parts.append(f"The hash **{user_hash}** was found in **{found_applications[0]}**.")
        else:
            response_parts.append(f"The hash **{user_hash}** was found in **{len(found_applications)} applications**: {', '.join(found_applications)}.")
    else:
        response_parts.append(f"The hash **{user_hash}** was **not found** in any of the checked applications.")
    
    if not_found_applications and found_applications:
        response_parts.append(f"Not found in: {', '.join(not_found_applications)}")
    
    if errors:
        response_parts.append(f"Errors encountered for: {', '.join([app_config[app]['display_name'] for app in errors.keys()])}")

    # Deterministic follow-up guidance so users can prove both "used" and "not used" claims.
    response_parts.append("\n**Proof follow-up options:**")
    app_to_type = {
        "logistic_accuracy": "logistic_accuracy",
        "KS": "KS",
        "LRT": "LRT",
    }
    for app in applications_to_check:
        display_name = app_config[app]["display_name"]
        proof_type_value = app_to_type[app]
        if app in results:
            exists_here = bool(results[app].get("exists"))
            if exists_here:
                response_parts.append(
                    f"- {display_name}: I can generate a proof that your information **was used** here. "
                    f"Say: \"submit proof for my data in {proof_type_value}\"."
                )
            else:
                response_parts.append(
                    f"- {display_name}: I can generate a proof that your information **was not used** here. "
                    f"Say: \"submit proof for my data in {proof_type_value}\"."
                )
        elif app in errors:
            response_parts.append(
                f"- {display_name}: check timed out/errored, but you can still try proof generation directly. "
                f"Say: \"submit proof for my data in {proof_type_value}\"."
            )

    response_parts.append(
        "- Nonce behavior: if you don't provide nonce, we use a default nonce automatically."
    )
    response_parts.append(
        "- Explicit nonce prompt format (if needed): "
        "\"submit proof for my data in KS with nonce [[0.0, 0.0, 0.0]]\"."
    )

    return "\n".join(response_parts)


if __name__ == "__main__":
    # Run as an HTTP (streamable) MCP server so clients can call it over HTTP.
    host = os.getenv("PROVER_MCP_HOST", "0.0.0.0")
    port = int(os.getenv("PROVER_MCP_PORT", "8003"))
    mcp.settings.host = host
    mcp.settings.port = port
    mcp.run(transport="streamable-http")

