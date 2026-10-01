import json
import os
from datetime import datetime
from typing import Dict, List, Optional

import redis


class ContextManager:
    """Minimal Redis context manager using state + jobs (+short messages)."""

    TTL_SECONDS = 7 * 24 * 60 * 60
    MESSAGE_LIMIT = 15

    def __init__(self):
        redis_host = os.getenv("REDIS_HOST", "localhost")
        redis_port = int(os.getenv("REDIS_PORT", 6379))
        self.redis_client = redis.Redis(
            host=redis_host,
            port=redis_port,
            decode_responses=True,
            socket_connect_timeout=5,
        )
        try:
            self.redis_client.ping()
            print(f"Connected to Redis at {redis_host}:{redis_port}")
        except redis.ConnectionError as e:
            print(f"Warning: Could not connect to Redis at {redis_host}:{redis_port}: {e}")
            self.redis_client = None

    def _is_connected(self) -> bool:
        if self.redis_client is None:
            return False
        try:
            self.redis_client.ping()
            return True
        except Exception:
            return False

    def _state_key(self, session_id: str) -> str:
        return f"session:{session_id}:state"

    def _jobs_key(self, session_id: str) -> str:
        return f"session:{session_id}:jobs"

    def _messages_key(self, session_id: str) -> str:
        return f"session:{session_id}:messages"

    def _now(self) -> str:
        return datetime.now().isoformat()

    def touch_session(self, session_id: str):
        """Refresh TTL for state/jobs/messages on each activity."""
        if not self._is_connected():
            return
        # Remove legacy keys from old architecture for this session.
        self.redis_client.delete(
            f"session:{session_id}:hashes",
            f"session:{session_id}:proof_types",
            f"session:{session_id}:jobs:timeline",
            f"session:{session_id}:tool_calls",
        )
        self.redis_client.expire(self._state_key(session_id), self.TTL_SECONDS)
        self.redis_client.expire(self._jobs_key(session_id), self.TTL_SECONDS)
        self.redis_client.expire(self._messages_key(session_id), self.TTL_SECONDS)

    # ========== State ==========

    def get_state(self, session_id: str) -> Dict[str, str]:
        if not self._is_connected():
            return {}
        self.touch_session(session_id)
        return self.redis_client.hgetall(self._state_key(session_id))

    def update_state(self, session_id: str, updates: Dict[str, str]):
        if not self._is_connected():
            return
        if not updates:
            return
        updates = {k: v for k, v in updates.items() if v is not None}
        updates["updated_at"] = self._now()
        self.redis_client.hset(self._state_key(session_id), mapping=updates)
        self.touch_session(session_id)

    def get_latest_job_id(self, session_id: str) -> Optional[str]:
        return self.get_state(session_id).get("latest_job_id")

    def get_latest_completed_job_id(self, session_id: str) -> Optional[str]:
        return self.get_state(session_id).get("latest_completed_job_id")

    def get_session_owner(self, session_id: str) -> Optional[str]:
        """Return the owner_user_id for this session, if any."""
        state = self.get_state(session_id)
        return state.get("owner_user_id")

    def claim_or_verify_session_owner(self, session_id: str, user_id: str) -> tuple[bool, Optional[str]]:
        """Ensure this session is owned by user_id.

        Returns (ok, error_code):
          - (True, None)            → owner set or matches
          - (False, "redis_down")   → Redis unavailable
          - (False, "no_user")      → no user_id passed
          - (False, "mismatch")     → different owner already set
        """
        if not self._is_connected():
            return False, "redis_down"
        if not user_id:
            return False, "no_user"

        key = self._state_key(session_id)
        owner = self.redis_client.hget(key, "owner_user_id")
        if owner is None:
            # First claim: set owner_user_id
            now = self._now()
            self.redis_client.hset(key, mapping={"owner_user_id": user_id, "updated_at": now})
            self.touch_session(session_id)
            return True, None

        if str(owner) == str(user_id):
            self.touch_session(session_id)
            return True, None

        return False, "mismatch"

    # ========== Jobs ==========

    def get_job(self, session_id: str, job_id: str) -> Optional[Dict]:
        if not self._is_connected():
            return None
        self.touch_session(session_id)
        raw = self.redis_client.hget(self._jobs_key(session_id), job_id)
        if not raw:
            return None
        try:
            return json.loads(raw)
        except Exception:
            return None

    def get_job_ids(self, session_id: str) -> List[Dict]:
        if not self._is_connected():
            return []
        self.touch_session(session_id)
        jobs = self.redis_client.hgetall(self._jobs_key(session_id))
        parsed: List[Dict] = []
        for value in jobs.values():
            try:
                parsed.append(json.loads(value))
            except Exception:
                continue
        parsed.sort(key=lambda x: x.get("created_at", ""), reverse=True)
        return parsed

    def add_job_id(
        self,
        session_id: str,
        job_id: str,
        proof_type: str,
        user_hash: str,
        nonce=None,
        status: str = "submitted",
    ):
        """Create or upsert a job record and update state pointers."""
        if not self._is_connected():
            return
        now = self._now()
        existing = self.get_job(session_id, job_id) or {}
        created_at = existing.get("created_at", now)
        job_data = {
            "job_id": job_id,
            "proof_type": proof_type,
            "user_hash": user_hash,
            "nonce": nonce if nonce is not None else existing.get("nonce", ""),
            "status": status or existing.get("status", "submitted"),
            "created_at": created_at,
            "updated_at": now,
        }
        self.redis_client.hset(self._jobs_key(session_id), job_id, json.dumps(job_data))
        self.update_state(
            session_id,
            {
                "latest_job_id": job_id,
                "latest_proof_type": proof_type,
                "latest_user_hash": user_hash,
            },
        )
        if (job_data["status"] or "").lower() in {"done", "completed", "success"}:
            self.update_state(session_id, {"latest_completed_job_id": job_id})
        self.touch_session(session_id)

    def update_job_status(self, session_id: str, job_id: str, status: str):
        if not self._is_connected():
            return
        existing = self.get_job(session_id, job_id)
        if not existing:
            # Minimal unknown record if a user references external/unknown job id.
            self.add_job_id(
                session_id=session_id,
                job_id=job_id,
                proof_type="unknown",
                user_hash="",
                nonce="",
                status=status or "unknown",
            )
            return
        existing["status"] = status
        existing["updated_at"] = self._now()
        self.redis_client.hset(self._jobs_key(session_id), job_id, json.dumps(existing))
        self.update_state(session_id, {"latest_job_id": job_id})
        if (status or "").lower() in {"done", "completed", "success"}:
            self.update_state(session_id, {"latest_completed_job_id": job_id})
        self.touch_session(session_id)

    def mark_job_downloaded(self, session_id: str, job_id: str):
        if not self._is_connected():
            return
        existing = self.get_job(session_id, job_id)
        if not existing:
            return
        existing["downloaded_at"] = self._now()
        existing["updated_at"] = self._now()
        self.redis_client.hset(self._jobs_key(session_id), job_id, json.dumps(existing))
        self.touch_session(session_id)

    # ========== Messages (Optional) ==========

    def save_message(self, session_id: str, role: str, content: str):
        if not self._is_connected():
            return
        message = {"role": role, "content": content, "timestamp": self._now()}
        key = self._messages_key(session_id)
        self.redis_client.rpush(key, json.dumps(message))
        self.redis_client.ltrim(key, -self.MESSAGE_LIMIT, -1)
        self.touch_session(session_id)

    def get_recent_messages(self, session_id: str, limit: int = 15) -> List[Dict]:
        if not self._is_connected():
            return []
        self.touch_session(session_id)
        key = self._messages_key(session_id)
        messages = self.redis_client.lrange(key, -limit, -1)
        parsed: List[Dict] = []
        for msg in messages:
            try:
                parsed.append(json.loads(msg))
            except Exception:
                continue
        return parsed

    # ========== Context Summary ==========

    def get_context_summary(self, session_id: str) -> str:
        """Deterministic short summary based only on state + jobs."""
        if not self._is_connected():
            return ""
        state = self.get_state(session_id)
        latest_job_id = state.get("latest_job_id")
        latest_completed = state.get("latest_completed_job_id", "none")

        if not latest_job_id:
            return f"Latest job: none. Latest completed: {latest_completed or 'none'}."

        latest_job = self.get_job(session_id, latest_job_id) or {}
        proof_type = latest_job.get("proof_type", state.get("latest_proof_type", "unknown"))
        user_hash = latest_job.get("user_hash", state.get("latest_user_hash", "unknown"))
        status = latest_job.get("status", "unknown")

        return (
            f"Latest job: {latest_job_id} ({proof_type}, hash {user_hash}, status {status}). "
            f"Latest completed: {latest_completed or 'none'}."
        )

    # ========== Cleanup ==========

    def cleanup_session(self, session_id: str):
        if not self._is_connected():
            return
        keys = [
            self._state_key(session_id),
            self._jobs_key(session_id),
            self._messages_key(session_id),
        ]
        self.redis_client.delete(*keys)

# Global instance
context_manager = ContextManager()
