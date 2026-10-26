#!/usr/bin/env python3
"""
agy-persist: Transactional, self-healing persistence and recovery engine for
Antigravity Spaces on Hugging Face Storage Buckets (hf-mount / FUSE) and volumes.

Guarantees & Protections:
1. Zero direct SQLite execution on FUSE mounts: all active SQLite databases
   (conversation_summaries.db and conversations/*.db) run exclusively on local
   POSIX container storage (~/.gemini/...) and are synced to persistent storage
   via SQLite's Online Backup API (sqlite3.Connection.backup), consolidating
   committed WAL frames into self-contained, checkpointed database files.
2. Atomic write-temp -> fsync -> close -> os.replace: forces hf-mount (FUSE) to
   flush dirty buffers to Xet object storage on close()/fsync() and prevents
   truncated or partially-written files from ever replacing valid bucket files.
3. Rolling known-good backups (${PERSIST_ROOT}/backups/prev/...): preserves the
   previous verified version of every database, credential, and config file.
4. Automatic SQLite corruption recovery & transcript reconciliation on startup:
   heals malformed SQLite headers (e.g. from prior ungraceful FUSE kills) via
   `sqlite3 .recover`, backfills any uncheckpointed steps from
   `brain/<cid>/.system_generated/logs/transcript_full.jsonl`, and reconciles
   `conversation_summaries.db`, `agyhub_summaries_proto.pb`,
   `jetbox_summaries_proto.pb`, `history.jsonl`, and `~/.gemini/config/projects/*.json`
   before `language_server` starts.
5. Anti-prune & non-empty overwrite guards: prevents startup reconciliation or
   empty initial states from silently dropping existing conversations or
   overwriting non-empty credentials, tokens, settings, or databases.
6. Object-storage empty-directory protection: places `.agy-keep` sentinels in
   empty `.git` subdirectories and tracked project directories so S3/Xet bucket
   remounts across Factory Rebuilds never drop empty directories.
"""

import argparse
import datetime
import fcntl
import hashlib
import json
import os
import re
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time
import urllib.parse
import uuid
from pathlib import Path


# =============================================================================
# Protobuf Wire-Format Helpers (Dependency-Free)
# =============================================================================

def encode_varint(val: int) -> bytes:
    if val < 0:
        val &= (1 << 64) - 1
    res = bytearray()
    while val >= 0x80:
        res.append((val & 0x7F) | 0x80)
        val >>= 7
    res.append(val & 0x7F)
    return bytes(res)


def decode_varint(data: bytes, idx: int) -> tuple[int, int]:
    val = 0
    shift = 0
    while True:
        if idx >= len(data):
            raise ValueError("Truncated varint")
        b = data[idx]
        idx += 1
        val |= (b & 0x7F) << shift
        shift += 7
        if not (b & 0x80):
            break
    return val, idx


def encode_bytes_field(field_num: int, val: bytes) -> bytes:
    tag = (field_num << 3) | 2
    return encode_varint(tag) + encode_varint(len(val)) + val


def encode_varint_field(field_num: int, val: int) -> bytes:
    tag = (field_num << 3) | 0
    return encode_varint(tag) + encode_varint(val)


def decode_pb_fields(data: bytes) -> list[tuple[int, str, int | bytes]]:
    idx = 0
    fields: list[tuple[int, str, int | bytes]] = []
    while idx < len(data):
        try:
            tag, idx = decode_varint(data, idx)
        except Exception:
            break
        field = tag >> 3
        wire = tag & 7
        if wire == 0:
            val, idx = decode_varint(data, idx)
            fields.append((field, "varint", val))
        elif wire == 2:
            length, idx = decode_varint(data, idx)
            if idx + length > len(data):
                break
            val = data[idx : idx + length]
            idx += length
            fields.append((field, "bytes", val))
        elif wire == 5:
            if idx + 4 > len(data):
                break
            val = data[idx : idx + 4]
            idx += 4
            fields.append((field, "32bit", val))
        elif wire == 1:
            if idx + 8 > len(data):
                break
            val = data[idx : idx + 8]
            idx += 8
            fields.append((field, "64bit", val))
        else:
            break
    return fields


def parse_iso_to_pb_timestamp(dt_str: str | None) -> tuple[bytes, datetime.datetime]:
    now = datetime.datetime.now(datetime.timezone.utc)
    dt = now
    if dt_str:
        try:
            cleaned = dt_str.strip().replace("Z", "+00:00")
            dt = datetime.datetime.fromisoformat(cleaned)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=datetime.timezone.utc)
        except Exception:
            dt = now
    sec = int(dt.timestamp())
    nano = dt.microsecond * 1000
    pb = encode_varint_field(1, sec)
    if nano > 0:
        pb += encode_varint_field(2, nano)
    return pb, dt


def pb_timestamp_to_datetime(pb_bytes: bytes) -> datetime.datetime:
    sec = 0
    nano = 0
    for f, t, v in decode_pb_fields(pb_bytes):
        if f == 1 and isinstance(v, int):
            sec = v
        elif f == 2 and isinstance(v, int):
            nano = v
    if sec <= 0:
        return datetime.datetime.now(datetime.timezone.utc)
    return datetime.datetime.fromtimestamp(sec + (nano / 1e9), tz=datetime.timezone.utc)


def format_sqlite_datetime(dt: datetime.datetime) -> str:
    dt_utc = dt.astimezone(datetime.timezone.utc)
    return dt_utc.strftime("%Y-%m-%d %H:%M:%S.%f000+00:00")


def extract_clean_user_preview(raw_content: str) -> str:
    if not raw_content:
        return "Conversation"
    m = re.search(r"<USER_REQUEST>\s*(.*?)\s*</USER_REQUEST>", raw_content, re.S)
    text = m.group(1).strip() if m else raw_content.strip()
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    first_line = lines[0] if lines else "Conversation"
    return first_line[:120]


def make_step_metadata(traj_id: str, casc_id: str, dt_str: str | None, step_idx: int = 0, model_enum: int = 1319) -> bytes:
    ts_pb, dt = parse_iso_to_pb_timestamp(dt_str)
    end_dt = dt + datetime.timedelta(milliseconds=250)
    end_pb = encode_varint_field(1, int(end_dt.timestamp())) + encode_varint_field(2, end_dt.microsecond * 1000)

    step_uuid = str(uuid.uuid4())
    f20 = encode_bytes_field(1, traj_id.encode("utf-8"))
    if step_idx > 0:
        f20 += encode_varint_field(2, step_idx)
    f20 += encode_bytes_field(4, casc_id.encode("utf-8"))

    res = bytearray()
    res += encode_bytes_field(1, ts_pb)
    res += encode_varint_field(3, 4 if step_idx == 0 else 2)
    if step_idx > 0:
        res += encode_bytes_field(6, ts_pb)
        res += encode_bytes_field(7, end_pb)
        res += encode_bytes_field(8, end_pb)
        res += encode_varint_field(11, model_enum)
    res += encode_bytes_field(12, step_uuid.encode("utf-8"))
    res += encode_bytes_field(20, f20)
    if step_idx > 0:
        res += encode_bytes_field(32, end_pb)
    return bytes(res)


def make_user_step_payload(meta: bytes, content: str) -> bytes:
    clean_text = extract_clean_user_preview(content)
    c_bytes = clean_text.encode("utf-8")
    p19 = bytearray()
    p19 += encode_bytes_field(2, c_bytes)
    p19 += encode_bytes_field(3, encode_bytes_field(1, c_bytes))

    payload = bytearray()
    payload += encode_varint_field(1, 14)
    payload += encode_varint_field(4, 3)
    payload += encode_bytes_field(5, meta)
    payload += encode_bytes_field(19, bytes(p19))
    return bytes(payload)


def make_planner_step_payload(meta: bytes, content: str, thinking: str = "", tool_calls: list | None = None) -> bytes:
    p20 = bytearray()
    if content:
        c_bytes = content.encode("utf-8")
        p20 += encode_bytes_field(1, c_bytes)
    if thinking:
        p20 += encode_bytes_field(3, thinking.encode("utf-8"))
    bot_id = f"bot-{uuid.uuid4()}".encode("utf-8")
    p20 += encode_bytes_field(6, bot_id)
    if tool_calls:
        for tc in tool_calls:
            call_id = str(tc.get("id") or f"call_{uuid.uuid4().hex[:8]}").encode("utf-8")
            name = str(tc.get("name") or "").encode("utf-8")
            args = json.dumps(tc.get("args") or {}).encode("utf-8")
            t_sub = encode_bytes_field(1, call_id) + encode_bytes_field(2, name) + encode_bytes_field(3, args)
            p20 += encode_bytes_field(7, t_sub)
    if content:
        p20 += encode_bytes_field(8, content.encode("utf-8"))
    p20 += encode_varint_field(12, 2)

    payload = bytearray()
    payload += encode_varint_field(1, 15)
    payload += encode_varint_field(4, 3)
    payload += encode_bytes_field(5, meta)
    payload += encode_bytes_field(20, bytes(p20))
    return bytes(payload)


