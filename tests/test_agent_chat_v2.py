"""End-to-end contract and invariant tests for Agent Chat v2."""

import json
import os
import sqlite3
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest


HERE = os.path.dirname(os.path.abspath(__file__))
CLI = os.path.join(os.path.dirname(HERE), "agent-chat")


SESSIONS = [
    {
        "id": "pm-work",
        "title": "Work PM",
        "profile": "work",
        "group": "Team",
        "tool": "claude",
        "view": "terminal",
        "is_project_manager": True,
    },
    {
        "id": "worker-1",
        "title": "Worker One",
        "profile": "work",
        "group": "Team",
        "tool": "codex",
        "view": "terminal",
        "is_project_manager": False,
    },
    {
        "id": "worker-2",
        "title": "Worker Two",
        "profile": "work",
        "group": "Team",
        "tool": "claude",
        "view": "terminal",
        "is_project_manager": False,
    },
    {
        "id": "pm-other",
        "title": "Other PM",
        "profile": "other",
        "group": "Team",
        "tool": "codex",
        "view": "terminal",
        "is_project_manager": True,
    },
]


class AgentChatV2Test(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.temp.name, "mail.db")
        self.aoe_log = os.path.join(self.temp.name, "aoe.log")
        self.fake_aoe = os.path.join(self.temp.name, "aoe")
        with open(self.fake_aoe, "w", encoding="utf-8") as handle:
            handle.write(
                "#!/usr/bin/env python3\n"
                "import json, os, sys, time\n"
                "with open(os.environ['FAKE_AOE_LOG'], 'a', encoding='utf-8') as f:\n"
                "    f.write(json.dumps(sys.argv[1:]) + '\\n')\n"
                "time.sleep(float(os.environ.get('FAKE_AOE_SLEEP', '0')))\n"
                "marker = os.environ.get('FAKE_AOE_FAIL_FIRST_FILE')\n"
                "if marker and not os.path.exists(marker):\n"
                "    open(marker, 'w').close()\n"
                "    sys.exit(7)\n"
                "sys.exit(int(os.environ.get('FAKE_AOE_EXIT', '0')))\n"
            )
        os.chmod(self.fake_aoe, 0o755)
        self.env = {
            **os.environ,
            "AGENT_CHAT_DB": self.db,
            "AGENT_CHAT_AOE_BIN": self.fake_aoe,
            "AGENT_CHAT_AOE_SESSIONS_JSON": json.dumps(SESSIONS),
            "FAKE_AOE_LOG": self.aoe_log,
        }

    def tearDown(self):
        self.temp.cleanup()

    def run_cli(self, *args, identity=None, env=None):
        command = [sys.executable, CLI]
        if identity:
            command.extend(["--from", identity])
        command.extend(str(arg) for arg in args)
        return subprocess.run(
            command,
            capture_output=True,
            text=True,
            env={**self.env, **(env or {})},
        )

    def ok(self, *args, identity=None, env=None):
        out = self.run_cli(*args, identity=identity, env=env)
        self.assertEqual(out.returncode, 0, out.stderr or out.stdout)
        wire = json.loads(out.stdout)
        self.assertEqual(wire["v"], 1)
        self.assertEqual(wire["status"], "ok")
        self.assertTrue(wire["request_id"])
        return wire["data"]

    def error(self, code, *args, identity=None, env=None):
        out = self.run_cli(*args, identity=identity, env=env)
        self.assertEqual(out.returncode, 1, out.stdout)
        wire = json.loads(out.stdout)
        self.assertEqual(wire["v"], 1)
        self.assertEqual(wire["status"], "error")
        self.assertEqual(wire["error"]["code"], code)
        return wire["error"]

    def gm_start(
        self, *, profile="work", key="start", body="secret @worker text", env=None
    ):
        return self.ok(
            "post",
            "Team",
            "--profile",
            profile,
            "--expected-no-active",
            "--expected-generation",
            "1",
            "--body",
            body,
            "--idempotency-key",
            key,
            "--actor",
            "general-manager",
            "--json",
            env=env,
        )

    def panel(self, profile="work", conversation=None, group="Team", env=None):
        args = ["panel", group, "--profile", profile, "--viewer", f"aoe-tui:{profile}"]
        if conversation:
            args.extend(["--conversation", conversation])
        args.append("--json")
        return self.ok(*args, env=env)

    def aoe_calls(self):
        if not os.path.exists(self.aoe_log):
            return []
        with open(self.aoe_log, encoding="utf-8") as handle:
            return [json.loads(line) for line in handle if line.strip()]

    def db_rows(self, sql, params=()):
        conn = sqlite3.connect(self.db)
        conn.row_factory = sqlite3.Row
        try:
            return conn.execute(sql, params).fetchall()
        finally:
            conn.close()

    def test_panel_is_a_pure_read_and_legacy_rows_are_not_imported(self):
        legacy = self.run_cli(
            "ask",
            "worker-1:Worker One",
            "legacy",
            "--timeout",
            "0",
            "--no-doorbell",
            identity="legacy-sender:Legacy",
        )
        self.assertEqual(legacy.returncode, 3)
        data = self.panel()
        self.assertEqual(data["conversations"], [])
        self.assertEqual(len(self.db_rows("SELECT * FROM messages")), 1)
        self.assertEqual(len(self.db_rows("SELECT * FROM conversations")), 0)

    def test_checked_in_wire_fixtures_share_the_tagged_v1_envelope(self):
        fixture_dir = os.path.join(HERE, "fixtures")
        fixtures = {}
        for name in ("panel-v1.json", "mutation-v1.json", "error-v1.json"):
            with open(os.path.join(fixture_dir, name), encoding="utf-8") as handle:
                fixtures[name] = json.load(handle)
            self.assertEqual(fixtures[name]["v"], 1)
            self.assertTrue(fixtures[name]["request_id"])
        self.assertEqual(fixtures["panel-v1.json"]["status"], "ok")
        self.assertEqual(
            fixtures["panel-v1.json"]["data"]["detail"]["body_visibility"], "full"
        )
        self.assertEqual(fixtures["mutation-v1.json"]["status"], "ok")
        self.assertEqual(fixtures["error-v1.json"]["status"], "error")

    def test_newer_schema_marker_is_rejected_without_downgrade(self):
        conn = sqlite3.connect(self.db)
        conn.execute("CREATE TABLE meta(k TEXT PRIMARY KEY,v TEXT NOT NULL)")
        conn.execute("INSERT INTO meta(k,v) VALUES('schema_version','3')")
        conn.commit()
        conn.close()
        error = self.error("unsupported_schema_version", "capabilities", "--json")
        self.assertEqual(
            error["details"], {"stored_version": 3, "supported_version": 2}
        )
        conn = sqlite3.connect(self.db)
        try:
            marker = conn.execute(
                "SELECT v FROM meta WHERE k='schema_version'"
            ).fetchone()[0]
            tables = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
        finally:
            conn.close()
        self.assertEqual(marker, "3")
        self.assertNotIn("conversations", tables)

    def test_older_schema_marker_advances_only_after_additive_setup(self):
        conn = sqlite3.connect(self.db)
        conn.execute("CREATE TABLE meta(k TEXT PRIMARY KEY,v TEXT NOT NULL)")
        conn.execute("INSERT INTO meta(k,v) VALUES('schema_version','1')")
        conn.commit()
        conn.close()
        capability = self.ok("capabilities", "--json")
        self.assertEqual(capability["schema_version"], 2)
        row = self.db_rows("SELECT v FROM meta WHERE k='schema_version'")[0]
        self.assertEqual(row["v"], "2")
        self.assertTrue(
            self.db_rows("SELECT name FROM sqlite_master WHERE name='conversations'")
        )

    @unittest.skipIf(os.name == "nt", "POSIX permission bits are required")
    def test_dedicated_store_directory_and_database_are_private_and_repaired(self):
        directory = os.path.join(self.temp.name, "agent-chat")
        path = os.path.join(directory, "mail.db")
        env = {"AGENT_CHAT_DB": path}
        out = self.run_cli("capabilities", "--json", env=env)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(stat.S_IMODE(os.stat(directory).st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)

        os.chmod(directory, 0o755)
        os.chmod(path, 0o644)
        legacy = self.run_cli(
            "inbox", "--json", identity="worker-1:Worker One", env=env
        )
        self.assertEqual(legacy.returncode, 0, legacy.stderr)
        self.assertEqual(stat.S_IMODE(os.stat(directory).st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        for suffix in ("-wal", "-shm"):
            candidate = path + suffix
            if os.path.exists(candidate):
                self.assertEqual(stat.S_IMODE(os.stat(candidate).st_mode), 0o600)

    def test_rust_wire_fixture_and_retry_before_revision_cas(self):
        started = self.gm_start()
        self.assertEqual(
            set(started), {"conversation_id", "generation", "revision", "post_seq"}
        )
        self.assertEqual(
            (started["generation"], started["revision"], started["post_seq"]), (1, 2, 1)
        )
        repeated = self.gm_start()
        self.assertEqual(repeated, started)

        posted = self.ok(
            "post",
            "--conversation",
            started["conversation_id"],
            "--expected-revision",
            "2",
            "--body",
            "second",
            "--idempotency-key",
            "post-2",
            "--actor",
            "general-manager",
            "--json",
        )
        # The stored revision is now 3. An ambiguous retry still resolves from
        # idempotency before the stale expected-revision check.
        retried = self.ok(
            "post",
            "--conversation",
            started["conversation_id"],
            "--expected-revision",
            "2",
            "--body",
            "second",
            "--idempotency-key",
            "post-2",
            "--actor",
            "general-manager",
            "--json",
        )
        self.assertEqual(retried, posted)
        self.assertEqual(
            len(
                self.db_rows(
                    "SELECT * FROM posts WHERE conversation_id=?",
                    (started["conversation_id"],),
                )
            ),
            2,
        )
        fixture = self.panel(conversation=started["conversation_id"])
        self.assertEqual(fixture["group"], {"profile": "work", "group_path": "Team"})
        self.assertEqual(fixture["detail"]["body_visibility"], "full")
        self.assertEqual(fixture["detail"]["window"]["first_seq"], 1)
        self.assertEqual(fixture["detail"]["window"]["through_seq"], 2)

    def test_committed_replays_precede_unavailable_external_preflight(self):
        started = self.gm_start(key="preflight-start")
        empty = {"AGENT_CHAT_AOE_SESSIONS_JSON": "[]"}
        self.assertEqual(
            self.gm_start(key="preflight-start", env=empty),
            started,
        )

        route_args = (
            "route",
            started["conversation_id"],
            "worker-1",
            "--sequence",
            "1",
            "--expected-revision",
            "2",
            "--idempotency-key",
            "preflight-route",
            "--actor",
            "general-manager",
            "--no-doorbell",
            "--json",
        )
        routed = self.ok(*route_args)
        self.assertEqual(self.ok(*route_args, env=empty), routed)

        side_args = (
            "sidechat",
            "worker-2",
            "preflight private",
            "--group",
            "Team",
            "--profile",
            "work",
            "--idempotency-key",
            "preflight-side",
            "--no-doorbell",
            "--json",
        )
        side = self.ok(*side_args, identity="worker-1:Worker One")
        self.assertEqual(
            self.ok(*side_args, identity="worker-1:Worker One", env=empty),
            side,
        )

    def test_same_group_path_is_separate_per_profile(self):
        work = self.gm_start(profile="work", key="work")
        other = self.gm_start(profile="other", key="other")
        self.assertNotEqual(work["conversation_id"], other["conversation_id"])
        self.assertEqual(
            self.panel("work")["conversations"][0]["id"], work["conversation_id"]
        )
        self.assertEqual(
            self.panel("other")["conversations"][0]["id"], other["conversation_id"]
        )

    def test_ordinary_posts_and_plain_mentions_never_call_aoe(self):
        started = self.gm_start(body="@worker this is still storage only")
        self.ok(
            "post",
            "--conversation",
            started["conversation_id"],
            "--expected-revision",
            "2",
            "--body",
            "another @worker mention",
            "--idempotency-key",
            "ordinary",
            "--actor",
            "general-manager",
            "--json",
        )
        self.ok(
            "say",
            started["conversation_id"],
            "agent @mention",
            "--idempotency-key",
            "agent-say",
            "--json",
            identity="worker-1:Worker One",
        )
        self.assertEqual(self.aoe_calls(), [])

    def test_revision_conflict_is_typed(self):
        started = self.gm_start()
        error = self.error(
            "revision_conflict",
            "post",
            "--conversation",
            started["conversation_id"],
            "--expected-revision",
            "1",
            "--body",
            "stale",
            "--idempotency-key",
            "stale",
            "--actor",
            "general-manager",
            "--json",
        )
        self.assertTrue(error["retryable"])
        self.assertEqual(error["details"]["actual_revision"], 2)

    def test_route_is_explicit_body_free_idempotent_and_coalesced(self):
        started = self.gm_start(body="TOP SECRET BODY")
        first = self.ok(
            "route",
            started["conversation_id"],
            "worker-1",
            "--sequence",
            "1",
            "--expected-revision",
            "2",
            "--idempotency-key",
            "route-1",
            "--actor",
            "general-manager",
            "--json",
        )
        calls = self.aoe_calls()
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][:2], ["send", "worker-1"])
        self.assertNotIn("TOP SECRET BODY", calls[0][2])
        self.assertIn(started["conversation_id"], calls[0][2])
        self.assertEqual(
            self.ok(
                "route",
                started["conversation_id"],
                "worker-1",
                "--sequence",
                "1",
                "--expected-revision",
                "2",
                "--idempotency-key",
                "route-1",
                "--actor",
                "general-manager",
                "--json",
            ),
            first,
        )
        self.assertEqual(len(self.aoe_calls()), 1)

        posted = self.ok(
            "post",
            "--conversation",
            started["conversation_id"],
            "--expected-revision",
            "3",
            "--body",
            "new message",
            "--idempotency-key",
            "post-new",
            "--actor",
            "general-manager",
            "--json",
        )
        self.assertEqual(posted["post_seq"], 3)  # seq 2 is the route audit
        self.ok(
            "route",
            started["conversation_id"],
            "worker-1",
            "--sequence",
            "3",
            "--expected-revision",
            "4",
            "--idempotency-key",
            "route-2",
            "--actor",
            "general-manager",
            "--json",
        )
        wakes = self.db_rows("SELECT * FROM wake_requests")
        self.assertEqual(len(wakes), 1)
        self.assertEqual((wakes[0]["source_seq"], wakes[0]["attempt_count"]), (3, 2))
        self.assertEqual(len(self.aoe_calls()), 2)

    def test_non_moderator_cannot_route_worker(self):
        started = self.gm_start()
        self.error(
            "moderator_required",
            "route",
            started["conversation_id"],
            "worker-2",
            "--sequence",
            "1",
            "--expected-revision",
            "2",
            "--idempotency-key",
            "forbidden",
            "--no-doorbell",
            "--json",
            identity="worker-1:Worker One",
        )

    def test_done_new_generation_and_restore_conflict(self):
        first = self.gm_start()
        done = self.ok(
            "done",
            first["conversation_id"],
            "--expected-revision",
            "2",
            "--idempotency-key",
            "done-1",
            "--actor",
            "general-manager",
            "--json",
        )
        self.assertEqual(done["revision"], 3)
        second = self.ok(
            "post",
            "Team",
            "--profile",
            "work",
            "--expected-no-active",
            "--expected-generation",
            "2",
            "--body",
            "new generation",
            "--idempotency-key",
            "start-2",
            "--actor",
            "general-manager",
            "--json",
        )
        self.assertEqual(second["generation"], 2)
        self.error(
            "active_generation_conflict",
            "restore",
            first["conversation_id"],
            "--expected-revision",
            "3",
            "--idempotency-key",
            "restore-1",
            "--actor",
            "general-manager",
            "--json",
        )
        panel = self.panel()
        self.assertEqual(
            [(item["generation"], item["status"]) for item in panel["conversations"]],
            [(2, "active"), (1, "trashed")],
        )

    def test_side_chat_bodies_are_private_from_gm_panel(self):
        self.gm_start()
        side = self.ok(
            "sidechat",
            "worker-2",
            "SIDE SECRET",
            "--group",
            "Team",
            "--profile",
            "work",
            "--idempotency-key",
            "side-1",
            "--no-doorbell",
            "--json",
            identity="worker-1:Worker One",
        )
        panel = self.panel(conversation=side["conversation_id"])
        self.assertEqual(panel["detail"]["body_visibility"], "metadata_only")
        self.assertEqual(panel["detail"]["window"]["posts"], [])
        self.assertNotIn("SIDE SECRET", json.dumps(panel))
        read = self.ok(
            "read",
            side["conversation_id"],
            "--json",
            identity="worker-2:Worker Two",
        )
        self.assertEqual(read["posts"][0]["body"], "SIDE SECRET")
        self.error(
            "not_a_participant",
            "read",
            side["conversation_id"],
            "--json",
            identity="pm-work:Work PM",
        )
        self.error(
            "side_chat_private",
            "page",
            side["conversation_id"],
            "--before",
            "2",
            "--limit",
            "20",
            "--json",
        )

        self.error(
            "not_a_participant",
            "post",
            "--conversation",
            side["conversation_id"],
            "--expected-revision",
            "2",
            "--body",
            "GM MUST NOT ENTER PRIVATE CHAT",
            "--idempotency-key",
            "gm-side-injection",
            "--actor",
            "general-manager",
            "--json",
        )
        self.assertEqual(
            len(
                self.db_rows(
                    "SELECT * FROM posts WHERE conversation_id=?",
                    (side["conversation_id"],),
                )
            ),
            1,
        )

    def test_group_done_moves_linked_active_side_chats_to_trash(self):
        group = self.gm_start()
        side = self.ok(
            "sidechat",
            "worker-2",
            "private work",
            "--group",
            "Team",
            "--profile",
            "work",
            "--idempotency-key",
            "side-before-done",
            "--no-doorbell",
            "--json",
            identity="worker-1:Worker One",
        )
        self.ok(
            "done",
            group["conversation_id"],
            "--expected-revision",
            "2",
            "--idempotency-key",
            "group-done-cascade",
            "--actor",
            "general-manager",
            "--json",
        )
        statuses = {
            row["id"]: row["status"]
            for row in self.db_rows("SELECT id,status FROM conversations")
        }
        self.assertEqual(statuses[group["conversation_id"]], "trashed")
        self.assertEqual(statuses[side["conversation_id"]], "trashed")
        wake = self.db_rows(
            "SELECT state,lease_token FROM wake_requests WHERE conversation_id=?",
            (side["conversation_id"],),
        )[0]
        self.assertEqual((wake["state"], wake["lease_token"]), ("acknowledged", None))

    def test_human_seen_cursor_never_advances_agent_cursor(self):
        started = self.gm_start()
        seen = self.ok(
            "seen",
            started["conversation_id"],
            "99",
            "--viewer",
            "aoe-tui:work",
            "--json",
        )
        self.assertEqual(seen["through_seq"], 1)
        panel = self.panel(conversation=started["conversation_id"])
        self.assertEqual(panel["total_viewer_unread"], 0)
        participants = {
            row["session_id"]: row for row in panel["detail"]["participants"]
        }
        self.assertEqual(participants["worker-1"]["unread_count"], 1)
        cursor = self.db_rows(
            "SELECT last_read_seq FROM participants WHERE conversation_id=? AND session_id='worker-1'",
            (started["conversation_id"],),
        )[0]
        self.assertEqual(cursor["last_read_seq"], 0)

    def test_paged_history_is_gap_free_and_bounded(self):
        started = self.gm_start()
        revision = 2
        for number in range(2, 9):
            result = self.ok(
                "post",
                "--conversation",
                started["conversation_id"],
                "--expected-revision",
                str(revision),
                "--body",
                f"message {number}",
                "--idempotency-key",
                f"page-{number}",
                "--actor",
                "general-manager",
                "--json",
            )
            revision = result["revision"]
        page = self.ok(
            "page",
            started["conversation_id"],
            "--before",
            "8",
            "--limit",
            "3",
            "--json",
        )
        self.assertEqual([post["seq"] for post in page["window"]["posts"]], [5, 6, 7])
        self.assertTrue(page["window"]["has_more_before"])

    def test_concurrent_agent_posts_allocate_contiguous_unique_sequences(self):
        started = self.gm_start()
        outputs = []

        def post(number):
            outputs.append(
                self.run_cli(
                    "say",
                    started["conversation_id"],
                    f"parallel {number}",
                    "--idempotency-key",
                    f"parallel-{number}",
                    "--json",
                    identity="worker-1:Worker One",
                )
            )

        threads = [threading.Thread(target=post, args=(number,)) for number in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertTrue(
            all(out.returncode == 0 for out in outputs), [out.stderr for out in outputs]
        )
        rows = self.db_rows(
            "SELECT seq FROM posts WHERE conversation_id=? ORDER BY seq",
            (started["conversation_id"],),
        )
        self.assertEqual([row["seq"] for row in rows], list(range(1, 10)))

    def test_group_rename_and_profile_move_preserve_owner_and_conversation_id(self):
        started = self.gm_start()
        moved_sessions = [
            {
                **SESSIONS[0],
                "profile": "other",
                "group": "Renamed",
            },
            {
                **SESSIONS[1],
                "profile": "other",
                "group": "Renamed",
            },
            {
                "id": "worker-3",
                "title": "Worker Three",
                "profile": "other",
                "group": "Renamed",
                "tool": "codex",
                "view": "terminal",
                "is_project_manager": False,
            },
        ]
        moved_env = {"AGENT_CHAT_AOE_SESSIONS_JSON": json.dumps(moved_sessions)}
        # Panel discovery follows immutable owner ID without mutating storage.
        panel = self.panel(
            profile="other",
            group="Renamed",
            conversation=started["conversation_id"],
            env=moved_env,
        )
        self.assertEqual(panel["conversations"][0]["id"], started["conversation_id"])
        live = {row["session_id"]: row for row in panel["detail"]["participants"]}
        self.assertTrue(live["worker-2"]["departed"])
        self.assertFalse(live["worker-3"]["departed"])
        self.assertEqual(live["worker-3"]["unread_count"], 0)

        posted = self.ok(
            "post",
            "--conversation",
            started["conversation_id"],
            "--expected-revision",
            "2",
            "--body",
            "after move",
            "--idempotency-key",
            "after-move",
            "--actor",
            "general-manager",
            "--json",
            env=moved_env,
        )
        self.assertEqual(posted["conversation_id"], started["conversation_id"])
        row = self.db_rows(
            "SELECT profile,group_path,owner_id,current_moderator_id FROM conversations WHERE id=?",
            (started["conversation_id"],),
        )[0]
        self.assertEqual(tuple(row), ("other", "Renamed", "pm-work", "pm-work"))
        participants = {
            row["session_id"]: row
            for row in self.db_rows(
                "SELECT * FROM participants WHERE conversation_id=?",
                (started["conversation_id"],),
            )
        }
        self.assertIsNotNone(participants["worker-2"]["left_at"])
        self.assertEqual(participants["worker-3"]["last_read_seq"], 1)

    def test_pm_replacement_preserves_owner_and_updates_current_moderator(self):
        started = self.gm_start()
        replacement = [
            {
                "id": "pm-new",
                "title": "Replacement PM",
                "profile": "work",
                "group": "Team",
                "tool": "claude",
                "view": "terminal",
                "is_project_manager": True,
            },
            {**SESSIONS[0], "is_project_manager": False},
            SESSIONS[1],
            SESSIONS[2],
        ]
        replacement_env = {"AGENT_CHAT_AOE_SESSIONS_JSON": json.dumps(replacement)}
        panel = self.panel(conversation=started["conversation_id"], env=replacement_env)
        self.assertEqual(panel["moderator"]["session_id"], "pm-new")
        posted = self.ok(
            "post",
            "--conversation",
            started["conversation_id"],
            "--expected-revision",
            "2",
            "--body",
            "new PM landed",
            "--idempotency-key",
            "pm-change",
            "--actor",
            "general-manager",
            "--json",
            env=replacement_env,
        )
        row = self.db_rows(
            "SELECT owner_id,current_moderator_id FROM conversations WHERE id=?",
            (started["conversation_id"],),
        )[0]
        self.assertEqual(tuple(row), ("pm-work", "pm-new"))
        # Reconciliation writes one audit post and then the requested message.
        self.assertEqual(posted["post_seq"], 3)

    def test_pm_replacement_keeps_owner_across_later_generations(self):
        first = self.gm_start()
        self.ok(
            "done",
            first["conversation_id"],
            "--expected-revision",
            "2",
            "--idempotency-key",
            "done-old-pm",
            "--actor",
            "general-manager",
            "--json",
        )
        replacement = [
            {
                "id": "pm-new",
                "title": "Replacement PM",
                "profile": "work",
                "group": "Team",
                "tool": "claude",
                "view": "terminal",
                "is_project_manager": True,
            },
            {**SESSIONS[0], "is_project_manager": False},
            SESSIONS[1],
            SESSIONS[2],
        ]
        replacement_env = {"AGENT_CHAT_AOE_SESSIONS_JSON": json.dumps(replacement)}
        before = self.panel(env=replacement_env)
        self.assertEqual(before["conversations"][0]["generation"], 1)
        second = self.ok(
            "post",
            "Team",
            "--profile",
            "work",
            "--expected-no-active",
            "--expected-generation",
            "2",
            "--body",
            "replacement generation",
            "--idempotency-key",
            "replacement-generation",
            "--actor",
            "general-manager",
            "--json",
            env=replacement_env,
        )
        row = self.db_rows(
            "SELECT owner_id,current_moderator_id,generation FROM conversations WHERE id=?",
            (second["conversation_id"],),
        )[0]
        self.assertEqual(tuple(row), ("pm-work", "pm-new", 2))

    def test_reused_path_does_not_expose_or_append_to_previous_owner_history(self):
        old = self.gm_start()
        reused = [
            {
                **SESSIONS[0],
                "group": "Old Team",
            },
            {
                **SESSIONS[1],
                "group": "Old Team",
            },
            {
                "id": "pm-new",
                "title": "New Team PM",
                "profile": "work",
                "group": "Team",
                "tool": "claude",
                "view": "terminal",
                "is_project_manager": True,
            },
            {
                **SESSIONS[2],
                "group": "Team",
            },
        ]
        reused_env = {"AGENT_CHAT_AOE_SESSIONS_JSON": json.dumps(reused)}
        self.assertEqual(self.panel(env=reused_env)["conversations"], [])
        new = self.ok(
            "post",
            "Team",
            "--profile",
            "work",
            "--expected-no-active",
            "--expected-generation",
            "1",
            "--body",
            "fresh owner",
            "--idempotency-key",
            "reused-path",
            "--actor",
            "general-manager",
            "--json",
            env=reused_env,
        )
        self.assertNotEqual(new["conversation_id"], old["conversation_id"])
        self.assertEqual(
            self.panel(env=reused_env)["conversations"][0]["id"], new["conversation_id"]
        )
        old_panel = self.panel(group="Old Team", env=reused_env)
        self.assertEqual(old_panel["conversations"][0]["id"], old["conversation_id"])
        rows = self.db_rows(
            "SELECT id,owner_id,group_path FROM conversations WHERE kind='group'"
        )
        self.assertEqual(
            {tuple(row) for row in rows},
            {
                (old["conversation_id"], "pm-work", "Old Team"),
                (new["conversation_id"], "pm-new", "Team"),
            },
        )

    def test_reused_path_with_absent_old_owner_fails_closed(self):
        old = self.gm_start(body="OLD PRIVATE GROUP BODY")
        old_side = self.ok(
            "sidechat",
            "worker-2",
            "OLD SIDE BODY",
            "--group",
            "Team",
            "--profile",
            "work",
            "--idempotency-key",
            "old-side-before-reuse",
            "--no-doorbell",
            "--json",
            identity="worker-1:Worker One",
        )
        replacement = [
            {
                "id": "pm-new",
                "title": "Unrelated New PM",
                "profile": "work",
                "group": "Team",
                "tool": "claude",
                "view": "terminal",
                "is_project_manager": True,
            },
            SESSIONS[2],
        ]
        replacement_env = {"AGENT_CHAT_AOE_SESSIONS_JSON": json.dumps(replacement)}
        panel = self.panel(env=replacement_env)
        self.assertEqual(panel["conversations"], [])
        self.assertNotIn("OLD PRIVATE GROUP BODY", json.dumps(panel))

        new = self.ok(
            "post",
            "Team",
            "--profile",
            "work",
            "--expected-no-active",
            "--expected-generation",
            "1",
            "--body",
            "new logical group",
            "--idempotency-key",
            "absent-owner-reuse",
            "--actor",
            "general-manager",
            "--json",
            env=replacement_env,
        )
        self.assertNotEqual(new["conversation_id"], old["conversation_id"])
        rows = {
            row["id"]: row
            for row in self.db_rows(
                "SELECT id,owner_id,status FROM conversations WHERE kind='group'"
            )
        }
        self.assertEqual(
            (
                rows[old["conversation_id"]]["owner_id"],
                rows[old["conversation_id"]]["status"],
            ),
            ("pm-work", "trashed"),
        )
        self.assertEqual(
            (
                rows[new["conversation_id"]]["owner_id"],
                rows[new["conversation_id"]]["status"],
            ),
            ("pm-new", "active"),
        )
        side_status = self.db_rows(
            "SELECT status FROM conversations WHERE id=?",
            (old_side["conversation_id"],),
        )[0]["status"]
        self.assertEqual(side_status, "trashed")
        fresh_panel = self.panel(
            conversation=new["conversation_id"], env=replacement_env
        )
        self.assertEqual(
            [item["id"] for item in fresh_panel["conversations"]],
            [new["conversation_id"]],
        )
        self.assertNotIn("OLD PRIVATE GROUP BODY", json.dumps(fresh_panel))

    def test_deleted_group_is_not_presented_as_live_or_writable(self):
        started = self.gm_start()
        deleted_env = {"AGENT_CHAT_AOE_SESSIONS_JSON": "[]"}
        self.error(
            "group_not_found",
            "panel",
            "Team",
            "--profile",
            "work",
            "--viewer",
            "aoe-tui:work",
            "--json",
            env=deleted_env,
        )
        self.error(
            "group_deleted",
            "post",
            "--conversation",
            started["conversation_id"],
            "--expected-revision",
            "2",
            "--body",
            "must not land",
            "--idempotency-key",
            "deleted-post",
            "--actor",
            "general-manager",
            "--json",
            env=deleted_env,
        )
        self.assertEqual(len(self.db_rows("SELECT * FROM posts")), 1)

    def test_panel_fails_closed_when_authoritative_aoe_lookup_is_unavailable(self):
        self.gm_start(body="MUST NOT LEAK ON FALLBACK")
        out = self.run_cli(
            "panel",
            "Team",
            "--profile",
            "work",
            "--viewer",
            "aoe-tui:work",
            "--json",
            env={"AGENT_CHAT_AOE_SESSIONS_JSON": "not-json"},
        )
        self.assertEqual(out.returncode, 1)
        self.assertEqual(json.loads(out.stdout)["error"]["code"], "aoe_invalid_json")
        self.assertNotIn("MUST NOT LEAK ON FALLBACK", out.stdout)

    def test_ambiguous_live_moderator_blocks_direct_group_operations(self):
        started = self.gm_start()
        ambiguous = [
            *SESSIONS[:3],
            {
                **SESSIONS[3],
                "profile": "work",
                "group": "Team",
            },
        ]
        env = {"AGENT_CHAT_AOE_SESSIONS_JSON": json.dumps(ambiguous)}
        self.error(
            "moderator_ambiguous",
            "post",
            "--conversation",
            started["conversation_id"],
            "--expected-revision",
            "2",
            "--body",
            "must not land",
            "--idempotency-key",
            "ambiguous-post",
            "--actor",
            "general-manager",
            "--json",
            env=env,
        )
        self.error(
            "moderator_ambiguous",
            "route",
            started["conversation_id"],
            "worker-1",
            "--sequence",
            "1",
            "--expected-revision",
            "2",
            "--idempotency-key",
            "ambiguous-route",
            "--actor",
            "general-manager",
            "--no-doorbell",
            "--json",
            env=env,
        )
        self.error(
            "moderator_ambiguous",
            "read",
            started["conversation_id"],
            "--json",
            identity="worker-1:Worker One",
            env=env,
        )
        moved_ambiguous = [
            {**SESSIONS[0], "profile": "other", "group": "Moved"},
            {**SESSIONS[3], "group": "Moved"},
            {**SESSIONS[1], "profile": "other", "group": "Moved"},
        ]
        self.error(
            "moderator_ambiguous",
            "post",
            "--conversation",
            started["conversation_id"],
            "--expected-revision",
            "2",
            "--body",
            "must not move",
            "--idempotency-key",
            "ambiguous-moved-post",
            "--actor",
            "general-manager",
            "--json",
            env={"AGENT_CHAT_AOE_SESSIONS_JSON": json.dumps(moved_ambiguous)},
        )
        self.assertEqual(len(self.db_rows("SELECT * FROM posts")), 1)

    def test_restore_validates_and_rebinds_the_live_owner_group(self):
        started = self.gm_start()
        self.ok(
            "done",
            started["conversation_id"],
            "--expected-revision",
            "2",
            "--idempotency-key",
            "done-before-move",
            "--actor",
            "general-manager",
            "--json",
        )
        moved = [
            {**SESSIONS[0], "profile": "other", "group": "Restored"},
            {**SESSIONS[1], "profile": "other", "group": "Restored"},
        ]
        moved_env = {"AGENT_CHAT_AOE_SESSIONS_JSON": json.dumps(moved)}
        restored = self.ok(
            "restore",
            started["conversation_id"],
            "--expected-revision",
            "3",
            "--idempotency-key",
            "restore-moved",
            "--actor",
            "general-manager",
            "--json",
            env=moved_env,
        )
        self.assertGreaterEqual(restored["revision"], 4)
        row = self.db_rows(
            "SELECT profile,group_path,status FROM conversations WHERE id=?",
            (started["conversation_id"],),
        )[0]
        self.assertEqual(tuple(row), ("other", "Restored", "active"))
        panel = self.panel(
            profile="other",
            group="Restored",
            conversation=started["conversation_id"],
            env=moved_env,
        )
        self.assertEqual(panel["conversations"][0]["status"], "active")

    def test_concurrent_idempotent_routes_share_one_delivery_lease(self):
        started = self.gm_start()
        outputs = []
        route_args = (
            "route",
            started["conversation_id"],
            "worker-1",
            "--sequence",
            "1",
            "--expected-revision",
            "2",
            "--idempotency-key",
            "one-concurrent-route",
            "--actor",
            "general-manager",
            "--json",
        )

        def route():
            outputs.append(self.run_cli(*route_args, env={"FAKE_AOE_SLEEP": "0.25"}))

        threads = [threading.Thread(target=route) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertTrue(
            all(out.returncode == 0 for out in outputs), [out.stdout for out in outputs]
        )
        self.assertEqual(len(self.aoe_calls()), 1)
        wake = self.db_rows("SELECT * FROM wake_requests")[0]
        self.assertEqual(
            (wake["state"], wake["attempt_count"], wake["lease_token"]),
            ("delivered", 1, None),
        )

    def test_interleaved_attention_replay_delivers_its_exact_original_wake(self):
        started = self.gm_start()
        notify_args = (
            "notify-moderator",
            started["conversation_id"],
            "1",
            "--expected-revision",
            "2",
            "--idempotency-key",
            "notify-exact",
            "--no-doorbell",
            "--json",
        )
        self.ok(*notify_args, identity="worker-1:Worker One")
        self.ok(
            "route",
            started["conversation_id"],
            "worker-2",
            "--sequence",
            "1",
            "--expected-revision",
            "3",
            "--idempotency-key",
            "interleaved-route",
            "--actor",
            "general-manager",
            "--no-doorbell",
            "--json",
        )
        replay_args = tuple(arg for arg in notify_args if arg != "--no-doorbell")
        self.ok(*replay_args, identity="worker-1:Worker One")
        calls = self.aoe_calls()
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][:2], ["send", "pm-work"])
        targets = {
            row["target_id"]: row["state"]
            for row in self.db_rows("SELECT target_id,state FROM wake_requests")
        }
        self.assertEqual(targets, {"pm-work": "delivered", "worker-2": "pending"})

    def test_failed_exact_wake_is_redriven_by_same_key_retry(self):
        started = self.gm_start()
        args = (
            "route",
            started["conversation_id"],
            "worker-1",
            "--sequence",
            "1",
            "--expected-revision",
            "2",
            "--idempotency-key",
            "retry-failed-wake",
            "--actor",
            "general-manager",
            "--json",
        )
        self.ok(*args, env={"FAKE_AOE_EXIT": "7"})
        failed = self.db_rows("SELECT * FROM wake_requests")[0]
        self.assertEqual((failed["state"], failed["attempt_count"]), ("failed", 1))
        self.ok(*args)
        delivered = self.db_rows("SELECT * FROM wake_requests")[0]
        self.assertEqual(
            (delivered["id"], delivered["state"], delivered["attempt_count"]),
            (failed["id"], "delivered", 2),
        )

    def test_newer_coalesced_source_is_drained_after_older_delivery_fails(self):
        started = self.gm_start()
        second = self.ok(
            "post",
            "--conversation",
            started["conversation_id"],
            "--expected-revision",
            "2",
            "--body",
            "second source",
            "--idempotency-key",
            "second-source",
            "--actor",
            "general-manager",
            "--json",
        )
        self.assertEqual(second["post_seq"], 2)
        marker = os.path.join(self.temp.name, "failed-first")
        first_output = []

        def first_route():
            first_output.append(
                self.run_cli(
                    "route",
                    started["conversation_id"],
                    "worker-1",
                    "--sequence",
                    "1",
                    "--expected-revision",
                    "3",
                    "--idempotency-key",
                    "older-route",
                    "--actor",
                    "general-manager",
                    "--json",
                    env={
                        "FAKE_AOE_SLEEP": "0.25",
                        "FAKE_AOE_FAIL_FIRST_FILE": marker,
                    },
                )
            )

        thread = threading.Thread(target=first_route)
        thread.start()
        for _ in range(100):
            if len(self.aoe_calls()) >= 1:
                break
            time.sleep(0.01)
        self.assertEqual(len(self.aoe_calls()), 1)
        newer = self.ok(
            "route",
            started["conversation_id"],
            "worker-1",
            "--sequence",
            "2",
            "--expected-revision",
            "4",
            "--idempotency-key",
            "newer-route",
            "--actor",
            "general-manager",
            "--json",
            env={"FAKE_AOE_FAIL_FIRST_FILE": marker},
        )
        self.assertEqual(newer["revision"], 5)
        thread.join()
        self.assertEqual(first_output[0].returncode, 0, first_output[0].stdout)
        wake = self.db_rows("SELECT * FROM wake_requests")[0]
        self.assertEqual(
            (wake["source_seq"], wake["state"], wake["attempt_count"]),
            (2, "delivered", 2),
        )
        calls = self.aoe_calls()
        self.assertEqual(len(calls), 2)
        self.assertIn("sequence 1", calls[0][2])
        self.assertIn("sequence 2", calls[1][2])

    def test_read_acknowledgement_clears_an_in_flight_wake_lease(self):
        started = self.gm_start()
        output = []

        def route():
            output.append(
                self.run_cli(
                    "route",
                    started["conversation_id"],
                    "worker-1",
                    "--sequence",
                    "1",
                    "--expected-revision",
                    "2",
                    "--idempotency-key",
                    "leased-route",
                    "--actor",
                    "general-manager",
                    "--json",
                    env={"FAKE_AOE_SLEEP": "0.3"},
                )
            )

        thread = threading.Thread(target=route)
        thread.start()
        for _ in range(100):
            rows = self.db_rows("SELECT lease_token FROM wake_requests")
            if rows and rows[0]["lease_token"]:
                break
            time.sleep(0.01)
        read = self.ok(
            "read",
            started["conversation_id"],
            "--json",
            identity="worker-1:Worker One",
        )
        self.assertGreaterEqual(read["through_seq"], 1)
        thread.join()
        self.assertEqual(output[0].returncode, 0, output[0].stdout)
        wake = self.db_rows("SELECT * FROM wake_requests")[0]
        self.assertEqual(wake["state"], "acknowledged")
        self.assertIsNone(wake["lease_until"])
        self.assertIsNone(wake["lease_token"])


if __name__ == "__main__":
    unittest.main()
