"""End-to-end tests for agent-chat, driven through the real CLI.

No aoe daemon needed: identities use --from, recipients use 'id:title', and
--no-doorbell skips `aoe send`. Run: python3 -m unittest discover -s tests
"""
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
CLI = os.path.join(os.path.dirname(HERE), "agent-chat")

A = "a1:AgentA"
B = "b1:AgentB"


class AgentChatTest(unittest.TestCase):
    def setUp(self):
        fd, self.db = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        os.remove(self.db)  # let the tool create it fresh

    def tearDown(self):
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(self.db + suffix)
            except OSError:
                pass

    def run_cli(self, *args, identity=None):
        cmd = [sys.executable, CLI]
        if identity:
            cmd += ["--from", identity]
        cmd += list(args)
        env = {**os.environ, "AGENT_CHAT_DB": self.db}
        return subprocess.run(cmd, capture_output=True, text=True, env=env)

    def question_id_for(self, identity):
        out = self.run_cli("inbox", "--json", identity=identity)
        rows = json.loads(out.stdout)
        return rows[0]["id"] if rows else None

    # --- tests -------------------------------------------------------------
    def test_whoami_override(self):
        out = self.run_cli("whoami", identity=A)
        self.assertEqual(out.returncode, 0)
        self.assertIn("a1", out.stdout)
        self.assertIn("AgentA", out.stdout)

    def test_async_roundtrip(self):
        # A asks, times out fast -> pending
        ask = self.run_cli("ask", B, "what restitution?", "--timeout", "1",
                           "--no-doorbell", identity=A)
        self.assertEqual(ask.returncode, 0)
        self.assertIn("pending", ask.stderr)

        # B sees it in inbox
        qid = self.question_id_for(B)
        self.assertIsNotNone(qid)

        # B replies
        rep = self.run_cli("reply", qid, "restitution=0.4", "--no-doorbell", identity=B)
        self.assertEqual(rep.returncode, 0)

        # question now answered -> drops off B's inbox
        self.assertIsNone(self.question_id_for(B))

        # A retrieves the reply, then it's consumed
        got = self.run_cli("replies", identity=A)
        self.assertIn("restitution=0.4", got.stdout)
        again = self.run_cli("replies", identity=A)
        self.assertIn("no new replies", again.stdout)

    def test_blocking_poll_returns_reply(self):
        # B replies ~1.5s after A starts a 6s blocking ask; ask must return it.
        def delayed_reply():
            for _ in range(40):
                qid = self.question_id_for(B)
                if qid:
                    self.run_cli("reply", qid, "ANSWER-42", "--no-doorbell", identity=B)
                    return
                time.sleep(0.25)

        t = threading.Thread(target=delayed_reply)
        t.start()
        ask = self.run_cli("ask", B, "blocking?", "--timeout", "6", "--no-doorbell",
                           identity=A)
        t.join()
        self.assertEqual(ask.returncode, 0)
        self.assertIn("ANSWER-42", ask.stdout)
        self.assertNotIn("pending", ask.stderr)

    def test_thread_shows_q_and_a(self):
        self.run_cli("ask", B, "Q-BODY", "--timeout", "1", "--no-doorbell", identity=A)
        qid = self.question_id_for(B)
        self.run_cli("reply", qid, "A-BODY", "--no-doorbell", identity=B)
        out = self.run_cli("thread", qid, identity=A)
        self.assertIn("Q-BODY", out.stdout)
        self.assertIn("A-BODY", out.stdout)
        self.assertIn("[Q]", out.stdout)
        self.assertIn("[A]", out.stdout)

    def test_unknown_reply_id_errors(self):
        out = self.run_cli("reply", "deadbeef", "x", "--no-doorbell", identity=B)
        self.assertNotEqual(out.returncode, 0)
        self.assertIn("no question", out.stderr)


if __name__ == "__main__":
    unittest.main()