def make_generic_step_payload(meta: bytes, step_type_num: int, content: str) -> bytes:
    payload = bytearray()
    payload += encode_varint_field(1, step_type_num)
    payload += encode_varint_field(4, 3)
    payload += encode_bytes_field(5, meta)
    if step_type_num == 101:  # SYSTEM_MESSAGE
        sub = encode_bytes_field(1, (content or "").encode("utf-8")) if content else b""
        payload += encode_bytes_field(114, sub)
    elif step_type_num == 23:  # CHECKPOINT
        sub = encode_bytes_field(1, (content or "").encode("utf-8")) if content else b""
        payload += encode_bytes_field(30, sub)
    elif step_type_num == 17:  # ERROR_MESSAGE
        sub = encode_bytes_field(1, (content or "").encode("utf-8")) if content else b""
        payload += encode_bytes_field(2, sub)
    else:  # 132 GENERIC
        sub = encode_bytes_field(2, (content or "").encode("utf-8")) if content else b""
        payload += encode_bytes_field(140, sub)
    return bytes(payload)


# =============================================================================
# Atomic File & SQLite Operations
# =============================================================================

def fsync_dir(dir_path: str) -> None:
    try:
        fd = os.open(dir_path, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError:
        pass


def atomic_write_bytes(dst_path: str, data: bytes, mode: int = 0o644, mtime_ns: int | None = None) -> bool:
    parent = os.path.dirname(dst_path)
    os.makedirs(parent, mode=0o700, exist_ok=True)
    tmp_path = f"{dst_path}.tmp.{os.getpid()}.{uuid.uuid4().hex[:8]}"
    try:
        fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
        try:
            view = memoryview(data)
            total = 0
            while total < len(data):
                written = os.write(fd, view[total:])
                if written <= 0:
                    raise OSError("short write")
                total += written
            os.fsync(fd)
        finally:
            os.close(fd)
        try:
            os.chmod(tmp_path, mode)
        except OSError:
            pass
        if mtime_ns is not None:
            try:
                os.utime(tmp_path, ns=(mtime_ns, mtime_ns))
            except OSError:
                pass
        os.replace(tmp_path, dst_path)
        fsync_dir(parent)
        return True
    except Exception as exc:
        print(f"[agy-persist] WARN: atomic_write_bytes failed for {dst_path}: {exc}", file=sys.stderr)
        try:
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)
        except OSError:
            pass
        return False


def atomic_copy_file(src_path: str, dst_path: str, mode: int | None = None) -> bool:
    try:
        st = os.stat(src_path)
        if stat.S_ISDIR(st.st_mode):
            return False
        with open(src_path, "rb") as f:
            data = f.read()
        target_mode = mode if mode is not None else (stat.S_IMODE(st.st_mode) or 0o644)
        return atomic_write_bytes(dst_path, data, mode=target_mode, mtime_ns=st.st_mtime_ns)
    except Exception as exc:
        print(f"[agy-persist] WARN: atomic_copy_file({src_path} -> {dst_path}) failed: {exc}", file=sys.stderr)
        return False


def is_valid_json_file(path: str) -> bool:
    try:
        if not os.path.isfile(path) or os.path.getsize(path) == 0:
            return False
        with open(path, "rb") as f:
            json.loads(f.read().decode("utf-8"))
        return True
    except Exception:
        return False


def check_sqlite_integrity(db_path: str) -> bool:
    if not os.path.isfile(db_path) or os.path.getsize(db_path) < 4096:
        return False
    try:
        uri = f"file:{urllib.parse.quote(os.path.abspath(db_path))}?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=5.0)
        try:
            res = conn.execute("PRAGMA integrity_check;").fetchall()
            if res != [("ok",)]:
                return False
            tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table';").fetchall()}
            return len(tables) > 0
        finally:
            conn.close()
    except Exception:
        return False


def snapshot_sqlite_db(src_db_path: str, dst_db_path: str, mode: int = 0o600) -> bool:
    """
    Create a self-contained, checkpointed, integrity-verified copy of `src_db_path`
    (including any live WAL frames in `src_db_path-wal`) using SQLite's Online
    Backup API into a local staging file, then atomically write + fsync + replace
    `dst_db_path`.
    """
    if not os.path.isfile(src_db_path) or os.path.getsize(src_db_path) == 0:
        return False

    os.makedirs("/tmp/agy-persist-stage", mode=0o700, exist_ok=True)
    stage_file = f"/tmp/agy-persist-stage/snap_{os.getpid()}_{uuid.uuid4().hex[:8]}.db"
    try:
        src_conn = sqlite3.connect(src_db_path, timeout=10.0)
        try:
            dst_conn = sqlite3.connect(stage_file, timeout=10.0)
            try:
                src_conn.backup(dst_conn)
                # Ensure the snapshot has no external WAL dependency
                dst_conn.execute("PRAGMA wal_checkpoint(TRUNCATE);")
                dst_conn.commit()
            finally:
                dst_conn.close()
        finally:
            src_conn.close()

        for wal_ext in ("-wal", "-shm"):
            if os.path.exists(stage_file + wal_ext):
                try:
                    os.unlink(stage_file + wal_ext)
                except OSError:
                    pass

        if not check_sqlite_integrity(stage_file):
            return False

        st = os.stat(src_db_path)
        wal_path = src_db_path + "-wal"
        mtime_ns = st.st_mtime_ns
        if os.path.exists(wal_path):
            try:
                mtime_ns = max(mtime_ns, os.stat(wal_path).st_mtime_ns)
            except OSError:
                pass

        with open(stage_file, "rb") as f:
            raw_bytes = f.read()
        return atomic_write_bytes(dst_db_path, raw_bytes, mode=mode, mtime_ns=mtime_ns)
    except Exception as exc:
        print(f"[agy-persist] WARN: snapshot_sqlite_db({src_db_path}) failed: {exc}", file=sys.stderr)
        return False
    finally:
        for suffix in ("", "-wal", "-shm"):
            try:
                if os.path.exists(stage_file + suffix):
                    os.unlink(stage_file + suffix)
            except OSError:
                pass


# =============================================================================
# Persistence Context & Inventory Configuration
# =============================================================================

AGY_SERVER_FILES = [
    "credentials.json",
    "sessions.json",
    "config.json",
    "csrf-token.txt",
]

GEMINI_ROOT_FILES = [
    "installation_id",
    "antigravity-oauth-token",
    "jetski-standalone-oauth-token",
    "settings.json",
    "jetski_state.pbtxt",
    "antigravity_state.pbtxt",
    "conversation_summaries.db",
    "history.jsonl",
    "GEMINI.md",
]

GEMINI_SUB_FILES = [
    "installation_id",
    "antigravity-oauth-token",
    "jetski-standalone-oauth-token",
    "settings.json",
    "jetski_state.pbtxt",
    "antigravity_state.pbtxt",
    "conversation_summaries.db",
    "history.jsonl",
    "agyhub_summaries_proto.pb",
    "jetbox_summaries_proto.pb",
    "webui_metadata.json",
]

GEMINI_SUB_DIRS = [
    "annotations",
    "implicit",
    "brain",
]

GEMINI_SUBS = ["antigravity-cli", "antigravity"]


class PersistEnv:
    def __init__(self) -> None:
        self.home = os.environ.get("HOME") or "/home/worker"
        self.agy_home = os.environ.get("AGY_HOME") or os.path.join(self.home, ".agy-remote")
        self.gemini_dir = os.path.join(self.home, ".gemini")
        self.config_agy_dir = os.path.join(self.home, ".config", "antigravity")

        worker_id = os.environ.get("WORKER_ID", "").strip()
        space_id = os.environ.get("SPACE_ID", "").strip()
        if not worker_id:
            if space_id:
                worker_id = space_id.split("/")[-1]
            else:
                worker_id = "agy-worker-local"
        self.worker_id = worker_id
        self.space_id = space_id or "local"

        bucket_mount = os.environ.get("AGY_BUCKET_MOUNT") or "/buckets/private"
        data_mount = os.environ.get("AGY_DATA_MOUNT") or "/data"

        self.require_persistent = (
            os.environ.get("AGY_REQUIRE_BUCKET", "") == "1"
            or (
                os.environ.get("AGY_ALLOW_EPHEMERAL", "") != "1"
                and (bool(space_id) or (worker_id.startswith("agy-worker-") and worker_id != "agy-worker-local"))
            )
        )

        if os.path.isdir(bucket_mount) and os.access(bucket_mount, os.W_OK | os.X_OK):
            self.persist_root = os.path.join(bucket_mount, self.worker_id)
            self.storage_mode = "hf-private-bucket"
        elif os.path.isdir(data_mount) and os.access(data_mount, os.W_OK | os.X_OK):
            self.persist_root = os.path.join(data_mount, self.worker_id)
            self.storage_mode = "persistent-volume"
        else:
            self.persist_root = os.path.join(self.home, ".worker-state", self.worker_id)
            self.storage_mode = "ephemeral-container"

        self.backup_prev_root = os.path.join(self.persist_root, "backups", "prev")
        self.workspace_dir = os.path.join(self.persist_root, "workspace")


