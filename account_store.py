import os
from typing import Any, Dict, Optional

import bcrypt
import psycopg2
import psycopg2.extras


def _get_conn():
    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        raise RuntimeError("DATABASE_URL is not set")
    return psycopg2.connect(database_url)


def mask_hash(raw_hash: str) -> str:
    if raw_hash is None:
        return ""
    value = str(raw_hash)
    if len(value) <= 4:
        return "*" * len(value)
    if len(value) <= 12:
        return f"{value[:2]}...{value[-2:]}"
    return f"{value[:6]}...{value[-6:]}"


def signin_dataset_user(username: str, password_plaintext: str) -> Dict[str, Any]:
    """Sign in using dataset_users (username + password). No user creation."""
    uname = (username or "").strip()
    if not uname or not password_plaintext:
        return {"success": False, "error": "username and password are required"}

    with _get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "SELECT id, username, password_hash FROM dataset_users WHERE username = %s",
                (uname,),
            )
            row = cur.fetchone()
            if not row:
                return {"success": False, "error": "Invalid username or password"}
            stored_hash = str(row["password_hash"])
            ok = bcrypt.checkpw(
                password_plaintext.encode("utf-8"),
                stored_hash.encode("utf-8"),
            )
            if not ok:
                return {"success": False, "error": "Invalid username or password"}
            return {
                "success": True,
                "user": {"id": str(row["id"]), "username": str(row["username"])},
            }


def get_user_by_id(user_id: str) -> Optional[Dict[str, Any]]:
    """Return user info for a dataset user by id."""
    if not user_id:
        return None
    with _get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT id, username FROM dataset_users WHERE id = %s", (user_id,))
            row = cur.fetchone()
            if not row:
                return None
            return {"id": str(row["id"]), "username": str(row["username"])}


def get_primary_hash_for_user(user_id: str) -> Optional[str]:
    if not user_id:
        return None
    with _get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT raw_hash FROM dataset_users WHERE id = %s", (user_id,))
            row = cur.fetchone()
            return str(row["raw_hash"]) if row and row["raw_hash"] else None


def get_input_data_for_user(user_id: str) -> Optional[Any]:
    """Return input_data (JSON) for dataset user. None for legacy users or if not set."""
    if not user_id:
        return None
    with _get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT input_data FROM dataset_users WHERE id = %s", (user_id,))
            row = cur.fetchone()
            if row and row.get("input_data") is not None:
                return row["input_data"]
    return None


def get_masked_hash_for_user(user_id: str) -> Optional[str]:
    raw = get_primary_hash_for_user(user_id)
    if not raw:
        return None
    return mask_hash(raw)
