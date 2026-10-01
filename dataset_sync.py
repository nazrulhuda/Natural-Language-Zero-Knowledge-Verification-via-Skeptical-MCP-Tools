"""
Sync dataset users from input-files zip API.
Downloads zip from INPUT_FILES_ZIP_URL, saves to INPUT_ZIPS_DIR, parses input_<raw_hash>.json
files, and upserts dataset_users (username User1, User2, ...; shared password).
"""
import json
import os
import re
import zipfile
from datetime import datetime
from io import BytesIO
from typing import Any, Dict, List
from urllib.request import urlopen, Request

import bcrypt
import psycopg2
import psycopg2.extras


def _get_conn():
    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        raise RuntimeError("DATABASE_URL is not set")
    return psycopg2.connect(database_url)


def _input_zip_url() -> str:
    url = os.getenv("INPUT_FILES_ZIP_URL", "").strip()
    if not url:
        raise ValueError("INPUT_FILES_ZIP_URL is not set")
    return url


def _input_zips_dir() -> str:
    d = os.path.abspath(os.getenv("INPUT_ZIPS_DIR", "/app/data/input_zips"))
    os.makedirs(d, exist_ok=True)
    return d


def _default_password() -> str:
    return os.getenv("DATASET_DEFAULT_PASSWORD", "password123")


def _fetch_zip_bytes(url: str, timeout: int = 120) -> bytes:
    req = Request(url, headers={"User-Agent": "MCP-DatasetSync/1.0"})
    with urlopen(req, timeout=timeout) as resp:
        return resp.read()


def _parse_input_filename(name: str) -> str | None:
    """Extract raw_hash from input_<raw_hash>.json. Returns None if not matching."""
    match = re.match(r"^input_(.+)\.json$", name, re.IGNORECASE)
    return match.group(1) if match else None


def run_dataset_sync() -> Dict[str, Any]:
    """
    Fetch zip from INPUT_FILES_ZIP_URL, save to INPUT_ZIPS_DIR, record in input_zip_archives,
    then parse each input_<raw_hash>.json and upsert dataset_users (User1, User2, ..., shared password).
    Returns dict with success, message, created, updated, zip_path, errors.
    """
    result = {"success": False, "message": "", "created": 0, "updated": 0, "zip_path": "", "errors": []}
    zip_url = _input_zip_url()
    zips_dir = _input_zips_dir()
    password_plain = _default_password()
    password_hash = bcrypt.hashpw(password_plain.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")

    try:
        zip_bytes = _fetch_zip_bytes(zip_url)
    except Exception as e:
        result["message"] = f"Failed to fetch zip: {e}"
        result["errors"].append(str(e))
        return result

    timestamp = datetime.utcnow().strftime("%Y-%m-%dT%H-%M-%S")
    zip_filename = f"input_files_{timestamp}.zip"
    zip_path = os.path.join(zips_dir, zip_filename)
    try:
        with open(zip_path, "wb") as f:
            f.write(zip_bytes)
    except Exception as e:
        result["message"] = f"Failed to save zip: {e}"
        result["errors"].append(str(e))
        return result

    file_size = len(zip_bytes)
    zip_archive_id = None
    with _get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                INSERT INTO input_zip_archives (source_url, stored_path, file_size_bytes, fetched_at)
                VALUES (%s, %s, %s, NOW())
                RETURNING id
                """,
                (zip_url, zip_path, file_size),
            )
            row = cur.fetchone()
            zip_archive_id = str(row["id"])
        conn.commit()

    result["zip_path"] = zip_path

    input_entries: List[tuple[str, str, Any]] = []
    try:
        with zipfile.ZipFile(BytesIO(zip_bytes), "r") as zf:
            for name in sorted(zf.namelist()):
                # Use only the basename so subdirectories like input_files/input_b1.json still match.
                raw_hash = _parse_input_filename(os.path.basename(name))
                if raw_hash is None:
                    continue
                try:
                    with zf.open(name) as f:
                        data = json.load(f)
                    input_data = data.get("input_data")
                except Exception as e:
                    result["errors"].append(f"{name}: {e}")
                    continue
                input_entries.append((name, raw_hash, input_data))
    except Exception as e:
        result["message"] = f"Failed to read zip: {e}"
        result["errors"].append(str(e))
        return result

    created = 0
    updated = 0
    with _get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            for source_file, raw_hash, input_data in input_entries:
                cur.execute(
                    "SELECT id, username FROM dataset_users WHERE raw_hash = %s",
                    (raw_hash,),
                )
                existing = cur.fetchone()
                if existing:
                    cur.execute(
                        """
                        UPDATE dataset_users
                        SET input_data = %s, source_file = %s, zip_archive_id = %s, updated_at = NOW()
                        WHERE id = %s
                        """,
                        (json.dumps(input_data) if input_data is not None else None, source_file, zip_archive_id, existing["id"]),
                    )
                    updated += 1
                else:
                    cur.execute(
                        """
                        SELECT COALESCE(MAX(CAST(SUBSTRING(username FROM 5) AS INTEGER)), 0) + 1 AS next_num
                        FROM dataset_users WHERE username ~ '^User[0-9]+$'
                        """
                    )
                    row = cur.fetchone()
                    next_num = row["next_num"] if row else 1
                    username = f"User{next_num}"
                    cur.execute(
                        """
                        INSERT INTO dataset_users (username, password_hash, raw_hash, input_data, source_file, zip_archive_id)
                        VALUES (%s, %s, %s, %s, %s, %s)
                        """,
                        (username, password_hash, raw_hash, json.dumps(input_data) if input_data is not None else None, source_file, zip_archive_id),
                    )
                    created += 1
        conn.commit()

    result["success"] = True
    result["message"] = f"Synced {created} created, {updated} updated. Zip: {zip_path}"
    result["created"] = created
    result["updated"] = updated
    return result
