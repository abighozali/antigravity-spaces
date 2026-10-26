#!/usr/bin/env python3
"""
Unit and regression tests for agy-persist.py.
Uses isolated temporary directories and synthetic data only.
"""

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest

PERSIST_SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "agy-persist.py")


class AgyPersistTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp_dir = tempfile.mkdtemp(prefix="agy-persist-ut-")
        self.bucket_mount = os.path.join(self.tmp_dir, "buckets", "private")
        self.home_dir = os.path.join(self.tmp_dir, "home")
        os.makedirs(self.bucket_mount, exist_ok=True)
        os.makedirs(self.home_dir, exist_ok=True)
        self.worker_id = "agy-worker-test"
        self.persist_root = os.path.join(self.bucket_mount, self.worker_id)

        self.env = os.environ.copy()
        self.env.update(
            {
                "HOME": self.home_dir,
                "AGY_HOME": os.path.join(self.home_dir, ".agy-remote"),
                "AGY_BUCKET_MOUNT": self.bucket_mount,
                "WORKER_ID": self.worker_id,
                "SPACE_ID": "example/agy-worker-test",
                "AGY_REQUIRE_BUCKET": "1",
            }
        )

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def run_cli(self, *args: str, env: dict | None = None, check: bool = True) -> subprocess.CompletedProcess:
        res = subprocess.run(
            [sys.executable, PERSIST_SCRIPT, *args],
            env=env or self.env,
            capture_output=True,
            text=True,
        )
        if check and res.returncode != 0:
            self.fail(f"agy-persist {args} failed (rc={res.returncode}):\n{res.stdout}\n{res.stderr}")
        return res

    def test_check_mount_fail_closed_when_bucket_missing(self) -> None:
        bad_env = self.env.copy()
        bad_env["AGY_BUCKET_MOUNT"] = os.path.join(self.tmp_dir, "missing-mount")
        bad_env["AGY_REQUIRE_BUCKET"] = "1"
        res = self.run_cli("check-mount", env=bad_env, check=False)
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("WORKER_INIT_FAIL", res.stderr)

    def test_restore_sync_and_factory_rebuild_survival(self) -> None:
        self.run_cli("check-mount")
        self.run_cli("restore")

        # Seed synthetic credentials, OAuth token, project config, and a conversation transcript
        os.makedirs(os.path.join(self.home_dir, ".agy-remote"), exist_ok=True)
        cred_path = os.path.join(self.home_dir, ".agy-remote", "credentials.json")
        with open(cred_path, "w", encoding="utf-8") as f:
            json.dump({"version": 1, "salt": "syn-salt", "hash": "syn-hash"}, f)

        cli_dir = os.path.join(self.home_dir, ".gemini", "antigravity-cli")
        with open(os.path.join(cli_dir, "antigravity-oauth-token"), "w", encoding="utf-8") as f:
            f.write("synthetic-oauth-token-payload")

        cid = "00000000-1111-2222-3333-444444444444"
        proj_id = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        ws_uri = f"file:///buckets/private/{self.worker_id}/workspace/demo-app"
        logs_dir = os.path.join(cli_dir, "brain", cid, ".system_generated", "logs")
        os.makedirs(logs_dir, exist_ok=True)
        transcript_path = os.path.join(logs_dir, "transcript_full.jsonl")
        steps = [
            {
                "step_index": 0,
                "type": "USER_INPUT",
                "status": "DONE",
                "created_at": "2026-10-26T00:00:01Z",
                "content": "Create a demo project",
            },
            {
                "step_index": 1,
                "type": "PLANNER_RESPONSE",
                "status": "DONE",
                "created_at": "2026-10-26T00:00:03Z",
                "content": "Demo project initialized.",
                "thinking": "Initializing project structure.",
            },
        ]
        with open(transcript_path, "w", encoding="utf-8") as f:
            for st in steps:
                f.write(json.dumps(st) + "\n")

        proj_dir = os.path.join(self.home_dir, ".gemini", "config", "projects")
        os.makedirs(proj_dir, exist_ok=True)
        with open(os.path.join(proj_dir, f"{proj_id}.json"), "w", encoding="utf-8") as f:
            json.dump(
                {
                    "id": proj_id,
                    "name": "demo-app",
                    "projectResources": {"resources": [{"folderUri": ws_uri}]},
                    "settings": {},
                    "isWorkspaceOnly": False,
                },
                f,
            )

        # Sync --final (reconciles transcript into SQLite DB + summaries + proto stores)
        self.run_cli("sync", "--final")

        with open(os.path.join(cli_dir, "installation_id"), "r", encoding="utf-8") as f:
            inst_id = f.read().strip()
        self.assertTrue(len(inst_id) > 10)

        # Simulate Factory Rebuild by wiping HOME completely
        shutil.rmtree(self.home_dir)
        os.makedirs(self.home_dir, exist_ok=True)
        self.run_cli("restore")

        # Verify installation_id, credentials, OAuth token, conversation DB, summaries DB, and proto stores
        with open(os.path.join(cli_dir, "installation_id"), "r", encoding="utf-8") as f:
            self.assertEqual(f.read().strip(), inst_id)
        self.assertTrue(os.path.isfile(cred_path))

        conv_db = os.path.join(cli_dir, "conversations", f"{cid}.db")
        self.assertTrue(os.path.isfile(conv_db))
        conn = sqlite3.connect(conv_db)
        self.assertEqual(conn.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        self.assertEqual(conn.execute("SELECT count(*) FROM steps").fetchone()[0], 2)
        conn.close()

        sum_db = os.path.join(cli_dir, "conversation_summaries.db")
        s_conn = sqlite3.connect(sum_db)
        self.assertEqual(s_conn.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        rows = s_conn.execute("SELECT conversation_id, step_count FROM conversation_summaries").fetchall()
        s_conn.close()
        self.assertEqual(rows, [(cid, 2)])

        for pb_name in ("agyhub_summaries_proto.pb", "jetbox_summaries_proto.pb"):
            pb_file = os.path.join(cli_dir, pb_name)
            self.assertTrue(os.path.isfile(pb_file))
            with open(pb_file, "rb") as f:
                self.assertIn(cid.encode("utf-8"), f.read())

        # Verify empty directory sentinel in workspace/demo-app and .git health
        ws_dir = os.path.join(self.persist_root, "workspace")
        self.assertTrue(os.path.isfile(os.path.join(ws_dir, "demo-app", ".agy-keep")))
        git_res = subprocess.run(["git", "-C", ws_dir, "status"], capture_output=True, text=True)
        self.assertEqual(git_res.returncode, 0)

    def test_corrupted_primary_fallback_and_anti_prune_guards(self) -> None:
        self.test_restore_sync_and_factory_rebuild_survival()
        cli_dir = os.path.join(self.home_dir, ".gemini", "antigravity-cli")
        cid = "00000000-1111-2222-3333-444444444444"

        # Trigger a second sync so backups/prev/ is populated for credentials.json
        cred_local = os.path.join(self.home_dir, ".agy-remote", "credentials.json")
        with open(cred_local, "w", encoding="utf-8") as f:
            json.dump({"version": 1, "salt": "syn-salt-2", "hash": "syn-hash-2"}, f)
        self.run_cli("sync", "--final")

        # 1. Corrupt primary credentials.json in bucket; verify restore falls back to backups/prev
        bucket_cred = os.path.join(self.persist_root, "agy-server", "credentials.json")
        with open(bucket_cred, "wb") as f:
            f.write(b"{invalid-truncated-json")

        shutil.rmtree(self.home_dir)
        os.makedirs(self.home_dir, exist_ok=True)
        self.run_cli("restore")
        with open(cred_local, "r", encoding="utf-8") as f:
            restored_cred = json.load(f)
        self.assertIn("hash", restored_cred)

        # 2. Simulate summary pruning + 0-byte local credential write; verify sync protects bucket state
        self.run_cli("sync", "--final")
        local_sum = os.path.join(cli_dir, "conversation_summaries.db")
        conn = sqlite3.connect(local_sum)
        conn.execute("DELETE FROM conversation_summaries WHERE conversation_id = ?", (cid,))
        conn.commit()
        conn.close()

        with open(cred_local, "wb") as f:
            pass

        self.run_cli("sync")
        bucket_sum = os.path.join(self.persist_root, "gemini", "antigravity-cli", "conversation_summaries.db")
        b_conn = sqlite3.connect(bucket_sum)
        b_cids = [r[0] for r in b_conn.execute("SELECT conversation_id FROM conversation_summaries").fetchall()]
        b_conn.close()
        self.assertEqual(b_cids, [cid])
        self.assertGreater(os.path.getsize(bucket_cred), 10)


if __name__ == "__main__":
    unittest.main()