def verify_mount_health(env: PersistEnv) -> bool:
    if env.require_persistent and env.storage_mode == "ephemeral-container":
        print(
            f"WORKER_INIT_FAIL: Persistent bucket/volume mount is required for worker_id={env.worker_id} "
            f"(space_id={env.space_id}), but neither {os.environ.get('AGY_BUCKET_MOUNT', '/buckets/private')} "
            f"nor /data is mounted and writable. Refusing to start with empty ephemeral state.",
            file=sys.stderr,
        )
        return False

    try:
        os.makedirs(env.persist_root, mode=0o700, exist_ok=True)
        probe = os.path.join(env.persist_root, f".mount_probe_{os.getpid()}")
        payload = f"probe:{env.worker_id}:{time.time_ns()}\n".encode("utf-8")
        if not atomic_write_bytes(probe, payload, mode=0o600):
            return False
        with open(probe, "rb") as f:
            read_back = f.read()
        try:
            os.unlink(probe)
        except OSError:
            pass
        return read_back == payload
    except Exception as exc:
        print(f"WORKER_INIT_FAIL: Storage mount health probe failed at {env.persist_root}: {exc}", file=sys.stderr)
        return False


def cleanup_stale_tmp_files(root_dir: str) -> None:
    if not os.path.isdir(root_dir):
        return
    now = time.time()
    for dirpath, _, filenames in os.walk(root_dir):
        for fn in filenames:
            if ".tmp." in fn or fn.endswith(".tmp") or fn.startswith(".mount_probe_"):
                full = os.path.join(dirpath, fn)
                try:
                    if now - os.path.getmtime(full) > 30:
                        os.unlink(full)
                except OSError:
                    pass


def save_rolling_backup(env: PersistEnv, rel_persist_path: str) -> None:
    src = os.path.join(env.persist_root, rel_persist_path)
    if not os.path.isfile(src) or os.path.getsize(src) == 0:
        return
    if src.endswith(".db") and not check_sqlite_integrity(src):
        return
    if src.endswith(".json") and not is_valid_json_file(src):
        return
    dst = os.path.join(env.backup_prev_root, rel_persist_path)
    atomic_copy_file(src, dst, mode=0o600)


# =============================================================================
# SQLite Session & Summary Recovery / Self-Healing Engine
# =============================================================================

CONVERSATION_DB_SCHEMA = """
PRAGMA page_size = 32768;
PRAGMA user_version = 1;
CREATE TABLE IF NOT EXISTS `trajectory_meta` (
    `trajectory_id` text,
    `cascade_id` text,
    `trajectory_type` integer,
    `source` integer,
    PRIMARY KEY (`trajectory_id`)
);
CREATE TABLE IF NOT EXISTS `steps` (
    `idx` integer,
    `step_type` integer NOT NULL DEFAULT 0,
    `status` integer NOT NULL DEFAULT 0,
    `has_subtrajectory` numeric NOT NULL DEFAULT false,
    `metadata` blob,
    `error_details` blob,
    `permissions` blob,
    `task_details` blob,
    `render_info` blob,
    `step_payload` blob,
    `step_format` integer NOT NULL DEFAULT 0,
    PRIMARY KEY (`idx`)
);
CREATE INDEX IF NOT EXISTS `idx_steps_status` ON `steps`(`status`);
CREATE INDEX IF NOT EXISTS `idx_steps_step_type` ON `steps`(`step_type`);
CREATE TABLE IF NOT EXISTS `gen_metadata` (
    `idx` integer,
    `data` blob,
    `size` integer NOT NULL DEFAULT 0,
    PRIMARY KEY (`idx`)
);
CREATE TABLE IF NOT EXISTS `executor_metadata` (
    `idx` integer,
    `data` blob,
    PRIMARY KEY (`idx`)
);
CREATE TABLE IF NOT EXISTS `parent_references` (
    `idx` integer,
    `data` blob,
    PRIMARY KEY (`idx`)
);
CREATE TABLE IF NOT EXISTS `trajectory_metadata_blob` (
    `id` text DEFAULT "main",
    `data` blob,
    PRIMARY KEY (`id`)
);
CREATE TABLE IF NOT EXISTS `battle_mode_infos` (
    `idx` integer,
    `data` blob,
    PRIMARY KEY (`idx`)
);
"""

SUMMARIES_DB_SCHEMA = """
CREATE TABLE IF NOT EXISTS `conversation_summaries` (
    `conversation_id` text,
    `title` text NOT NULL DEFAULT "",
    `preview` text NOT NULL DEFAULT "",
    `step_count` integer NOT NULL DEFAULT 0,
    `last_modified_time` datetime NOT NULL,
    `workspace_uris` text NOT NULL,
    `status` text NOT NULL DEFAULT "",
    `source` text NOT NULL DEFAULT "",
    `project_id` text NOT NULL DEFAULT "",
    `agent_name` text NOT NULL DEFAULT "",
    `parent_conversation_id` text NOT NULL DEFAULT "",
    `nesting_depth` integer NOT NULL DEFAULT 0,
    `battle_id` text NOT NULL DEFAULT "",
    `winning_conversation_id` text NOT NULL DEFAULT "",
    `not_fully_idle` numeric NOT NULL DEFAULT false,
    `killed` numeric NOT NULL DEFAULT false,
    `last_user_input_time` datetime NOT NULL,
    `last_user_input_step_index` integer NOT NULL DEFAULT -1,
    `app_data_dir` text NOT NULL DEFAULT "",
    `raw_summary` blob,
    `group_id` text NOT NULL DEFAULT "",
    PRIMARY KEY (`conversation_id`)
);
CREATE INDEX IF NOT EXISTS `idx_conversation_summaries_last_user_input_time` ON `conversation_summaries`(`last_user_input_time`);
CREATE INDEX IF NOT EXISTS `idx_conversation_summaries_last_modified_time` ON `conversation_summaries`(`last_modified_time`);
CREATE TABLE IF NOT EXISTS search_schema_version (
    version INTEGER PRIMARY KEY
);
INSERT OR IGNORE INTO search_schema_version VALUES (1);
CREATE VIRTUAL TABLE IF NOT EXISTS conversations_fts USING fts5(
    conversation_id UNINDEXED,
    chunk_index UNINDEXED,
    chunk_first_step UNINDEXED,
    chunk_last_step UNINDEXED,
    last_modified_time UNINDEXED,
    step_count UNINDEXED,
    last_step_created_at UNINDEXED,
    archived UNINDEXED,
    user_step_byte_offsets UNINDEXED,
    agent_step_byte_offsets UNINDEXED,
    title,
    workspace,
    project,
    user_body,
    agent_body,
    tokenize = 'unicode61 remove_diacritics 2',
    prefix = '1 2 3'
);
"""


def find_reference_artifacts(conv_dir: str, exclude_cid: str) -> tuple[bytes | None, bytes | None, bytes | None]:
    """Find reference (gen_metadata, executor_metadata, mendel_flags_f15) from any healthy sibling DB."""
    if not os.path.isdir(conv_dir):
        return None, None, None
    for fn in sorted(os.listdir(conv_dir)):
        if not fn.endswith(".db") or fn == f"{exclude_cid}.db":
            continue
        p = os.path.join(conv_dir, fn)
        if not check_sqlite_integrity(p):
            continue
        try:
            conn = sqlite3.connect(p)
            try:
                gen_row = conn.execute("SELECT data FROM gen_metadata WHERE idx=0 AND size > 0").fetchone()
                exec_row = conn.execute("SELECT data FROM executor_metadata WHERE idx=0").fetchone()
                blob_row = conn.execute("SELECT data FROM trajectory_metadata_blob WHERE id='main'").fetchone()
                f15 = None
                if blob_row and blob_row[0]:
                    for f, _, v in decode_pb_fields(blob_row[0]):
                        if f == 15 and isinstance(v, bytes):
                            f15 = v
                            break
                return (
                    gen_row[0] if gen_row else None,
                    exec_row[0] if exec_row else None,
                    f15,
                )
            finally:
                conn.close()
        except Exception:
            continue
    return None, None, None


def load_transcript_steps(brain_dir: str, cid: str) -> dict[int, dict]:
    steps: dict[int, dict] = {}
    for rel in (
        f"{cid}/.system_generated/logs/transcript_full.jsonl",
        f"{cid}/.system_generated/logs/transcript.jsonl",
    ):
        p = os.path.join(brain_dir, rel)
        if os.path.isfile(p) and os.path.getsize(p) > 0:
            try:
                with open(p, "r", encoding="utf-8", errors="replace") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        obj = json.loads(line)
                        idx = obj.get("step_index")
                        if isinstance(idx, int) and idx not in steps:
                            steps[idx] = obj
                if steps:
                    break
            except Exception:
                continue
    return steps


