-- Dataset-driven users and input zip archive storage.
-- Each row in dataset_users comes from an input file (input_<raw_hash>.json) and is tied to a zip archive.

CREATE TABLE IF NOT EXISTS input_zip_archives (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    source_url TEXT NOT NULL,
    stored_path TEXT NOT NULL,
    file_size_bytes BIGINT,
    fetched_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS dataset_users (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    username TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    raw_hash TEXT NOT NULL,
    input_data JSONB,
    source_file TEXT NOT NULL,
    zip_archive_id UUID REFERENCES input_zip_archives(id) ON DELETE SET NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_dataset_users_username ON dataset_users(username);
CREATE INDEX IF NOT EXISTS idx_dataset_users_raw_hash ON dataset_users(raw_hash);
CREATE INDEX IF NOT EXISTS idx_dataset_users_zip_archive_id ON dataset_users(zip_archive_id);