def recover_corrupt_sqlite_via_cli(corrupt_db_path: str, out_db_path: str) -> bool:
    """Use `sqlite3 <db> .recover` to salvage all intact B-Tree pages from a malformed SQLite file."""
    if shutil.which("sqlite3") is None:
        return False
    try:
        res = subprocess.run(
            ["sqlite3", corrupt_db_path, ".recover"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if res.returncode != 0 or not res.stdout.strip():
            return False
        if os.path.exists(out_db_path):
            os.unlink(out_db_path)
        apply_proc = subprocess.run(
            ["sqlite3", out_db_path],
            input=res.stdout,
            capture_output=True,
            text=True,
            timeout=30,
        )
        return apply_proc.returncode == 0 and check_sqlite_integrity(out_db_path)
    except Exception as exc:
        print(f"[agy-persist] WARN: sqlite3 .recover failed for {corrupt_db_path}: {exc}", file=sys.stderr)
        return False


def heal_and_reconcile_sub_dir(env: PersistEnv, sub: str) -> None:
    sub_local = os.path.join(env.gemini_dir, sub)
    conv_dir = os.path.join(sub_local, "conversations")
    brain_dir = os.path.join(sub_local, "brain")
    ann_dir = os.path.join(sub_local, "annotations")
    os.makedirs(conv_dir, mode=0o700, exist_ok=True)
    os.makedirs(brain_dir, mode=0o700, exist_ok=True)
    os.makedirs(ann_dir, mode=0o700, exist_ok=True)

    # Collect all known conversation IDs from both conversations/*.db and brain/<cid>
    cids: set[str] = set()
    for fn in os.listdir(conv_dir):
        if fn.endswith(".db") and not fn.startswith("."):
            cids.add(fn[:-3])
    for dn in os.listdir(brain_dir):
        if os.path.isdir(os.path.join(brain_dir, dn)) and not dn.startswith("."):
            if load_transcript_steps(brain_dir, dn):
                cids.add(dn)

    if not cids:
        return

    type_map = {
        "USER_INPUT": 14,
        "PLANNER_RESPONSE": 15,
        "ERROR_MESSAGE": 17,
        "CHECKPOINT": 23,
        "SYSTEM_MESSAGE": 101,
        "GENERIC": 132,
    }

    # Pass 1: Heal any corrupted or missing conversations/<cid>.db and backfill missing transcript steps
    for cid in sorted(cids):
        db_path = os.path.join(conv_dir, f"{cid}.db")
        t_steps = load_transcript_steps(brain_dir, cid)

        if not check_sqlite_integrity(db_path):
            print(f"[agy-persist] Healing damaged or missing conversation DB: {sub}/{cid}.db")
            prev_backup = os.path.join(env.backup_prev_root, "gemini", sub, "conversations", f"{cid}.db")
            healed = False
            if check_sqlite_integrity(prev_backup):
                shutil.copy2(prev_backup, db_path)
                healed = True
            elif os.path.isfile(db_path) and os.path.getsize(db_path) > 0:
                tmp_rec = f"{db_path}.recovered.{os.getpid()}"
                if recover_corrupt_sqlite_via_cli(db_path, tmp_rec):
                    os.replace(tmp_rec, db_path)
                    healed = True
                elif os.path.exists(tmp_rec):
                    os.unlink(tmp_rec)

            if not healed:
                # Reconstruct fresh DB from transcript
                if os.path.exists(db_path):
                    os.unlink(db_path)
                conn = sqlite3.connect(db_path)
                try:
                    conn.executescript(CONVERSATION_DB_SCHEMA)
                    conn.commit()
                finally:
                    conn.close()

        # Open healthy DB and ensure trajectory_meta, trajectory_metadata_blob, executor_metadata, gen_metadata, and steps are complete
        ref_gen, ref_exec, ref_f15 = find_reference_artifacts(conv_dir, cid)
        conn = sqlite3.connect(db_path)
        try:
            conn.executescript(CONVERSATION_DB_SCHEMA)
            meta_row = conn.execute("SELECT trajectory_id, cascade_id FROM trajectory_meta LIMIT 1").fetchone()
            if meta_row:
                traj_id, casc_id = meta_row[0], meta_row[1]
            else:
                traj_id, casc_id = str(uuid.uuid4()), cid
                conn.execute("INSERT OR REPLACE INTO trajectory_meta VALUES (?, ?, 4, 1)", (traj_id, casc_id))

            first_ts_str = t_steps.get(0, {}).get("created_at") if t_steps else None
            ts_pb, _ = parse_iso_to_pb_timestamp(first_ts_str)

            blob_row = conn.execute("SELECT data FROM trajectory_metadata_blob WHERE id='main'").fetchone()
            if not blob_row or not blob_row[0]:
                out_blob = bytearray()
                out_blob += encode_bytes_field(2, ts_pb)
                out_blob += encode_bytes_field(3, str(uuid.uuid4()).encode("utf-8"))
                out_blob += encode_bytes_field(6, cid.encode("utf-8"))
                if ref_f15:
                    out_blob += encode_bytes_field(15, ref_f15)
                out_blob += encode_bytes_field(18, b"outside-of-project")
                conn.execute("INSERT OR REPLACE INTO trajectory_metadata_blob VALUES ('main', ?)", (bytes(out_blob),))

            if ref_exec:
                exec_row = conn.execute("SELECT length(data) FROM executor_metadata WHERE idx=0").fetchone()
                if not exec_row or not exec_row[0]:
                    conn.execute("INSERT OR REPLACE INTO executor_metadata VALUES (0, ?)", (ref_exec,))

            if ref_gen:
                gen_row = conn.execute("SELECT size, length(data) FROM gen_metadata WHERE idx=0").fetchone()
                if not gen_row or not gen_row[0] or not gen_row[1]:
                    conn.execute("INSERT OR REPLACE INTO gen_metadata VALUES (0, ?, ?)", (ref_gen, len(ref_gen)))

            existing_indices = {r[0] for r in conn.execute("SELECT idx FROM steps").fetchall()}
            for s_idx, st in sorted(t_steps.items()):
                if s_idx in existing_indices:
                    continue
                s_type_str = st.get("type", "GENERIC")
                s_type_num = type_map.get(s_type_str, 132)
                content = st.get("content", "")
                created_at = st.get("created_at")
                s_meta = make_step_metadata(traj_id, cid, created_at, step_idx=s_idx)
                if s_type_num == 14:
                    s_payload = make_user_step_payload(s_meta, content)
                elif s_type_num == 15:
                    s_payload = make_planner_step_payload(
                        s_meta,
                        content,
                        st.get("thinking", ""),
                        st.get("tool_calls", []),
                    )
                else:
                    s_payload = make_generic_step_payload(s_meta, s_type_num, content)
                conn.execute(
                    "INSERT OR REPLACE INTO steps VALUES (?, ?, 3, 0, ?, NULL, NULL, NULL, NULL, ?, 0)",
                    (s_idx, s_type_num, s_meta, s_payload),
                )

            conn.commit()
        finally:
            conn.close()
        os.chmod(db_path, 0o600)

    # Pass 2: Reconcile conversation_summaries.db, projects/*.json, annotations, proto stores, and history.jsonl
    sum_db_path = os.path.join(sub_local, "conversation_summaries.db")
    if os.path.exists(sum_db_path) and not check_sqlite_integrity(sum_db_path):
        prev_sum = os.path.join(env.backup_prev_root, "gemini", sub, "conversation_summaries.db")
        if check_sqlite_integrity(prev_sum):
            shutil.copy2(prev_sum, sum_db_path)
        else:
            tmp_rec = f"{sum_db_path}.recovered.{os.getpid()}"
            if recover_corrupt_sqlite_via_cli(sum_db_path, tmp_rec):
                os.replace(tmp_rec, sum_db_path)
            else:
                os.unlink(sum_db_path)

    sum_conn = sqlite3.connect(sum_db_path)
    try:
        sum_conn.executescript(SUMMARIES_DB_SCHEMA)
        existing_summaries = {
            r[0]: r[1]
            for r in sum_conn.execute("SELECT conversation_id, step_count FROM conversation_summaries").fetchall()
        }

        for cid in sorted(cids):
            db_path = os.path.join(conv_dir, f"{cid}.db")
            if not check_sqlite_integrity(db_path):
                continue

            t_steps = load_transcript_steps(brain_dir, cid)
            c_conn = sqlite3.connect(db_path)
            try:
                m_row = c_conn.execute("SELECT trajectory_id FROM trajectory_meta LIMIT 1").fetchone()
                traj_id = m_row[0] if m_row else str(uuid.uuid4())
                step_count = c_conn.execute("SELECT count(*) FROM steps").fetchone()[0]
                blob_row = c_conn.execute("SELECT data FROM trajectory_metadata_blob WHERE id='main'").fetchone()
                blob_bytes = blob_row[0] if (blob_row and blob_row[0]) else b""
            finally:
                c_conn.close()

            # Decode trajectory_metadata_blob fields
            f1_ws_entry = None
            f2_created_pb = None
            f3_session_id = None
            f7_ws_uri = ""
            f18_project_id = ""
            for f, _, v in decode_pb_fields(blob_bytes):
                if f == 1 and isinstance(v, bytes):
                    f1_ws_entry = v
                elif f == 2 and isinstance(v, bytes):
                    f2_created_pb = v
                elif f == 3 and isinstance(v, bytes):
                    f3_session_id = v.decode("utf-8", errors="ignore")
                elif f == 7 and isinstance(v, bytes):
                    f7_ws_uri = v.decode("utf-8", errors="ignore")
                elif f in (18, 146) and isinstance(v, bytes):
                    f18_project_id = v.decode("utf-8", errors="ignore")

            # Reconstruct missing project JSON & workspace folder if conversation belongs to a custom project
            if f18_project_id and f18_project_id not in ("outside-of-project", "default-cli-project"):
                proj_dir = os.path.join(env.gemini_dir, "config", "projects")
                os.makedirs(proj_dir, mode=0o700, exist_ok=True)
                proj_file = os.path.join(proj_dir, f"{f18_project_id}.json")
                if not is_valid_json_file(proj_file):
                    ws_uri = f7_ws_uri or f"file://{env.workspace_dir}"
                    proj_name = ws_uri.rstrip("/").split("/")[-1] or "workspace"
                    proj_obj = {
                        "id": f18_project_id,
                        "name": proj_name,
                        "projectResources": {
                            "resources": [{"folderUri": ws_uri}]
                        },
                        "settings": {},
                        "isWorkspaceOnly": False,
                    }
                    atomic_write_bytes(
                        proj_file,
                        (json.dumps(proj_obj, indent=2) + "\n").encode("utf-8"),
                        mode=0o600,
                    )
                if f7_ws_uri.startswith("file:///"):
                    local_ws_folder = urllib.parse.unquote(f7_ws_uri[len("file://") :])
                    if local_ws_folder.startswith(env.persist_root) or local_ws_folder.startswith(env.home):
                        os.makedirs(local_ws_folder, mode=0o755, exist_ok=True)
                        keep_file = os.path.join(local_ws_folder, ".agy-keep")
                        if not os.listdir(local_ws_folder):
                            atomic_write_bytes(keep_file, b"", mode=0o644)

            # Determine title & preview
            ann_file = os.path.join(ann_dir, f"{cid}.pbtxt")
            ann_title = ""
            if os.path.isfile(ann_file):
                try:
                    with open(ann_file, "r", encoding="utf-8", errors="ignore") as af:
                        ann_txt = af.read()
                    m_title = re.search(r'title:\s*"([^"]+)"', ann_txt)
                    if m_title:
                        ann_title = m_title.group(1)
                except Exception:
                    pass

            user_text = "Hello"
            agent_text = ""
            first_dt_str = None
            last_dt_str = None
            last_user_idx = -1
            if t_steps:
                for idx_k, st_v in sorted(t_steps.items()):
                    if first_dt_str is None:
                        first_dt_str = st_v.get("created_at")
                    last_dt_str = st_v.get("created_at") or last_dt_str
                    if st_v.get("type") == "USER_INPUT":
                        user_text = extract_clean_user_preview(st_v.get("content", ""))
                        last_user_idx = idx_k
                    elif st_v.get("type") == "PLANNER_RESPONSE" and not agent_text:
                        agent_text = st_v.get("content", "")

            preview = ann_title or user_text or "Conversation"
            title = ann_title

            # Only insert/update summary if missing or step_count grew
            if cid not in existing_summaries or existing_summaries[cid] < step_count:
                created_pb = f2_created_pb or parse_iso_to_pb_timestamp(first_dt_str)[0]
                created_dt = pb_timestamp_to_datetime(created_pb)
                mod_pb, mod_dt = parse_iso_to_pb_timestamp(last_dt_str)
                if mod_dt < created_dt:
                    mod_dt = created_dt
                    mod_pb = created_pb

                # Build sub-message 17 (trajectory metadata summary)
                sub17 = bytearray()
                if f1_ws_entry:
                    sub17 += encode_bytes_field(1, f1_ws_entry)
                sub17 += encode_bytes_field(2, created_pb)
                sub17 += encode_bytes_field(3, (f3_session_id or str(uuid.uuid4())).encode("utf-8"))
                sub17 += encode_bytes_field(6, cid.encode("utf-8"))
                if f7_ws_uri:
                    sub17 += encode_bytes_field(7, f7_ws_uri.encode("utf-8"))
                if f18_project_id:
                    sub17 += encode_bytes_field(18, f18_project_id.encode("utf-8"))

                raw_sum = bytearray()
                raw_sum += encode_bytes_field(1, preview.encode("utf-8"))
                raw_sum += encode_varint_field(2, step_count)
                raw_sum += encode_bytes_field(3, mod_pb)
                raw_sum += encode_bytes_field(4, traj_id.encode("utf-8"))
                raw_sum += encode_varint_field(5, 1)  # CASCADE_RUN_STATUS_IDLE
                raw_sum += encode_bytes_field(7, created_pb)
                if f1_ws_entry:
                    raw_sum += encode_bytes_field(9, f1_ws_entry)
                raw_sum += encode_bytes_field(10, created_pb)
                f15_ann = bytearray()
                if title:
                    f15_ann += encode_bytes_field(1, title.encode("utf-8"))
                f15_ann += encode_bytes_field(7, mod_pb)
                raw_sum += encode_bytes_field(15, bytes(f15_ann))
                raw_sum += encode_varint_field(16, max(0, last_user_idx))
                raw_sum += encode_bytes_field(17, bytes(sub17))
                raw_sum += encode_varint_field(22, 4)
                raw_sum += encode_varint_field(28, 4)

                ws_uris_json = json.dumps([f7_ws_uri]) if f7_ws_uri else ""
                mod_sql = format_sqlite_datetime(mod_dt)
                inp_sql = format_sqlite_datetime(created_dt)

                sum_conn.execute(
                    """
                    INSERT OR REPLACE INTO conversation_summaries (
                        conversation_id, title, preview, step_count, last_modified_time,
                        workspace_uris, status, source, project_id, agent_name,
                        parent_conversation_id, nesting_depth, battle_id, winning_conversation_id,
                        not_fully_idle, killed, last_user_input_time, last_user_input_step_index,
                        app_data_dir, raw_summary, group_id
                    ) VALUES (?, ?, ?, ?, ?, ?, 'CASCADE_RUN_STATUS_IDLE', '', ?, '', '', 0, '', '', 0, 0, ?, ?, ?, ?, '')
                    """,
                    (
                        cid,
                        title,
                        preview,
                        step_count,
                        mod_sql,
                        ws_uris_json,
                        f18_project_id or "outside-of-project",
                        inp_sql,
                        last_user_idx,
                        sub,
                        bytes(raw_sum),
                    ),
                )

                # Ensure FTS index entry exists
                fts_exists = sum_conn.execute(
                    "SELECT count(*) FROM conversations_fts WHERE conversation_id = ?", (cid,)
                ).fetchone()[0]
                if not fts_exists:
                    mod_ns = int(mod_dt.timestamp() * 1e9)
                    cre_ns = int(created_dt.timestamp() * 1e9)
                    sum_conn.execute(
                        """
                        INSERT INTO conversations_fts (
                            conversation_id, chunk_index, chunk_first_step, chunk_last_step,
                            last_modified_time, step_count, last_step_created_at, archived,
                            user_step_byte_offsets, agent_step_byte_offsets,
                            title, workspace, project, user_body, agent_body
                        ) VALUES (?, -1, 0, 0, ?, ?, ?, 0, '', '', ?, ?, ?, '', '')
                        """,
                        (cid, mod_ns, step_count, cre_ns, title or preview, f7_ws_uri, f18_project_id),
                    )
                    sum_conn.execute(
                        """
                        INSERT INTO conversations_fts (
                            conversation_id, chunk_index, chunk_first_step, chunk_last_step,
                            last_modified_time, step_count, last_step_created_at, archived,
                            user_step_byte_offsets, agent_step_byte_offsets,
                            title, workspace, project, user_body, agent_body
                        ) VALUES (?, 0, 0, ?, 0, 0, 0, 0, X'5B5B302C305D5D', X'5B5B312C305D5D', '', '', '', ?, ?)
                        """,
                        (cid, max(0, step_count - 1), user_text + "\n", (agent_text or "") + "\n"),
                    )

        sum_conn.commit()

        # Reconcile agyhub_summaries_proto.pb and jetbox_summaries_proto.pb with conversation_summaries.db
        all_raw_rows = sum_conn.execute(
            "SELECT conversation_id, raw_summary FROM conversation_summaries WHERE raw_summary IS NOT NULL"
        ).fetchall()
    finally:
        sum_conn.close()

    if all_raw_rows:
        for proto_fn in ("agyhub_summaries_proto.pb", "jetbox_summaries_proto.pb"):
            proto_path = os.path.join(sub_local, proto_fn)
            existing_map: dict[str, bytes] = {}
            if os.path.isfile(proto_path) and os.path.getsize(proto_path) > 0:
                try:
                    with open(proto_path, "rb") as pf:
                        raw_pb = pf.read()
                    for f, _, v in decode_pb_fields(raw_pb):
                        if f == 1 and isinstance(v, bytes):
                            k_str = ""
                            val_b = b""
                            for sf, _, sv in decode_pb_fields(v):
                                if sf == 1 and isinstance(sv, bytes):
                                    k_str = sv.decode("utf-8", errors="ignore")
                                elif sf == 2 and isinstance(sv, bytes):
                                    val_b = sv
                            if k_str and val_b:
                                existing_map[k_str] = val_b
                except Exception:
                    pass

            changed_proto = False
            for r_cid, r_raw in all_raw_rows:
                if r_cid not in existing_map:
                    existing_map[r_cid] = r_raw
                    changed_proto = True

            if changed_proto or not os.path.isfile(proto_path):
                out_pb = bytearray()
                for k_cid, v_raw in existing_map.items():
                    entry = encode_bytes_field(1, k_cid.encode("utf-8")) + encode_bytes_field(2, v_raw)
                    out_pb += encode_bytes_field(1, entry)
                atomic_write_bytes(proto_path, bytes(out_pb), mode=0o600)

    # Ensure history.jsonl has /resume entries for all conversations
    hist_path = os.path.join(sub_local, "history.jsonl")
    existing_hist_cids: set[str] = set()
    if os.path.isfile(hist_path):
        try:
            with open(hist_path, "r", encoding="utf-8", errors="ignore") as hf:
                for line in hf:
                    for cid in cids:
                        if cid in line:
                            existing_hist_cids.add(cid)
        except Exception:
            pass

    missing_hist = sorted(cids - existing_hist_cids)
    if missing_hist:
        try:
            with open(hist_path, "a", encoding="utf-8") as hf:
                for cid in missing_hist:
                    rec = {
                        "display": "/resume",
                        "timestamp": int(time.time() * 1000),
                        "workspace": env.workspace_dir,
                        "conversationId": cid,
                        "type": "slash_command",
                    }
                    hf.write(json.dumps(rec) + "\n")
                hf.flush()
                os.fsync(hf.fileno())
            os.chmod(hist_path, 0o600)
        except Exception:
            pass


def repair_workspace_and_git(env: PersistEnv) -> None:
    os.makedirs(env.workspace_dir, mode=0o755, exist_ok=True)
    git_dir = os.path.join(env.workspace_dir, ".git")
    git_subdirs = [
        os.path.join(git_dir, "objects", "info"),
        os.path.join(git_dir, "objects", "pack"),
        os.path.join(git_dir, "refs", "heads"),
        os.path.join(git_dir, "refs", "tags"),
    ]
    if not os.path.isdir(git_dir) or not os.path.isfile(os.path.join(git_dir, "HEAD")):
        subprocess.run(["git", "-C", env.workspace_dir, "init", "-b", "main"], capture_output=True)

    for sd in git_subdirs:
        os.makedirs(sd, mode=0o755, exist_ok=True)
        keep = os.path.join(sd, ".agy-keep")
        if not os.path.exists(keep):
            atomic_write_bytes(keep, b"", mode=0o644)

    # Also ensure any project directory registered in ~/.gemini/config/projects/*.json has .agy-keep if empty
    proj_dir = os.path.join(env.gemini_dir, "config", "projects")
    if os.path.isdir(proj_dir):
        for fn in os.listdir(proj_dir):
            if not fn.endswith(".json"):
                continue
            pf = os.path.join(proj_dir, fn)
            try:
                with open(pf, "r", encoding="utf-8") as f:
                    pdata = json.load(f)
                for res in pdata.get("projectResources", {}).get("resources", []):
                    uri = res.get("folderUri", "")
                    if uri.startswith("file:///"):
                        folder = urllib.parse.unquote(uri[len("file://") :])
                        canonical_prefix = f"/buckets/private/{env.worker_id}/workspace/"
                        target_folders = []
                        if folder.startswith(env.persist_root) or folder.startswith(env.home):
                            target_folders.append(folder)
                        if folder.startswith(canonical_prefix):
                            rel_ws = folder[len(canonical_prefix) :]
                            mapped = os.path.join(env.workspace_dir, rel_ws)
                            if mapped not in target_folders:
                                target_folders.append(mapped)
                        for tf in target_folders:
                            os.makedirs(tf, mode=0o755, exist_ok=True)
                            if not os.listdir(tf):
                                atomic_write_bytes(os.path.join(tf, ".agy-keep"), b"", mode=0o644)
            except Exception:
                continue

    # Protect any empty user directories inside workspace from object-storage pruning
    for dirpath, dirnames, filenames in os.walk(env.workspace_dir):
        if ".git" in dirpath.split(os.sep):
            continue
        if dirpath != env.workspace_dir and not dirnames and not filenames:
            atomic_write_bytes(os.path.join(dirpath, ".agy-keep"), b"", mode=0o644)


def enforce_remote_control_disabled(env: PersistEnv) -> None:
    # 1. ~/.gemini/settings.json
    root_settings = os.path.join(env.gemini_dir, "settings.json")
    s_obj = {}
    if is_valid_json_file(root_settings):
        try:
            with open(root_settings, "r", encoding="utf-8") as f:
                s_obj = json.load(f)
        except Exception:
            s_obj = {}
    if not isinstance(s_obj, dict):
        s_obj = {}
    if s_obj.get("remoteControlEnabled") is not False:
        s_obj["remoteControlEnabled"] = False
        atomic_write_bytes(root_settings, (json.dumps(s_obj, indent=2) + "\n").encode("utf-8"), mode=0o600)

    # 2. ~/.gemini/config/config.json
    cfg_dir = os.path.join(env.gemini_dir, "config")
    os.makedirs(cfg_dir, mode=0o700, exist_ok=True)
    cfg_file = os.path.join(cfg_dir, "config.json")
    c_obj = {}
    if is_valid_json_file(cfg_file):
        try:
            with open(cfg_file, "r", encoding="utf-8") as f:
                c_obj = json.load(f)
        except Exception:
            c_obj = {}
    if not isinstance(c_obj, dict):
        c_obj = {}
    user_settings = c_obj.get("userSettings")
    if not isinstance(user_settings, dict):
        user_settings = {}
        c_obj["userSettings"] = user_settings
    if user_settings.get("remoteControlEnabled") is not False:
        user_settings["remoteControlEnabled"] = False
        atomic_write_bytes(cfg_file, (json.dumps(c_obj, indent=2) + "\n").encode("utf-8"), mode=0o600)


# =============================================================================
# Restore Command (Startup)
# =============================================================================

def restore_tree(src_root: str, dst_root: str, default_mode: int = 0o600) -> None:
    if not os.path.isdir(src_root):
        return
    for dirpath, _, filenames in os.walk(src_root):
        rel_dir = os.path.relpath(dirpath, src_root)
        target_dir = dst_root if rel_dir == "." else os.path.join(dst_root, rel_dir)
        os.makedirs(target_dir, mode=0o700, exist_ok=True)
        for fn in sorted(filenames):
            if ".tmp." in fn or fn.endswith(".tmp") or fn.startswith(".mount_probe_"):
                continue
            s_file = os.path.join(dirpath, fn)
            d_file = os.path.join(target_dir, fn)
            if os.path.getsize(s_file) > 0:
                atomic_copy_file(s_file, d_file, mode=default_mode)


def restore_single_file_with_fallback(env: PersistEnv, rel_persist_path: str, dst_path: str, mode: int = 0o600) -> None:
    src = os.path.join(env.persist_root, rel_persist_path)
    prev = os.path.join(env.backup_prev_root, rel_persist_path)

    chosen = None
    if os.path.isfile(src) and os.path.getsize(src) > 0:
        if src.endswith(".json") and not is_valid_json_file(src) and is_valid_json_file(prev):
            chosen = prev
        elif src.endswith(".db") and not check_sqlite_integrity(src) and check_sqlite_integrity(prev):
            chosen = prev
        else:
            chosen = src
    elif os.path.isfile(prev) and os.path.getsize(prev) > 0:
        chosen = prev

    if chosen:
        atomic_copy_file(chosen, dst_path, mode=mode)


def cmd_restore(env: PersistEnv) -> int:
    if not verify_mount_health(env):
        return 1

    cleanup_stale_tmp_files(env.persist_root)

    # 1. Create required persistent & local directory trees
    for d in (
        env.workspace_dir,
        os.path.join(env.persist_root, "agy-server"),
        os.path.join(env.persist_root, "config-antigravity"),
        os.path.join(env.persist_root, "gemini", "root"),
        os.path.join(env.persist_root, "gemini", "config"),
        env.backup_prev_root,
        env.agy_home,
        os.path.join(env.home, ".local", "share", "agy-server"),
        env.gemini_dir,
        os.path.join(env.gemini_dir, "config"),
        env.config_agy_dir,
    ):
        os.makedirs(d, mode=0o700, exist_ok=True)

    for sub in GEMINI_SUBS:
        os.makedirs(os.path.join(env.persist_root, "gemini", sub), mode=0o700, exist_ok=True)
        sub_local = os.path.join(env.gemini_dir, sub)
        os.makedirs(sub_local, mode=0o700, exist_ok=True)
        # Ensure conversations, brain, annotations, implicit are real local POSIX directories (NEVER FUSE symlinks!)
        for dname in ("conversations", "brain", "annotations", "implicit", "config"):
            os.makedirs(os.path.join(env.persist_root, "gemini", sub, dname), mode=0o700, exist_ok=True)
            local_d = os.path.join(sub_local, dname)
            if os.path.islink(local_d):
                os.unlink(local_d)
            os.makedirs(local_d, mode=0o700, exist_ok=True)

    # 2. Link ~/workspace -> ${PERSIST_ROOT}/workspace
    home_ws = os.path.join(env.home, "workspace")
    if not os.path.islink(home_ws):
        if os.path.isdir(home_ws):
            for item in os.listdir(home_ws):
                s_item = os.path.join(home_ws, item)
                d_item = os.path.join(env.workspace_dir, item)
                if not os.path.exists(d_item):
                    if os.path.isdir(s_item):
                        shutil.copytree(s_item, d_item, symlinks=True)
                    else:
                        shutil.copy2(s_item, d_item)
            shutil.rmtree(home_ws, ignore_errors=True)
        os.symlink(env.workspace_dir, home_ws)

    # 3. Restore agy-server files
    for fname in AGY_SERVER_FILES:
        restore_single_file_with_fallback(
            env,
            os.path.join("agy-server", fname),
            os.path.join(env.agy_home, fname),
            mode=0o600,
        )

    # 4. Restore ~/.config/antigravity and ~/.gemini/config trees
    restore_tree(os.path.join(env.persist_root, "config-antigravity"), env.config_agy_dir, default_mode=0o600)
    restore_tree(os.path.join(env.persist_root, "gemini", "config"), os.path.join(env.gemini_dir, "config"), default_mode=0o600)

    # 5. Restore ~/.gemini root files
    for fname in GEMINI_ROOT_FILES:
        mode = 0o600 if ("oauth-token" in fname or fname.endswith(".db")) else 0o644
        restore_single_file_with_fallback(
            env,
            os.path.join("gemini", "root", fname),
            os.path.join(env.gemini_dir, fname),
            mode=mode,
        )

    # 6. Restore ~/.gemini/{antigravity-cli,antigravity} files and subdirectories
    for sub in GEMINI_SUBS:
        sub_local = os.path.join(env.gemini_dir, sub)
        for fname in GEMINI_SUB_FILES:
            mode = 0o600 if ("oauth-token" in fname or fname.endswith(".db") or fname.endswith(".pb")) else 0o644
            restore_single_file_with_fallback(
                env,
                os.path.join("gemini", sub, fname),
                os.path.join(sub_local, fname),
                mode=mode,
            )
        for dname in GEMINI_SUB_DIRS:
            restore_tree(
                os.path.join(env.persist_root, "gemini", sub, dname),
                os.path.join(sub_local, dname),
                default_mode=0o644,
            )
        # Restore conversations/*.db to local POSIX directory
        p_conv = os.path.join(env.persist_root, "gemini", sub, "conversations")
        l_conv = os.path.join(sub_local, "conversations")
        if os.path.isdir(p_conv):
            for fn in sorted(os.listdir(p_conv)):
                if fn.endswith(".db") and not fn.startswith("."):
                    restore_single_file_with_fallback(
                        env,
                        os.path.join("gemini", sub, "conversations", fn),
                        os.path.join(l_conv, fn),
                        mode=0o600,
                    )

    # 7. Ensure unique per-Space installation_id exists (never overwrite an existing one)
    cli_inst = os.path.join(env.gemini_dir, "antigravity-cli", "installation_id")
    agy_inst = os.path.join(env.gemini_dir, "antigravity", "installation_id")
    root_inst = os.path.join(env.gemini_dir, "installation_id")
    inst_val = ""
    for candidate in (cli_inst, agy_inst, root_inst):
        if os.path.isfile(candidate) and os.path.getsize(candidate) > 0:
            with open(candidate, "r", encoding="utf-8", errors="ignore") as f:
                inst_val = f.read().strip()
            if inst_val:
                break
    if not inst_val:
        inst_val = str(uuid.uuid4())
    for dst_inst in (cli_inst, agy_inst, root_inst):
        atomic_write_bytes(dst_inst, inst_val.encode("utf-8"), mode=0o600)

    # 8. Heal any damaged conversation DBs, backfill uncheckpointed transcript steps, and reconcile summaries/projects
    for sub in GEMINI_SUBS:
        heal_and_reconcile_sub_dir(env, sub)

    # 9. Enforce remoteControlEnabled=false and repair workspace/.git
    enforce_remote_control_disabled(env)
    repair_workspace_and_git(env)

    return 0


# =============================================================================
# Sync Command (Runtime Periodic & Final Shutdown Flush)
# =============================================================================

def files_differ(src_path: str, dst_path: str) -> bool:
    try:
        st_s = os.stat(src_path)
    except OSError:
        return False
    if st_s.st_size == 0:
        # Never overwrite a non-empty file in persistent storage with an empty file
        if os.path.exists(dst_path) and os.path.getsize(dst_path) > 0:
            return False
        return not os.path.exists(dst_path)

    try:
        st_d = os.stat(dst_path)
    except OSError:
        return True

    if st_s.st_size != st_d.st_size:
        return True
    # Compare mtime with 1ms tolerance or SHA-256 if newer
    if st_s.st_mtime_ns > st_d.st_mtime_ns + 1_000_000:
        h_s = hashlib.sha256()
        h_d = hashlib.sha256()
        with open(src_path, "rb") as fs, open(dst_path, "rb") as fd:
            while chunk := fs.read(65536):
                h_s.update(chunk)
            while chunk := fd.read(65536):
                h_d.update(chunk)
        return h_s.digest() != h_d.digest()
    return False


def sync_regular_file(env: PersistEnv, src_path: str, rel_persist_path: str, mode: int = 0o600) -> bool:
    if not os.path.isfile(src_path):
        return False
    dst_path = os.path.join(env.persist_root, rel_persist_path)
    if os.path.getsize(src_path) == 0 and os.path.isfile(dst_path) and os.path.getsize(dst_path) > 0:
        return False
    if src_path.endswith(".json") and os.path.getsize(src_path) > 0 and not is_valid_json_file(src_path):
        print(f"[agy-persist] WARN: Skipping sync of invalid JSON file {src_path}", file=sys.stderr)
        return False
    if not files_differ(src_path, dst_path):
        return False
    save_rolling_backup(env, rel_persist_path)
    return atomic_copy_file(src_path, dst_path, mode=mode)


def sync_directory_tree(env: PersistEnv, src_root: str, rel_persist_root: str, default_mode: int = 0o600) -> None:
    if not os.path.isdir(src_root):
        return
    for dirpath, _, filenames in os.walk(src_root):
        rel_dir = os.path.relpath(dirpath, src_root)
        for fn in sorted(filenames):
            if ".tmp." in fn or fn.endswith(".tmp") or fn.endswith("-wal") or fn.endswith("-shm") or fn.endswith(".lock"):
                continue
            s_file = os.path.join(dirpath, fn)
            rel_p = os.path.join(rel_persist_root, fn) if rel_dir == "." else os.path.join(rel_persist_root, rel_dir, fn)
            sync_regular_file(env, s_file, rel_p, mode=default_mode)


def preserve_unpruned_summaries(local_sum_db: str, bucket_sum_db: str, local_conv_dir: str, local_brain_dir: str) -> None:
    """
    Anti-prune guard: if `bucket_sum_db` has conversation_summaries rows whose
    conversation still exists in `local_conv_dir` or `local_brain_dir`, ensure
    those rows are not dropped from `local_sum_db`.
    """
    if not check_sqlite_integrity(bucket_sum_db) or not check_sqlite_integrity(local_sum_db):
        return
    try:
        b_conn = sqlite3.connect(f"file:{urllib.parse.quote(os.path.abspath(bucket_sum_db))}?mode=ro", uri=True, timeout=5.0)
        try:
            cols = [c[1] for c in b_conn.execute("PRAGMA table_info(conversation_summaries)").fetchall()]
            b_rows = b_conn.execute("SELECT * FROM conversation_summaries").fetchall()
        finally:
            b_conn.close()

        if not b_rows or "conversation_id" not in cols:
            return
        cid_idx = cols.index("conversation_id")

        l_conn = sqlite3.connect(local_sum_db, timeout=10.0)
        try:
            l_cids = {r[0] for r in l_conn.execute("SELECT conversation_id FROM conversation_summaries").fetchall()}
            restored = 0
            placeholders = ",".join(["?"] * len(cols))
            col_names = ",".join([f"`{c}`" for c in cols])
            for row in b_rows:
                cid = row[cid_idx]
                if cid in l_cids:
                    continue
                # Check if conversation still exists on disk
                has_db = os.path.isfile(os.path.join(local_conv_dir, f"{cid}.db"))
                has_brain = os.path.isdir(os.path.join(local_brain_dir, cid))
                if has_db or has_brain:
                    l_conn.execute(f"INSERT OR IGNORE INTO conversation_summaries ({col_names}) VALUES ({placeholders})", row)
                    restored += 1
            if restored > 0:
                l_conn.commit()
        finally:
            l_conn.close()
    except Exception as exc:
        print(f"[agy-persist] WARN: preserve_unpruned_summaries error: {exc}", file=sys.stderr)


def sqlite_needs_sync(local_db: str, bucket_db: str) -> bool:
    if not os.path.isfile(local_db) or os.path.getsize(local_db) == 0:
        return False
    if not os.path.isfile(bucket_db) or os.path.getsize(bucket_db) == 0:
        return True
    try:
        st_b = os.stat(bucket_db)
        st_l = os.stat(local_db)
        l_mtime = st_l.st_mtime_ns
        wal_path = local_db + "-wal"
        if os.path.isfile(wal_path):
            st_w = os.stat(wal_path)
            if st_w.st_size > 0:
                return True
            l_mtime = max(l_mtime, st_w.st_mtime_ns)
        return l_mtime > st_b.st_mtime_ns + 1_000_000 or st_l.st_size != st_b.st_size
    except OSError:
        return True


def sync_sqlite_file(env: PersistEnv, local_db: str, rel_persist_path: str, is_conversation_db: bool = False) -> bool:
    bucket_db = os.path.join(env.persist_root, rel_persist_path)
    if not sqlite_needs_sync(local_db, bucket_db):
        return False

    # Non-regression guard for conversation DBs: never overwrite a valid bucket DB that has MORE steps than local_db
    if is_conversation_db and check_sqlite_integrity(bucket_db) and check_sqlite_integrity(local_db):
        try:
            b_conn = sqlite3.connect(f"file:{urllib.parse.quote(os.path.abspath(bucket_db))}?mode=ro", uri=True, timeout=5.0)
            l_conn = sqlite3.connect(f"file:{urllib.parse.quote(os.path.abspath(local_db))}?mode=ro", uri=True, timeout=5.0)
            try:
                b_steps = b_conn.execute("SELECT count(*) FROM steps").fetchone()[0]
                l_steps = l_conn.execute("SELECT count(*) FROM steps").fetchone()[0]
                if l_steps < b_steps:
                    print(
                        f"[agy-persist] WARN: Refusing to overwrite {rel_persist_path} "
                        f"(bucket steps={b_steps} > local steps={l_steps})",
                        file=sys.stderr,
                    )
                    return False
            finally:
                b_conn.close()
                l_conn.close()
        except Exception:
            pass

    save_rolling_backup(env, rel_persist_path)
    return snapshot_sqlite_db(local_db, bucket_db, mode=0o600)


def cmd_sync(env: PersistEnv, final_flush: bool = False) -> int:
    lock_file = "/tmp/agy-persist.lock"
    with open(lock_file, "w") as lf:
        try:
            fcntl.flock(lf, fcntl.LOCK_EX | (0 if final_flush else fcntl.LOCK_NB))
        except BlockingIOError:
            return 0

        try:
            if not os.path.isdir(env.persist_root) or not os.access(env.persist_root, os.W_OK | os.X_OK):
                print(f"[agy-persist] WARN: Persist root {env.persist_root} unavailable; skipping sync tick", file=sys.stderr)
                return 1

            # 1. Sync agy-server state
            for fname in AGY_SERVER_FILES:
                sync_regular_file(
                    env,
                    os.path.join(env.agy_home, fname),
                    os.path.join("agy-server", fname),
                    mode=0o600,
                )

            # 2. Sync ~/.config/antigravity and ~/.gemini/config trees
            sync_directory_tree(env, env.config_agy_dir, "config-antigravity", default_mode=0o600)
            sync_directory_tree(env, os.path.join(env.gemini_dir, "config"), os.path.join("gemini", "config"), default_mode=0o600)

            # 3. Sync ~/.gemini root files
            for fname in GEMINI_ROOT_FILES:
                src = os.path.join(env.gemini_dir, fname)
                rel = os.path.join("gemini", "root", fname)
                if fname.endswith(".db"):
                    sync_sqlite_file(env, src, rel, is_conversation_db=False)
                else:
                    mode = 0o600 if "oauth-token" in fname else 0o644
                    sync_regular_file(env, src, rel, mode=mode)

            # 4. Sync ~/.gemini/{antigravity-cli,antigravity}
            for sub in GEMINI_SUBS:
                sub_local = os.path.join(env.gemini_dir, sub)
                if final_flush:
                    heal_and_reconcile_sub_dir(env, sub)

                # Sync subdirectories (annotations, implicit, brain) first so transcripts are durable before DBs
                for dname in GEMINI_SUB_DIRS:
                    sync_directory_tree(
                        env,
                        os.path.join(sub_local, dname),
                        os.path.join("gemini", sub, dname),
                        default_mode=0o644,
                    )

                # Sync conversations/*.db via SQLite Online Backup API
                l_conv = os.path.join(sub_local, "conversations")
                if os.path.isdir(l_conv):
                    for fn in sorted(os.listdir(l_conv)):
                        if fn.endswith(".db") and not fn.startswith("."):
                            sync_sqlite_file(
                                env,
                                os.path.join(l_conv, fn),
                                os.path.join("gemini", sub, "conversations", fn),
                                is_conversation_db=True,
                            )

                # Anti-prune guard before syncing conversation_summaries.db
                local_sum = os.path.join(sub_local, "conversation_summaries.db")
                bucket_sum = os.path.join(env.persist_root, "gemini", sub, "conversation_summaries.db")
                preserve_unpruned_summaries(
                    local_sum,
                    bucket_sum,
                    l_conv,
                    os.path.join(sub_local, "brain"),
                )

                for fname in GEMINI_SUB_FILES:
                    src = os.path.join(sub_local, fname)
                    rel = os.path.join("gemini", sub, fname)
                    if fname.endswith(".db"):
                        sync_sqlite_file(env, src, rel, is_conversation_db=False)
                    else:
                        mode = 0o600 if ("oauth-token" in fname or fname.endswith(".pb")) else 0o644
                        sync_regular_file(env, src, rel, mode=mode)

            # 5. Protect empty directories in workspace & .git
            repair_workspace_and_git(env)
            return 0
        finally:
            try:
                fcntl.flock(lf, fcntl.LOCK_UN)
            except OSError:
                pass


def main() -> int:
    parser = argparse.ArgumentParser(description="Antigravity Spaces Persistence & Recovery Engine")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("check-mount", help="Verify persistent mount availability and fail-closed policy")
    sub.add_parser("restore", help="Restore and self-heal all durable state before server launch")
    p_sync = sub.add_parser("sync", help="Atomically snapshot and sync local durable state to persistent storage")
    p_sync.add_argument("--final", action="store_true", help="Final shutdown flush (waits for lock & runs full reconciliation)")
    sub.add_parser("status", help="Print resolved worker persistence status JSON")

    args = parser.parse_args()
    env = PersistEnv()

    if args.cmd == "check-mount":
        return 0 if verify_mount_health(env) else 1
    elif args.cmd == "restore":
        return cmd_restore(env)
    elif args.cmd == "sync":
        return cmd_sync(env, final_flush=args.final)
    elif args.cmd == "status":
        inst_file = os.path.join(env.gemini_dir, "antigravity-cli", "installation_id")
        inst_id = ""
        if os.path.isfile(inst_file):
            with open(inst_file, "r", encoding="utf-8", errors="ignore") as f:
                inst_id = f.read().strip()
        print(
            json.dumps(
                {
                    "worker_id": env.worker_id,
                    "space_id": env.space_id,
                    "installation_id": inst_id,
                    "storage_mode": env.storage_mode,
                    "persist_root": env.persist_root,
                    "workspace_dir": env.workspace_dir,
                }
            )
        )
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
