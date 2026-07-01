"""Agent Chat v2 conversation store and versioned CLI protocol.

The v1 question/reply table is intentionally not read or migrated here. This
module owns only the additive group and side-chat model. Ordinary writes and
targeted wakes are separate code paths by construction.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any, Callable


WIRE_VERSION = 1
SCHEMA_VERSION = 2
DEFAULT_DB = os.path.expanduser("~/.local/share/agent-chat/mail.db")
DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 200
GENERAL_MANAGER_ID = "general-manager"
GENERAL_MANAGER_TITLE = "General Manager"
STORAGE_OPEN_LOCK = threading.Lock()


class ProtocolFault(Exception):
    """A stable error returned through the v2 JSON envelope."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        retryable: bool = False,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable
        self.details = details or {}

    def wire(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "retryable": self.retryable,
            "details": self.details,
        }


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _new_id() -> str:
    return uuid.uuid4().hex


def _db_path() -> str:
    return os.environ.get("AGENT_CHAT_DB", DEFAULT_DB)


def prepare_storage(path: str) -> None:
    """Create private SQLite storage without mutating arbitrary custom parents."""
    parent = os.path.dirname(path)
    if parent:
        created_parent = not os.path.exists(parent)
        os.makedirs(parent, mode=0o700, exist_ok=True)
        if (
            created_parent
            or os.path.abspath(path) == os.path.abspath(DEFAULT_DB)
            or os.path.basename(parent) == "agent-chat"
        ):
            os.chmod(parent, 0o700)
    if not os.path.exists(path):
        descriptor = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
        os.close(descriptor)
    os.chmod(path, 0o600)


def repair_storage_modes(path: str) -> None:
    for candidate in (path, f"{path}-wal", f"{path}-shm"):
        if os.path.exists(candidate):
            os.chmod(candidate, 0o600)


def _open_db() -> sqlite3.Connection:
    path = _db_path()
    # os.umask is process-global. Serialize the short connection bootstrap so
    # parallel broadcast delivery cannot restore another thread's umask.
    with STORAGE_OPEN_LOCK:
        prepare_storage(path)
        previous_umask = os.umask(0o077)
        try:
            conn = sqlite3.connect(path, timeout=30, isolation_level=None)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA busy_timeout=5000")
            _reject_unsupported_schema(conn)
            for attempt in range(6):
                try:
                    conn.execute("PRAGMA journal_mode=WAL")
                    break
                except sqlite3.OperationalError as error:
                    if "locked" not in str(error).lower() or attempt == 5:
                        raise
                    time.sleep(0.05 * (attempt + 1))
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA foreign_keys=ON")
            _ensure_schema(conn)
        finally:
            os.umask(previous_umask)
            repair_storage_modes(path)
    return conn


def _ensure_schema(conn: sqlite3.Connection) -> None:
    # Keep this DDL additive. In particular, never ALTER or import v1 rows.
    conn.execute(
        "CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT NOT NULL)"
    )
    _reject_unsupported_schema(conn)
    _install_schema(conn)


def _reject_unsupported_schema(conn: sqlite3.Connection) -> None:
    has_meta = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='meta'"
    ).fetchone()
    if has_meta is None:
        return
    marker = conn.execute("SELECT v FROM meta WHERE k='schema_version'").fetchone()
    if marker is not None:
        try:
            stored_version = int(marker["v"])
        except (TypeError, ValueError) as error:
            raise ProtocolFault(
                "invalid_schema_version",
                "mail.db has an invalid Agent Chat schema marker",
                details={"stored_version": marker["v"]},
            ) from error
        if stored_version > SCHEMA_VERSION:
            raise ProtocolFault(
                "unsupported_schema_version",
                "mail.db was written by a newer Agent Chat; upgrade this CLI",
                details={
                    "stored_version": stored_version,
                    "supported_version": SCHEMA_VERSION,
                },
            )


def _install_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS conversations (
            id                   TEXT PRIMARY KEY,
            kind                 TEXT NOT NULL CHECK(kind IN ('group','side')),
            profile              TEXT NOT NULL,
            group_path           TEXT NOT NULL,
            generation           INTEGER NOT NULL DEFAULT 1,
            title                TEXT NOT NULL,
            status               TEXT NOT NULL DEFAULT 'active'
                                 CHECK(status IN ('active','trashed')),
            owner_id             TEXT NOT NULL,
            current_moderator_id TEXT,
            parent_id            TEXT REFERENCES conversations(id),
            revision             INTEGER NOT NULL DEFAULT 1,
            next_seq             INTEGER NOT NULL DEFAULT 1,
            created_at           TEXT NOT NULL,
            done_at              TEXT,
            trashed_at           TEXT,
            restored_at          TEXT
        );

        CREATE UNIQUE INDEX IF NOT EXISTS ux_v2_active_group_key
            ON conversations(profile, group_path)
            WHERE kind='group' AND status='active';
        CREATE UNIQUE INDEX IF NOT EXISTS ux_v2_active_group_owner
            ON conversations(owner_id)
            WHERE kind='group' AND status='active';
        CREATE UNIQUE INDEX IF NOT EXISTS ux_v2_group_generation
            ON conversations(owner_id, generation)
            WHERE kind='group';
        CREATE INDEX IF NOT EXISTS ix_v2_conversations_group
            ON conversations(profile, group_path, kind, status);

        CREATE TABLE IF NOT EXISTS participants (
            conversation_id TEXT NOT NULL REFERENCES conversations(id),
            session_id      TEXT NOT NULL,
            session_title   TEXT NOT NULL,
            role            TEXT NOT NULL DEFAULT 'member'
                            CHECK(role IN ('moderator','member')),
            last_read_seq   INTEGER NOT NULL DEFAULT 0,
            joined_at       TEXT NOT NULL,
            left_at         TEXT,
            muted           INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (conversation_id, session_id)
        );

        CREATE TABLE IF NOT EXISTS posts (
            id              TEXT PRIMARY KEY,
            conversation_id TEXT NOT NULL REFERENCES conversations(id),
            seq             INTEGER NOT NULL,
            from_id         TEXT NOT NULL,
            from_title      TEXT NOT NULL,
            body            TEXT NOT NULL,
            kind            TEXT NOT NULL DEFAULT 'message'
                            CHECK(kind IN ('message','route','system')),
            routed_to       TEXT,
            created_at      TEXT NOT NULL,
            UNIQUE (conversation_id, seq)
        );
        CREATE INDEX IF NOT EXISTS ix_v2_posts_conversation_seq
            ON posts(conversation_id, seq);

        CREATE TABLE IF NOT EXISTS viewer_cursors (
            viewer_id       TEXT NOT NULL,
            conversation_id TEXT NOT NULL REFERENCES conversations(id),
            last_seen_seq   INTEGER NOT NULL DEFAULT 0,
            updated_at      TEXT NOT NULL,
            PRIMARY KEY (viewer_id, conversation_id)
        );

        CREATE TABLE IF NOT EXISTS wake_requests (
            id               TEXT PRIMARY KEY,
            conversation_id  TEXT NOT NULL REFERENCES conversations(id),
            source_seq       INTEGER NOT NULL,
            requester_id     TEXT NOT NULL,
            target_id        TEXT NOT NULL,
            state            TEXT NOT NULL DEFAULT 'pending'
                             CHECK(state IN ('pending','delivered','failed','acknowledged')),
            delivery_key     TEXT NOT NULL,
            attempt_count    INTEGER NOT NULL DEFAULT 0,
            lease_until      REAL,
            lease_token      TEXT,
            last_attempt_at  TEXT,
            delivered_at     TEXT,
            acknowledged_at  TEXT,
            last_error       TEXT,
            created_at       TEXT NOT NULL,
            updated_at       TEXT NOT NULL
        );
        CREATE UNIQUE INDEX IF NOT EXISTS ux_v2_open_wake
            ON wake_requests(conversation_id, target_id)
            WHERE state != 'acknowledged';
        CREATE INDEX IF NOT EXISTS ix_v2_wake_target
            ON wake_requests(target_id, state, source_seq);

        CREATE TABLE IF NOT EXISTS idempotency_results (
            idempotency_key TEXT PRIMARY KEY,
            operation       TEXT NOT NULL,
            request_hash    TEXT NOT NULL,
            response_json   TEXT NOT NULL,
            created_at      TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS idempotency_wakes (
            idempotency_key TEXT PRIMARY KEY,
            wake_id         TEXT NOT NULL,
            target_id       TEXT NOT NULL,
            FOREIGN KEY (wake_id) REFERENCES wake_requests(id)
        );

        CREATE TABLE IF NOT EXISTS group_bindings (
            profile      TEXT NOT NULL,
            group_path   TEXT NOT NULL,
            owner_id     TEXT NOT NULL,
            updated_at   TEXT NOT NULL,
            PRIMARY KEY (profile, group_path),
            UNIQUE (owner_id)
        );

        """
    )
    conn.execute(
        "INSERT INTO meta(k,v) VALUES('schema_version',?) "
        "ON CONFLICT(k) DO UPDATE SET v=CASE "
        "WHEN CAST(meta.v AS INTEGER) < CAST(excluded.v AS INTEGER) "
        "THEN excluded.v ELSE meta.v END",
        (str(SCHEMA_VERSION),),
    )
    # During v2 development a database may already contain the first additive
    # draft. Keep the migration additive and idempotent.
    wake_columns = {
        row["name"]
        for row in conn.execute("PRAGMA table_info(wake_requests)").fetchall()
    }
    if "lease_token" not in wake_columns:
        conn.execute("ALTER TABLE wake_requests ADD COLUMN lease_token TEXT")


def _rollback(conn: sqlite3.Connection) -> None:
    try:
        conn.execute("ROLLBACK")
    except sqlite3.Error:
        pass


def _immediate(conn: sqlite3.Connection, callback: Callable[[], Any]) -> Any:
    for attempt in range(6):
        try:
            conn.execute("BEGIN IMMEDIATE")
            value = callback()
            conn.execute("COMMIT")
            return value
        except sqlite3.OperationalError as error:
            _rollback(conn)
            if "locked" not in str(error).lower() or attempt == 5:
                raise
            time.sleep(0.05 * (attempt + 1))
        except Exception:
            _rollback(conn)
            raise
    raise AssertionError("unreachable")


def _request_hash(operation: str, payload: dict[str, Any]) -> str:
    raw = json.dumps(
        {"operation": operation, "payload": payload},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode()
    return hashlib.sha256(raw).hexdigest()


def _idempotent(
    conn: sqlite3.Connection,
    key: str,
    operation: str,
    payload: dict[str, Any],
    callback: Callable[[], dict[str, Any]],
) -> tuple[dict[str, Any], bool]:
    if not key:
        raise ProtocolFault(
            "idempotency_key_required", "an idempotency key is required"
        )
    digest = _request_hash(operation, payload)
    row = conn.execute(
        "SELECT operation,request_hash,response_json FROM idempotency_results "
        "WHERE idempotency_key=?",
        (key,),
    ).fetchone()
    if row is not None:
        if row["operation"] != operation or row["request_hash"] != digest:
            raise ProtocolFault(
                "idempotency_conflict",
                "the idempotency key was already used for a different request",
                details={"idempotency_key": key},
            )
        return json.loads(row["response_json"]), True
    result = callback()
    conn.execute(
        "INSERT INTO idempotency_results"
        "(idempotency_key,operation,request_hash,response_json,created_at) "
        "VALUES(?,?,?,?,?)",
        (key, operation, digest, json.dumps(result, sort_keys=True), _now()),
    )
    return result, False


def _cached_idempotent(
    conn: sqlite3.Connection,
    key: str,
    operation: str,
    payload: dict[str, Any],
) -> dict[str, Any] | None:
    """Return a durable result before any external/current-state preflight."""
    row = conn.execute(
        "SELECT operation,request_hash,response_json FROM idempotency_results "
        "WHERE idempotency_key=?",
        (key,),
    ).fetchone()
    if row is None:
        return None
    digest = _request_hash(operation, payload)
    if row["operation"] != operation or row["request_hash"] != digest:
        raise ProtocolFault(
            "idempotency_conflict",
            "the idempotency key was already used for a different request",
            details={"idempotency_key": key},
        )
    return json.loads(row["response_json"])


def _aoe_bin() -> str:
    return os.environ.get("AGENT_CHAT_AOE_BIN", "aoe")


def _aoe_json(args: list[str]) -> Any:
    try:
        proc = subprocess.run(
            [_aoe_bin(), *args, "--json"], capture_output=True, text=True, timeout=10
        )
    except subprocess.TimeoutExpired as error:
        raise ProtocolFault(
            "aoe_timeout",
            "AoE did not respond in time",
            retryable=True,
            details={"timeout_seconds": 10},
        ) from error
    except OSError as error:
        raise ProtocolFault(
            "aoe_unavailable",
            "cannot run the AoE CLI",
            retryable=True,
            details={"error": str(error)},
        ) from error
    if proc.returncode != 0:
        raise ProtocolFault(
            "aoe_unavailable",
            f"`aoe {' '.join(args)}` failed",
            retryable=True,
            details={"stderr": proc.stderr.strip()},
        )
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as error:
        raise ProtocolFault(
            "aoe_invalid_json",
            "AoE returned invalid JSON",
            retryable=True,
            details={"error": str(error)},
        ) from error


def _sessions(
    profile: str | None = None, *, all_profiles: bool = False
) -> list[dict[str, Any]]:
    fixture = os.environ.get("AGENT_CHAT_AOE_SESSIONS_JSON")
    if fixture is not None:
        try:
            value = json.loads(fixture)
        except json.JSONDecodeError as error:
            raise ProtocolFault(
                "aoe_invalid_json",
                "AGENT_CHAT_AOE_SESSIONS_JSON is invalid",
                details={"error": str(error)},
            ) from error
    else:
        args: list[str] = []
        if profile:
            args.extend(["--profile", profile])
        args.append("list")
        if all_profiles:
            args.append("--all")
        value = _aoe_json(args)
    if isinstance(value, dict):
        value = value.get("sessions", [])
    if not isinstance(value, list):
        raise ProtocolFault("aoe_invalid_json", "AoE session list is not an array")
    rows = [row for row in value if isinstance(row, dict)]
    if profile is not None:
        rows = [row for row in rows if (row.get("profile") or profile) == profile]
    return rows


def _current() -> dict[str, Any]:
    env = os.environ.get("AGENT_CHAT_ID")
    if env:
        sid, _, title = env.partition(":")
        return {
            "id": sid,
            "title": title or sid,
            "session": title or sid,
            "profile": os.environ.get("AGENT_OF_EMPIRES_PROFILE", "default"),
            "group": os.environ.get("AGENT_CHAT_GROUP", ""),
        }
    value = _aoe_json(["session", "current"])
    if not isinstance(value, dict) or not value.get("id"):
        raise ProtocolFault(
            "identity_unavailable", "cannot determine current AoE session"
        )
    return value


def _identity(args: argparse.Namespace) -> tuple[str, str]:
    if getattr(args, "actor", None) == "general-manager":
        return GENERAL_MANAGER_ID, GENERAL_MANAGER_TITLE
    spec = getattr(args, "from_", None)
    if spec:
        sid, _, title = spec.partition(":")
        return sid, title or sid
    current = _current()
    return current["id"], current.get("session") or current.get("title") or current[
        "id"
    ]


def _profile(args: argparse.Namespace) -> str:
    if getattr(args, "profile", None):
        return args.profile
    env = os.environ.get("AGENT_OF_EMPIRES_PROFILE")
    if env:
        return env
    return str(_current().get("profile") or "default")


def _group_members(
    profile: str, group_path: str
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    members = [row for row in _sessions(profile) if row.get("group", "") == group_path]
    moderators = [row for row in members if row.get("is_project_manager")]
    if not moderators:
        raise ProtocolFault(
            "moderator_not_found",
            "the group has no Project Manager",
            details={"profile": profile, "group_path": group_path},
        )
    if len(moderators) > 1:
        raise ProtocolFault(
            "moderator_ambiguous",
            "the group has more than one Project Manager",
            details={"profile": profile, "group_path": group_path},
        )
    return moderators[0], members


def _session_group_key(session: dict[str, Any]) -> tuple[str, str] | None:
    profile = str(session.get("profile") or "")
    group_path = str(session.get("group") or "")
    if not profile or not group_path:
        return None
    return profile, group_path


def _pm_session(
    sessions: list[dict[str, Any]], session_id: str
) -> dict[str, Any] | None:
    return next(
        (
            row
            for row in sessions
            if row.get("id") == session_id and row.get("is_project_manager")
        ),
        None,
    )


def _session_by_id(
    sessions: list[dict[str, Any]], session_id: str
) -> dict[str, Any] | None:
    return next((row for row in sessions if row.get("id") == session_id), None)


def _members_from_snapshot(
    sessions: list[dict[str, Any]], profile: str, group_path: str
) -> list[dict[str, Any]]:
    return [row for row in sessions if _session_group_key(row) == (profile, group_path)]


def _bind_group(
    conn: sqlite3.Connection, profile: str, group_path: str, owner_id: str
) -> None:
    # An owner follows a renamed/moved group, and a reused path changes owner.
    conn.execute("DELETE FROM group_bindings WHERE owner_id=?", (owner_id,))
    conn.execute(
        "INSERT INTO group_bindings(profile,group_path,owner_id,updated_at) VALUES(?,?,?,?) "
        "ON CONFLICT(profile,group_path) DO UPDATE SET owner_id=excluded.owner_id,"
        "updated_at=excluded.updated_at",
        (profile, group_path, owner_id, _now()),
    )


def _reconcile_group(
    conn: sqlite3.Connection,
    conversation: sqlite3.Row,
    profile: str,
    group_path: str,
    moderator: dict[str, Any],
    members: list[dict[str, Any]],
) -> sqlite3.Row:
    """Rebind a live owner and reconcile exact group membership atomically."""
    if conversation["kind"] != "group" or conversation["status"] != "active":
        return conversation
    old_profile = conversation["profile"]
    old_path = conversation["group_path"]
    old_moderator = conversation["current_moderator_id"]
    moderator_id = str(moderator["id"])
    owner_id = conversation["owner_id"]
    changes: list[str] = []

    if (old_profile, old_path) != (profile, group_path):
        conflict = conn.execute(
            "SELECT id FROM conversations WHERE kind='group' AND status='active' "
            "AND profile=? AND group_path=? AND id!=?",
            (profile, group_path, conversation["id"]),
        ).fetchone()
        if conflict is not None:
            raise ProtocolFault(
                "active_generation_conflict",
                "another active conversation already occupies the group's new path",
                retryable=True,
                details={"active_conversation_id": conflict["id"]},
            )
        conn.execute(
            "UPDATE conversations SET profile=?,group_path=? WHERE kind='group' AND owner_id=?",
            (profile, group_path, owner_id),
        )
        conn.execute(
            "UPDATE conversations SET profile=?,group_path=? WHERE kind='side' AND parent_id IN "
            "(SELECT id FROM conversations WHERE kind='group' AND owner_id=?)",
            (profile, group_path, owner_id),
        )
        changes.append(
            f"group moved from {old_profile}/{old_path} to {profile}/{group_path}"
        )

    highest = _highest_seq(conn, conversation["id"])
    live: dict[str, dict[str, Any]] = {
        str(member["id"]): member for member in members if member.get("id")
    }
    stored = conn.execute(
        "SELECT * FROM participants WHERE conversation_id=?",
        (conversation["id"],),
    ).fetchall()
    stored_ids = {row["session_id"] for row in stored}
    now = _now()
    membership_changed = False
    for row in stored:
        member = live.get(row["session_id"])
        if member is None:
            if row["left_at"] is None:
                membership_changed = True
            conn.execute(
                "UPDATE participants SET left_at=COALESCE(left_at,?) WHERE conversation_id=? "
                "AND session_id=?",
                (now, conversation["id"], row["session_id"]),
            )
            continue
        title = str(member.get("title") or member.get("session") or row["session_id"])
        role = "moderator" if row["session_id"] == moderator_id else "member"
        if (
            row["left_at"] is not None
            or row["session_title"] != title
            or row["role"] != role
        ):
            membership_changed = True
        conn.execute(
            "UPDATE participants SET session_title=?,role=?,left_at=NULL WHERE "
            "conversation_id=? AND session_id=?",
            (title, role, conversation["id"], row["session_id"]),
        )
    for sid, member in live.items():
        if sid in stored_ids:
            continue
        membership_changed = True
        title = str(member.get("title") or member.get("session") or sid)
        role = "moderator" if sid == moderator_id else "member"
        conn.execute(
            "INSERT INTO participants"
            "(conversation_id,session_id,session_title,role,last_read_seq,joined_at,muted) "
            "VALUES(?,?,?,?,?,?,0)",
            (conversation["id"], sid, title, role, highest, now),
        )
    if old_moderator != moderator_id:
        changes.append(f"moderator changed to {moderator.get('title') or moderator_id}")
    if membership_changed:
        changes.append("group membership reconciled")
    conn.execute(
        "UPDATE conversations SET current_moderator_id=? WHERE id=?",
        (moderator_id, conversation["id"]),
    )
    _bind_group(conn, profile, group_path, owner_id)
    current = _conversation(conn, conversation["id"])
    if changes:
        _insert_post(
            conn,
            current,
            "agent-chat",
            "Agent Chat",
            "; ".join(changes),
            kind="system",
        )
    return _conversation(conn, conversation["id"])


def _authoritative_group_for_owner(
    conversation: sqlite3.Row, sessions: list[dict[str, Any]]
) -> tuple[str, str, dict[str, Any], list[dict[str, Any]]] | None:
    owner_session = _session_by_id(sessions, conversation["owner_id"])
    owner = (
        owner_session
        if owner_session is not None and owner_session.get("is_project_manager")
        else None
    )
    owner_key = _session_group_key(owner_session) if owner_session is not None else None
    stored_key = (conversation["profile"], conversation["group_path"])
    if owner is not None and owner_key is not None and owner_key != stored_key:
        destination_members = _members_from_snapshot(
            sessions, owner_key[0], owner_key[1]
        )
        destination_moderators = [
            member for member in destination_members if member.get("is_project_manager")
        ]
        if len(destination_moderators) != 1:
            raise ProtocolFault(
                "moderator_ambiguous"
                if len(destination_moderators) > 1
                else "moderator_not_found",
                "the destination AoE group must have exactly one Project Manager",
                details={"profile": owner_key[0], "group_path": owner_key[1]},
            )
        return (
            owner_key[0],
            owner_key[1],
            owner,
            destination_members,
        )
    if owner_session is not None and owner is None and owner_key != stored_key:
        return None
    members = _members_from_snapshot(sessions, stored_key[0], stored_key[1])
    moderators = [member for member in members if member.get("is_project_manager")]
    if len(moderators) > 1:
        raise ProtocolFault(
            "moderator_ambiguous",
            "the AoE group has more than one Project Manager",
            details={"profile": stored_key[0], "group_path": stored_key[1]},
        )
    if len(moderators) == 1:
        if (
            owner_session is None
            and moderators[0].get("id") != conversation["owner_id"]
        ):
            # The immutable owner disappeared and another PM now occupies the
            # same text path. Treat it as path reuse, never as a safe rename.
            return None
        return stored_key[0], stored_key[1], moderators[0], members
    if owner is not None and owner_key is not None:
        return (
            owner_key[0],
            owner_key[1],
            owner,
            _members_from_snapshot(sessions, owner_key[0], owner_key[1]),
        )
    return None


def _actor_is_gm(actor_id: str) -> bool:
    return actor_id == GENERAL_MANAGER_ID


def _conversation(conn: sqlite3.Connection, conversation_id: str) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM conversations WHERE id=?", (conversation_id,)
    ).fetchone()
    if row is None:
        raise ProtocolFault(
            "conversation_not_found",
            "no such conversation",
            details={"conversation_id": conversation_id},
        )
    return row


def _participant(
    conn: sqlite3.Connection, conversation_id: str, session_id: str
) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM participants WHERE conversation_id=? AND session_id=?",
        (conversation_id, session_id),
    ).fetchone()


def _require_participant(
    conn: sqlite3.Connection,
    conversation_id: str,
    session_id: str,
    *,
    active: bool = False,
) -> sqlite3.Row:
    row = _participant(conn, conversation_id, session_id)
    if row is None or (active and row["left_at"] is not None):
        raise ProtocolFault(
            "not_a_participant",
            "the actor is not an active participant in this conversation",
            details={"conversation_id": conversation_id, "session_id": session_id},
        )
    return row


def _check_revision(row: sqlite3.Row, expected: int | None) -> None:
    if expected is not None and row["revision"] != expected:
        raise ProtocolFault(
            "revision_conflict",
            "the conversation changed; refresh and retry",
            retryable=True,
            details={"expected_revision": expected, "actual_revision": row["revision"]},
        )


def _insert_post(
    conn: sqlite3.Connection,
    conversation: sqlite3.Row,
    actor_id: str,
    actor_title: str,
    body: str,
    *,
    kind: str = "message",
    routed_to: str | None = None,
) -> tuple[int, int]:
    if conversation["status"] != "active":
        raise ProtocolFault(
            "conversation_trashed",
            "cannot write to a conversation in Trash",
            details={"conversation_id": conversation["id"]},
        )
    seq = conversation["next_seq"]
    revision = conversation["revision"] + 1
    conn.execute(
        "INSERT INTO posts"
        "(id,conversation_id,seq,from_id,from_title,body,kind,routed_to,created_at) "
        "VALUES(?,?,?,?,?,?,?,?,?)",
        (
            _new_id(),
            conversation["id"],
            seq,
            actor_id,
            actor_title,
            body,
            kind,
            routed_to,
            _now(),
        ),
    )
    conn.execute(
        "UPDATE conversations SET next_seq=?,revision=? WHERE id=?",
        (seq + 1, revision, conversation["id"]),
    )
    if actor_id != GENERAL_MANAGER_ID:
        conn.execute(
            "UPDATE participants SET last_read_seq=MAX(last_read_seq,?) "
            "WHERE conversation_id=? AND session_id=?",
            (seq, conversation["id"], actor_id),
        )
    return seq, revision


def _mutation_result(
    conversation_id: str,
    generation: int | None,
    revision: int,
    post_seq: int | None = None,
) -> dict[str, Any]:
    return {
        "conversation_id": conversation_id,
        "generation": generation,
        "revision": revision,
        "post_seq": post_seq,
    }


def _body(args: argparse.Namespace) -> str:
    value = getattr(args, "body", None) or getattr(args, "message", None)
    if value is None or not value.strip():
        raise ProtocolFault("empty_message", "message body cannot be empty")
    if len(value.encode("utf-8")) > 64 * 1024:
        raise ProtocolFault("message_too_large", "message body exceeds 64 KiB")
    return value


def _key(args: argparse.Namespace) -> str:
    return getattr(args, "idempotency_key", None) or _new_id()


def _active_group(
    conn: sqlite3.Connection, profile: str, group_path: str
) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM conversations WHERE kind='group' AND profile=? "
        "AND group_path=? AND status='active'",
        (profile, group_path),
    ).fetchone()


def _logical_group_owner(
    conn: sqlite3.Connection,
    sessions: list[dict[str, Any]],
    profile: str,
    group_path: str,
    current_moderator_id: str,
) -> str:
    key = (profile, group_path)
    by_key = _active_group(conn, profile, group_path)
    if by_key is not None:
        owner = _session_by_id(sessions, by_key["owner_id"])
        owner_key = _session_group_key(owner) if owner is not None else None
        if by_key["owner_id"] != current_moderator_id and (
            owner is None or owner_key != key
        ):
            return current_moderator_id
        return str(by_key["owner_id"])
    moderator_active = conn.execute(
        "SELECT owner_id FROM conversations WHERE kind='group' AND status='active' "
        "AND owner_id=?",
        (current_moderator_id,),
    ).fetchone()
    if moderator_active is not None:
        return current_moderator_id
    binding = conn.execute(
        "SELECT owner_id FROM group_bindings WHERE profile=? AND group_path=?",
        key,
    ).fetchone()
    if binding is not None:
        bound_owner = _session_by_id(sessions, binding["owner_id"])
        bound_key = _session_group_key(bound_owner) if bound_owner is not None else None
        if binding["owner_id"] == current_moderator_id or bound_key == key:
            return str(binding["owner_id"])
    return current_moderator_id


def _active_for_live_group(
    conn: sqlite3.Connection,
    sessions: list[dict[str, Any]],
    profile: str,
    group_path: str,
    moderator: dict[str, Any],
    members: list[dict[str, Any]],
) -> sqlite3.Row | None:
    row = _active_group(conn, profile, group_path)
    if row is not None and row["owner_id"] != moderator["id"]:
        old_session = _session_by_id(sessions, row["owner_id"])
        old_owner = (
            old_session
            if old_session is not None and old_session.get("is_project_manager")
            else None
        )
        old_key = _session_group_key(old_session) if old_session is not None else None
        if (
            old_owner is not None
            and old_key is not None
            and old_key != (profile, group_path)
        ):
            _reconcile_group(
                conn,
                row,
                old_key[0],
                old_key[1],
                old_owner,
                _members_from_snapshot(sessions, old_key[0], old_key[1]),
            )
            row = None
        elif old_session is None or old_key != (profile, group_path):
            _quarantine_group(conn, row, "group path was reused by a different owner")
            row = None
    if row is not None:
        return _reconcile_group(conn, row, profile, group_path, moderator, members)
    logical_owner = _logical_group_owner(
        conn, sessions, profile, group_path, str(moderator["id"])
    )
    row = conn.execute(
        "SELECT * FROM conversations WHERE kind='group' AND owner_id=? AND status='active'",
        (logical_owner,),
    ).fetchone()
    if row is None:
        return None
    return _reconcile_group(conn, row, profile, group_path, moderator, members)


def _quarantine_group(
    conn: sqlite3.Connection, conversation: sqlite3.Row, reason: str
) -> None:
    if conversation["status"] != "active":
        return
    # Record the reason before freeing the partial active indexes. This history
    # remains bound to its immutable owner and is never exposed to the new PM.
    _, revision = _insert_post(
        conn,
        conversation,
        "agent-chat",
        "Agent Chat",
        reason,
        kind="system",
    )
    now = _now()
    conn.execute(
        "UPDATE conversations SET status='trashed',revision=?,done_at=?,trashed_at=? WHERE id=?",
        (revision + 1, now, now, conversation["id"]),
    )
    _trash_linked_sides_and_wakes(conn, conversation["id"], now)
    conn.execute(
        "DELETE FROM group_bindings WHERE owner_id=? AND profile=? AND group_path=?",
        (conversation["owner_id"], conversation["profile"], conversation["group_path"]),
    )


def _trash_linked_sides_and_wakes(
    conn: sqlite3.Connection, group_conversation_id: str, now: str
) -> None:
    conn.execute(
        "UPDATE conversations SET status='trashed',revision=revision+1,done_at=?,trashed_at=? "
        "WHERE kind='side' AND parent_id=? AND status='active'",
        (now, now, group_conversation_id),
    )
    conn.execute(
        "UPDATE wake_requests SET state='acknowledged',acknowledged_at=?,updated_at=?,"
        "lease_until=NULL,lease_token=NULL,last_error=NULL WHERE state!='acknowledged' AND "
        "(conversation_id=? OR conversation_id IN "
        "(SELECT id FROM conversations WHERE kind='side' AND parent_id=?))",
        (now, now, group_conversation_id, group_conversation_id),
    )


def _cancel_conversation_wakes(
    conn: sqlite3.Connection, conversation_id: str, now: str
) -> None:
    conn.execute(
        "UPDATE wake_requests SET state='acknowledged',acknowledged_at=?,updated_at=?,"
        "lease_until=NULL,lease_token=NULL,last_error=NULL "
        "WHERE conversation_id=? AND state!='acknowledged'",
        (now, now, conversation_id),
    )


def _create_group(
    conn: sqlite3.Connection,
    profile: str,
    group_path: str,
    moderator: dict[str, Any],
    members: list[dict[str, Any]],
    expected_generation: int | None,
    owner_id: str | None = None,
) -> sqlite3.Row:
    owner_id = owner_id or str(moderator["id"])
    moderator_id = str(moderator["id"])
    next_generation = conn.execute(
        "SELECT COALESCE(MAX(generation),0)+1 AS generation FROM conversations "
        "WHERE kind='group' AND owner_id=?",
        (owner_id,),
    ).fetchone()["generation"]
    if expected_generation is not None and expected_generation != next_generation:
        raise ProtocolFault(
            "generation_conflict",
            "the next group generation changed; refresh and retry",
            retryable=True,
            details={
                "expected_generation": expected_generation,
                "actual_generation": next_generation,
            },
        )
    owner_active = conn.execute(
        "SELECT id,profile,group_path FROM conversations WHERE kind='group' "
        "AND owner_id=? AND status='active'",
        (owner_id,),
    ).fetchone()
    if owner_active is not None:
        raise ProtocolFault(
            "owner_active_conflict",
            "this Project Manager already owns another active group conversation",
            details=dict(owner_active),
        )
    conversation_id = _new_id()
    created = _now()
    conn.execute(
        "INSERT INTO conversations"
        "(id,kind,profile,group_path,generation,title,status,owner_id,"
        "current_moderator_id,parent_id,revision,next_seq,created_at) "
        "VALUES(?,?,?,?,?,'Group Chat','active',?,?,NULL,1,1,?)",
        (
            conversation_id,
            "group",
            profile,
            group_path,
            next_generation,
            owner_id,
            moderator_id,
            created,
        ),
    )
    seen: set[str] = set()
    for member in members:
        sid = str(member.get("id") or "")
        if not sid or sid in seen:
            continue
        seen.add(sid)
        title = str(member.get("title") or member.get("session") or sid)
        role = "moderator" if sid == moderator_id else "member"
        conn.execute(
            "INSERT INTO participants"
            "(conversation_id,session_id,session_title,role,last_read_seq,joined_at,muted) "
            "VALUES(?,?,?,?,0,?,0)",
            (conversation_id, sid, title, role, created),
        )
    if moderator_id not in seen:
        conn.execute(
            "INSERT INTO participants"
            "(conversation_id,session_id,session_title,role,last_read_seq,joined_at,muted) "
            "VALUES(?,?,?,'moderator',0,?,0)",
            (
                conversation_id,
                moderator_id,
                str(moderator.get("title") or moderator.get("session") or moderator_id),
                created,
            ),
        )
    _bind_group(conn, profile, group_path, owner_id)
    return _conversation(conn, conversation_id)


def cmd_post(args: argparse.Namespace) -> dict[str, Any]:
    """Post to storage only. This function has no wake-delivery call."""
    conn = _open_db()
    actor_id, actor_title = _identity(args)
    body = _body(args)
    key = _key(args)
    conversation_id = getattr(args, "conversation", None)

    if conversation_id:
        payload = {
            "conversation_id": conversation_id,
            "expected_revision": args.expected_revision,
            "actor_id": actor_id,
            "body": body,
        }
        cached = _cached_idempotent(conn, key, "post_conversation", payload)
        if cached is not None:
            return cached
        initial = _conversation(conn, conversation_id)
        session_snapshot = (
            _sessions(all_profiles=True) if initial["kind"] == "group" else []
        )

        def write_existing() -> dict[str, Any]:
            row = _conversation(conn, conversation_id)
            _check_revision(row, args.expected_revision)
            if row["kind"] == "group":
                context = _authoritative_group_for_owner(row, session_snapshot)
                if context is None:
                    raise ProtocolFault(
                        "group_deleted",
                        "the owning AoE group no longer exists",
                        details={"conversation_id": row["id"]},
                    )
                row = _reconcile_group(conn, row, *context)
            if row["kind"] == "side" or not _actor_is_gm(actor_id):
                _require_participant(conn, row["id"], actor_id, active=True)
            seq, revision = _insert_post(conn, row, actor_id, actor_title, body)
            generation = row["generation"] if row["kind"] == "group" else None
            return _mutation_result(row["id"], generation, revision, seq)

        result, _ = _immediate(
            conn,
            lambda: _idempotent(
                conn, key, "post_conversation", payload, write_existing
            ),
        )
        return result

    if not args.group:
        raise ProtocolFault(
            "group_required", "a group path or --conversation is required"
        )
    profile = _profile(args)
    group_path = args.group
    payload = {
        "profile": profile,
        "group_path": group_path,
        "expected_no_active": bool(args.expected_no_active),
        "expected_generation": args.expected_generation,
        "actor_id": actor_id,
        "body": body,
    }
    cached = _cached_idempotent(conn, key, "post_group", payload)
    if cached is not None:
        return cached
    # Group identity and membership come from one authoritative AoE snapshot.
    # SQLite reconciliation then happens atomically with the post.
    session_snapshot = _sessions(all_profiles=True)
    target_members = _members_from_snapshot(session_snapshot, profile, group_path)
    target_moderators = [
        member for member in target_members if member.get("is_project_manager")
    ]
    if len(target_moderators) != 1:
        raise ProtocolFault(
            "moderator_not_found" if not target_moderators else "moderator_ambiguous",
            "the group must have exactly one Project Manager",
            details={"profile": profile, "group_path": group_path},
        )
    target_moderator = target_moderators[0]

    def write_group() -> dict[str, Any]:
        row = _active_for_live_group(
            conn,
            session_snapshot,
            profile,
            group_path,
            target_moderator,
            target_members,
        )
        if row is not None and args.expected_no_active:
            raise ProtocolFault(
                "active_generation_conflict",
                "an active group generation already exists",
                retryable=True,
                details={"conversation_id": row["id"], "generation": row["generation"]},
            )
        if row is None:
            logical_owner = _logical_group_owner(
                conn,
                session_snapshot,
                profile,
                group_path,
                str(target_moderator["id"]),
            )
            row = _create_group(
                conn,
                profile,
                group_path,
                target_moderator,
                target_members,
                args.expected_generation,
                logical_owner,
            )
        if not _actor_is_gm(actor_id):
            _require_participant(conn, row["id"], actor_id, active=True)
        seq, revision = _insert_post(conn, row, actor_id, actor_title, body)
        return _mutation_result(row["id"], row["generation"], revision, seq)

    result, _ = _immediate(
        conn, lambda: _idempotent(conn, key, "post_group", payload, write_group)
    )
    return result


def cmd_say(args: argparse.Namespace) -> dict[str, Any]:
    # Agent-facing spelling for a participant post. It intentionally delegates
    # to the same storage-only path as `post --conversation`.
    args.conversation = args.conversation_id
    args.group = None
    args.expected_no_active = False
    args.expected_generation = None
    args.expected_revision = getattr(args, "expected_revision", None)
    args.body = args.message
    args.actor = None
    return cmd_post(args)


def _highest_seq(conn: sqlite3.Connection, conversation_id: str) -> int:
    row = conn.execute(
        "SELECT COALESCE(MAX(seq),0) AS highest FROM posts WHERE conversation_id=?",
        (conversation_id,),
    ).fetchone()
    return int(row["highest"])


def _viewer_seen(conn: sqlite3.Connection, conversation_id: str, viewer: str) -> int:
    row = conn.execute(
        "SELECT last_seen_seq FROM viewer_cursors WHERE viewer_id=? AND conversation_id=?",
        (viewer, conversation_id),
    ).fetchone()
    return int(row["last_seen_seq"]) if row is not None else 0


def _conversation_summary(
    conn: sqlite3.Connection, row: sqlite3.Row, viewer: str
) -> dict[str, Any]:
    aggregate = conn.execute(
        "SELECT COALESCE(MAX(seq),0) AS highest,MAX(created_at) AS last_post_at "
        "FROM posts WHERE conversation_id=?",
        (row["id"],),
    ).fetchone()
    count = conn.execute(
        "SELECT COUNT(*) AS count FROM participants WHERE conversation_id=?",
        (row["id"],),
    ).fetchone()["count"]
    highest = int(aggregate["highest"])
    seen = _viewer_seen(conn, row["id"], viewer)
    return {
        "id": row["id"],
        "kind": row["kind"],
        "status": row["status"],
        "title": row["title"],
        "generation": int(row["generation"]) if row["kind"] == "group" else None,
        "parent_id": row["parent_id"],
        "revision": int(row["revision"]),
        "highest_seq": highest,
        "viewer_unread": max(0, highest - seen),
        "participant_count": int(count),
        "last_post_at": aggregate["last_post_at"],
    }


def _participant_summaries(
    conn: sqlite3.Connection,
    conversation_id: str,
    highest: int,
    live_members: list[dict[str, Any]] | None = None,
    live_moderator_id: str | None = None,
) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM participants WHERE conversation_id=? "
        "ORDER BY CASE role WHEN 'moderator' THEN 0 ELSE 1 END, "
        "LOWER(session_title),session_id",
        (conversation_id,),
    ).fetchall()
    if live_members is None:
        return [
            {
                "session_id": row["session_id"],
                "title": row["session_title"],
                "role": row["role"],
                "unread_count": max(0, highest - int(row["last_read_seq"])),
                "departed": row["left_at"] is not None,
            }
            for row in rows
        ]
    live = {str(member["id"]): member for member in live_members if member.get("id")}
    result = []
    stored_ids = set()
    for row in rows:
        sid = row["session_id"]
        stored_ids.add(sid)
        member = live.get(sid)
        result.append(
            {
                "session_id": sid,
                "title": (
                    str(member.get("title") or member.get("session") or sid)
                    if member is not None
                    else row["session_title"]
                ),
                "role": "moderator" if sid == live_moderator_id else "member",
                "unread_count": max(0, highest - int(row["last_read_seq"])),
                "departed": member is None,
            }
        )
    for sid, member in live.items():
        if sid in stored_ids:
            continue
        # A join starts at the current high-water mark, so it has no initial
        # unread count even before the next mutation persists membership.
        result.append(
            {
                "session_id": sid,
                "title": str(member.get("title") or member.get("session") or sid),
                "role": "moderator" if sid == live_moderator_id else "member",
                "unread_count": 0,
                "departed": False,
            }
        )
    result.sort(
        key=lambda item: (
            item["role"] != "moderator",
            item["title"].lower(),
            item["session_id"],
        )
    )
    return result


def _post_wire(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "seq": int(row["seq"]),
        "from_id": row["from_id"],
        "from_title": row["from_title"],
        "body": row["body"],
        "created_at": row["created_at"],
        "kind": row["kind"],
        "routed_to": row["routed_to"],
    }


def _window_recent(
    conn: sqlite3.Connection, conversation_id: str, limit: int = DEFAULT_PAGE_SIZE
) -> dict[str, Any]:
    rows = conn.execute(
        "SELECT * FROM posts WHERE conversation_id=? ORDER BY seq DESC LIMIT ?",
        (conversation_id, limit),
    ).fetchall()
    rows = list(reversed(rows))
    first = int(rows[0]["seq"]) if rows else None
    through = int(rows[-1]["seq"]) if rows else None
    has_more = bool(
        first is not None
        and first > 1
        and conn.execute(
            "SELECT 1 FROM posts WHERE conversation_id=? AND seq<? LIMIT 1",
            (conversation_id, first),
        ).fetchone()
    )
    return {
        "first_seq": first,
        "through_seq": through,
        "has_more_before": has_more,
        "posts": [_post_wire(row) for row in rows],
    }


def _wake_summaries(
    conn: sqlite3.Connection, conversation_id: str
) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM wake_requests WHERE conversation_id=? ORDER BY created_at,id",
        (conversation_id,),
    ).fetchall()
    return [
        {
            "id": row["id"],
            "source_seq": int(row["source_seq"]),
            "requester_id": row["requester_id"],
            "target_id": row["target_id"],
            "state": row["state"],
            "attempt_count": int(row["attempt_count"]),
            "last_error": row["last_error"],
        }
        for row in rows
    ]


def _detail(
    conn: sqlite3.Connection,
    row: sqlite3.Row,
    viewer: str,
    *,
    gm_surface: bool,
    live_members: list[dict[str, Any]] | None = None,
    live_moderator_id: str | None = None,
) -> dict[str, Any]:
    summary = _conversation_summary(conn, row, viewer)
    participants = _participant_summaries(
        conn,
        row["id"],
        summary["highest_seq"],
        live_members,
        live_moderator_id,
    )
    if live_members is not None:
        summary["participant_count"] = len(live_members)
    metadata_only = gm_surface and row["kind"] == "side"
    window = (
        {"first_seq": None, "through_seq": None, "has_more_before": False, "posts": []}
        if metadata_only
        else _window_recent(conn, row["id"])
    )
    return {
        "conversation": summary,
        "body_visibility": "metadata_only" if metadata_only else "full",
        "participants": participants,
        "window": window,
        "wakes": _wake_summaries(conn, row["id"]),
    }


def cmd_panel(args: argparse.Namespace) -> dict[str, Any]:
    """Pure read: this command never creates a room or advances a cursor."""
    conn = _open_db()
    live_moderator = None
    live_members = None
    desired_owner = None
    try:
        snapshot = _sessions(all_profiles=True)
        members = _members_from_snapshot(snapshot, args.profile, args.group)
        moderators = [member for member in members if member.get("is_project_manager")]
        if not members or not moderators:
            raise ProtocolFault(
                "group_not_found",
                "the AoE group no longer exists or has no Project Manager",
                details={"profile": args.profile, "group_path": args.group},
            )
        if len(moderators) != 1:
            raise ProtocolFault(
                "moderator_ambiguous",
                "the AoE group has more than one Project Manager",
            )
        live_moderator = moderators[0]
        live_members = members
        desired_owner = _logical_group_owner(
            conn,
            snapshot,
            args.profile,
            args.group,
            str(live_moderator["id"]),
        )
    except ProtocolFault:
        # Without authoritative ownership, a reused text path cannot be
        # distinguished safely. Fail closed instead of exposing stored bodies.
        raise
    if desired_owner is not None:
        rows = conn.execute(
            "SELECT * FROM conversations WHERE "
            "(kind='group' AND owner_id=?) OR "
            "(kind='side' AND parent_id IN "
            "(SELECT id FROM conversations WHERE kind='group' AND owner_id=?)) "
            "ORDER BY CASE status WHEN 'active' THEN 0 ELSE 1 END, "
            "CASE kind WHEN 'group' THEN 0 ELSE 1 END,generation DESC,created_at DESC,id",
            (desired_owner, desired_owner),
        ).fetchall()
    else:
        rows = []
    summaries = [_conversation_summary(conn, row, args.viewer) for row in rows]
    detail = None
    if args.conversation:
        selected = next((row for row in rows if row["id"] == args.conversation), None)
        if selected is None:
            raise ProtocolFault(
                "conversation_not_found",
                "the selected conversation does not belong to this group",
                details={"conversation_id": args.conversation},
            )
        use_live = selected["kind"] == "group" and selected["status"] == "active"
        detail = _detail(
            conn,
            selected,
            args.viewer,
            gm_surface=True,
            live_members=live_members if use_live else None,
            live_moderator_id=(
                str(live_moderator["id"])
                if use_live and live_moderator is not None
                else None
            ),
        )
    moderator = None
    preferred = next(
        (row for row in rows if row["kind"] == "group" and row["status"] == "active"),
        next((row for row in rows if row["kind"] == "group"), None),
    )
    if live_moderator is not None:
        moderator = {
            "session_id": str(live_moderator["id"]),
            "title": str(
                live_moderator.get("title")
                or live_moderator.get("session")
                or live_moderator["id"]
            ),
            "dormant": str(live_moderator.get("status") or "").lower()
            not in ("running", "waiting", "idle", ""),
        }
    elif preferred is not None and preferred["current_moderator_id"]:
        participant = _participant(
            conn, preferred["id"], preferred["current_moderator_id"]
        )
        moderator = {
            "session_id": preferred["current_moderator_id"],
            "title": (
                participant["session_title"]
                if participant is not None
                else preferred["current_moderator_id"]
            ),
            "dormant": False,
        }
    return {
        "group": {"profile": args.profile, "group_path": args.group},
        "moderator": moderator,
        "conversations": summaries,
        "detail": detail,
        "total_viewer_unread": sum(item["viewer_unread"] for item in summaries),
    }


def cmd_page(args: argparse.Namespace) -> dict[str, Any]:
    conn = _open_db()
    conversation = _conversation(conn, args.conversation_id)
    if conversation["kind"] == "side":
        raise ProtocolFault(
            "side_chat_private",
            "side-chat bodies are private to their two participants",
            details={"conversation_id": conversation["id"]},
        )
    limit = min(max(1, args.limit), MAX_PAGE_SIZE)
    rows = conn.execute(
        "SELECT * FROM posts WHERE conversation_id=? AND seq<? "
        "ORDER BY seq DESC LIMIT ?",
        (conversation["id"], args.before, limit),
    ).fetchall()
    rows = list(reversed(rows))
    first = int(rows[0]["seq"]) if rows else None
    through = int(rows[-1]["seq"]) if rows else None
    has_more = bool(
        first is not None
        and conn.execute(
            "SELECT 1 FROM posts WHERE conversation_id=? AND seq<? LIMIT 1",
            (conversation["id"], first),
        ).fetchone()
    )
    return {
        "conversation_id": conversation["id"],
        "window": {
            "first_seq": first,
            "through_seq": through,
            "has_more_before": has_more,
            "posts": [_post_wire(row) for row in rows],
        },
    }


def cmd_seen(args: argparse.Namespace) -> dict[str, Any]:
    conn = _open_db()

    def write() -> dict[str, Any]:
        conversation = _conversation(conn, args.conversation_id)
        highest = max(0, int(conversation["next_seq"]) - 1)
        through = min(max(0, args.through_seq), highest)
        conn.execute(
            "INSERT INTO viewer_cursors(viewer_id,conversation_id,last_seen_seq,updated_at) "
            "VALUES(?,?,?,?) ON CONFLICT(viewer_id,conversation_id) DO UPDATE SET "
            "last_seen_seq=MAX(viewer_cursors.last_seen_seq,excluded.last_seen_seq),"
            "updated_at=excluded.updated_at",
            (args.viewer, conversation["id"], through, _now()),
        )
        actual = conn.execute(
            "SELECT last_seen_seq FROM viewer_cursors WHERE viewer_id=? AND conversation_id=?",
            (args.viewer, conversation["id"]),
        ).fetchone()["last_seen_seq"]
        return {"conversation_id": conversation["id"], "through_seq": int(actual)}

    return _immediate(conn, write)


def cmd_panels(args: argparse.Namespace) -> dict[str, Any]:
    conn = _open_db()
    snapshot = _sessions(all_profiles=True)
    paths = sorted(
        {
            str(row["group"])
            for row in snapshot
            if row.get("profile") == args.profile
            and row.get("group")
            and row.get("is_project_manager")
        }
    )
    groups = []
    for group_path in paths:
        members = _members_from_snapshot(snapshot, args.profile, group_path)
        moderators = [member for member in members if member.get("is_project_manager")]
        if len(moderators) != 1:
            continue
        owner_id = _logical_group_owner(
            conn, snapshot, args.profile, group_path, str(moderators[0]["id"])
        )
        conversations = conn.execute(
            "SELECT * FROM conversations WHERE (kind='group' AND owner_id=?) OR "
            "(kind='side' AND parent_id IN "
            "(SELECT id FROM conversations WHERE kind='group' AND owner_id=?))",
            (owner_id, owner_id),
        ).fetchall()
        summaries = [
            _conversation_summary(conn, conversation, args.viewer)
            for conversation in conversations
        ]
        active = next(
            (
                item["id"]
                for item in summaries
                if item["kind"] == "group" and item["status"] == "active"
            ),
            None,
        )
        groups.append(
            {
                "group": {"profile": args.profile, "group_path": group_path},
                "active_conversation_id": active,
                "total_viewer_unread": sum(item["viewer_unread"] for item in summaries),
            }
        )
    return {"profile": args.profile, "groups": groups}


def cmd_capabilities(_args: argparse.Namespace) -> dict[str, Any]:
    # Capability negotiation must still reject a database written by a newer
    # store; otherwise an old binary could appear safe before its first write.
    _open_db().close()
    return {
        "wire_version": WIRE_VERSION,
        "schema_version": SCHEMA_VERSION,
        "capabilities": [
            "group_generations",
            "side_chat_privacy",
            "viewer_cursors",
            "paged_history",
            "expected_revision",
            "idempotent_mutations",
            "coalesced_wake_outbox",
            "group_broadcast",
        ],
        "wake_transports": ["terminal"],
        "ordinary_posts_wake": False,
    }


def _authorize_lifecycle(
    conn: sqlite3.Connection, conversation: sqlite3.Row, actor_id: str
) -> None:
    if _actor_is_gm(actor_id):
        return
    if conversation["kind"] == "group":
        if actor_id != conversation["current_moderator_id"]:
            raise ProtocolFault(
                "moderator_required",
                "only the group Project Manager or General Manager may do this",
            )
        return
    _require_participant(conn, conversation["id"], actor_id)


def _source_post(
    conn: sqlite3.Connection, conversation_id: str, source_seq: int
) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM posts WHERE conversation_id=? AND seq=?",
        (conversation_id, source_seq),
    ).fetchone()
    if row is None or row["kind"] != "message":
        raise ProtocolFault(
            "source_post_not_found",
            "the routed source sequence is not a message in this conversation",
            details={"conversation_id": conversation_id, "source_seq": source_seq},
        )
    return row


def _queue_wake(
    conn: sqlite3.Connection,
    conversation_id: str,
    source_seq: int,
    requester_id: str,
    target_id: str,
) -> str:
    participant = _participant(conn, conversation_id, target_id)
    if participant is not None and int(participant["last_read_seq"]) >= source_seq:
        # Keep an audit row even if the target already consumed the source.
        wake_id = _new_id()
        now = _now()
        conn.execute(
            "INSERT INTO wake_requests"
            "(id,conversation_id,source_seq,requester_id,target_id,state,delivery_key,"
            "attempt_count,acknowledged_at,created_at,updated_at) "
            "VALUES(?,?,?,?,?,'acknowledged',?,0,?,?,?)",
            (
                wake_id,
                conversation_id,
                source_seq,
                requester_id,
                target_id,
                _new_id(),
                now,
                now,
                now,
            ),
        )
        return wake_id
    open_row = conn.execute(
        "SELECT * FROM wake_requests WHERE conversation_id=? AND target_id=? "
        "AND state!='acknowledged'",
        (conversation_id, target_id),
    ).fetchone()
    now = _now()
    if open_row is None:
        wake_id = _new_id()
        conn.execute(
            "INSERT INTO wake_requests"
            "(id,conversation_id,source_seq,requester_id,target_id,state,delivery_key,"
            "attempt_count,created_at,updated_at) VALUES(?,?,?,?,?,'pending',?,0,?,?)",
            (
                wake_id,
                conversation_id,
                source_seq,
                requester_id,
                target_id,
                _new_id(),
                now,
                now,
            ),
        )
        return wake_id
    if source_seq > int(open_row["source_seq"]):
        conn.execute(
            "UPDATE wake_requests SET source_seq=?,requester_id=?,state='pending',"
            "delivery_key=?,last_error=NULL,updated_at=? WHERE id=?",
            (source_seq, requester_id, _new_id(), now, open_row["id"]),
        )
    return str(open_row["id"])


def _record_idempotent_wake(
    conn: sqlite3.Connection, idempotency_key: str, wake_id: str, target_id: str
) -> None:
    conn.execute(
        "INSERT INTO idempotency_wakes(idempotency_key,wake_id,target_id) VALUES(?,?,?) "
        "ON CONFLICT(idempotency_key) DO NOTHING",
        (idempotency_key, wake_id, target_id),
    )


def _idempotent_wake(
    conn: sqlite3.Connection, idempotency_key: str
) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT wake_id,target_id FROM idempotency_wakes WHERE idempotency_key=?",
        (idempotency_key,),
    ).fetchone()


def _target_transport(target_id: str) -> dict[str, Any]:
    matches = [
        row for row in _sessions(all_profiles=True) if row.get("id") == target_id
    ]
    if len(matches) != 1:
        raise ProtocolFault(
            "wake_target_not_found",
            "the wake target is not an AoE session",
            retryable=True,
            details={"target_id": target_id},
        )
    target = matches[0]
    view = str(target.get("view") or "terminal").lower()
    tool = str(target.get("tool") or "").lower()
    if view != "terminal" or not (
        tool.startswith("claude") or tool.startswith("codex")
    ):
        raise ProtocolFault(
            "unsupported_wake_transport",
            "v1 wakes support terminal Claude and Codex sessions only",
            details={"target_id": target_id, "view": view, "tool": tool},
        )
    return target


def _wake_control_text(conversation_id: str, source_seq: int) -> str:
    return (
        f"[agent-chat] New panel activity in conversation {conversation_id} through "
        f"sequence {source_seq}. Handle it only in Agent Chat: "
        f"agent-chat read {conversation_id}. Finish this pane with exactly: "
        "Agent Chat message handled."
    )


def _deliver_wake(wake_id: str, *, enabled: bool = True) -> None:
    """Best-effort outbox delivery. Only explicit wake commands call this."""
    if not enabled or os.environ.get("AGENT_CHAT_NO_DOORBELL"):
        return
    conn = _open_db()
    # A higher source may arrive while one delivery is leased. The lease owner
    # finishes its pointer first, then loops once more for the coalesced high
    # water mark. Equal concurrent routes share one delivery.
    for _ in range(4):
        token = _new_id()
        lease_deadline = time.time() + 45.0

        def claim() -> dict[str, Any] | None:
            row = conn.execute(
                "SELECT * FROM wake_requests WHERE id=?", (wake_id,)
            ).fetchone()
            if row is None or row["state"] in ("delivered", "acknowledged"):
                return None
            if (
                row["lease_until"] is not None
                and float(row["lease_until"]) > time.time()
            ):
                return None
            now = _now()
            updated = conn.execute(
                "UPDATE wake_requests SET lease_until=?,lease_token=?,"
                "attempt_count=attempt_count+1,last_attempt_at=?,updated_at=? "
                "WHERE id=? AND state NOT IN ('delivered','acknowledged') "
                "AND (lease_until IS NULL OR lease_until<=?)",
                (lease_deadline, token, now, now, wake_id, time.time()),
            ).rowcount
            if updated != 1:
                return None
            return dict(
                conn.execute(
                    "SELECT * FROM wake_requests WHERE id=?", (wake_id,)
                ).fetchone()
            )

        claimed = _immediate(conn, claim)
        if claimed is None:
            return
        try:
            _target_transport(claimed["target_id"])
            # Intentionally body-free. The pane receives only a durable pointer.
            text = _wake_control_text(
                str(claimed["conversation_id"]), int(claimed["source_seq"])
            )
            proc = subprocess.run(
                [_aoe_bin(), "send", claimed["target_id"], text],
                capture_output=True,
                text=True,
                timeout=20,
            )
            if proc.returncode != 0:
                raise ProtocolFault(
                    "wake_delivery_failed",
                    "AoE rejected the targeted wake",
                    retryable=True,
                    details={"stderr": proc.stderr.strip()},
                )
            delivered = True
            error = None
        except (ProtocolFault, OSError, subprocess.TimeoutExpired) as exc:
            delivered = False
            error = exc.message if isinstance(exc, ProtocolFault) else str(exc)
        now = _now()

        def finish() -> str | None:
            current = conn.execute(
                "SELECT source_seq,state FROM wake_requests WHERE id=? AND lease_token=?",
                (wake_id, token),
            ).fetchone()
            if current is None or current["state"] == "acknowledged":
                return None
            newer_pending = int(current["source_seq"]) > int(claimed["source_seq"])
            state = (
                "pending" if newer_pending else ("delivered" if delivered else "failed")
            )
            conn.execute(
                "UPDATE wake_requests SET state=?,"
                "delivered_at=CASE WHEN ?='delivered' THEN ? ELSE delivered_at END,"
                "last_error=?,lease_until=NULL,lease_token=NULL,updated_at=? "
                "WHERE id=? AND lease_token=?",
                (state, state, now, error, now, wake_id, token),
            )
            return state

        final_state = _immediate(conn, finish)
        if final_state != "pending":
            return


def _broadcast_wake_refs(result: dict[str, Any]) -> list[dict[str, str]]:
    refs = result.get("wake_ids", [])
    if not isinstance(refs, list):
        raise ProtocolFault(
            "invalid_idempotency_result",
            "stored broadcast wake references are invalid",
        )
    parsed = []
    for ref in refs:
        if (
            not isinstance(ref, dict)
            or not ref.get("wake_id")
            or not ref.get("target_id")
        ):
            raise ProtocolFault(
                "invalid_idempotency_result",
                "stored broadcast wake references are invalid",
            )
        parsed.append(
            {"wake_id": str(ref["wake_id"]), "target_id": str(ref["target_id"])}
        )
    parsed.sort(key=lambda ref: (ref["target_id"], ref["wake_id"]))
    return parsed


def _deliver_broadcast_wakes(
    refs: list[dict[str, str]], *, enabled: bool = True
) -> None:
    if not enabled or not refs:
        return
    wake_ids = list(dict.fromkeys(ref["wake_id"] for ref in refs))
    if len(wake_ids) == 1:
        _deliver_wake(wake_ids[0], enabled=True)
        return
    # Delivery is best-effort and each target owns an independent outbox row.
    # Parallel workers keep latency bounded by the slowest target rather than
    # multiplying the transport timeout by group size.
    with ThreadPoolExecutor(
        max_workers=min(8, len(wake_ids)), thread_name_prefix="agent-chat-broadcast"
    ) as executor:
        futures = [
            executor.submit(_deliver_wake, wake_id, enabled=True)
            for wake_id in wake_ids
        ]
        for future in futures:
            try:
                future.result()
            except (OSError, ProtocolFault, sqlite3.Error):
                # The durable outbox remains available to an idempotent retry.
                continue


def cmd_broadcast(args: argparse.Namespace) -> dict[str, Any]:
    """Store one group message, then explicitly wake every eligible participant."""
    conn = _open_db()
    actor_id, actor_title = _identity(args)
    profile = _profile(args)
    body = _body(args)
    key = _key(args)
    payload = {
        "profile": profile,
        "group_path": args.group,
        "actor_id": actor_id,
        "body": body,
    }
    cached = _cached_idempotent(conn, key, "broadcast", payload)
    if cached is not None:
        _deliver_broadcast_wakes(
            _broadcast_wake_refs(cached), enabled=not args.no_doorbell
        )
        return cached

    session_snapshot = _sessions(all_profiles=True)
    members = _members_from_snapshot(session_snapshot, profile, args.group)
    moderators = [member for member in members if member.get("is_project_manager")]
    if len(moderators) != 1:
        raise ProtocolFault(
            "moderator_not_found" if not moderators else "moderator_ambiguous",
            "the group must have exactly one Project Manager",
            details={"profile": profile, "group_path": args.group},
        )

    def write() -> dict[str, Any]:
        conversation = _active_for_live_group(
            conn,
            session_snapshot,
            profile,
            args.group,
            moderators[0],
            members,
        )
        if conversation is None:
            owner_id = _logical_group_owner(
                conn,
                session_snapshot,
                profile,
                args.group,
                str(moderators[0]["id"]),
            )
            conversation = _create_group(
                conn,
                profile,
                args.group,
                moderators[0],
                members,
                None,
                owner_id,
            )
        if not _actor_is_gm(actor_id):
            _require_participant(conn, conversation["id"], actor_id, active=True)
        seq, revision = _insert_post(conn, conversation, actor_id, actor_title, body)
        rows = conn.execute(
            "SELECT session_id FROM participants WHERE conversation_id=? "
            "AND left_at IS NULL AND muted=0 ORDER BY session_id",
            (conversation["id"],),
        ).fetchall()
        wake_refs = []
        for row in rows:
            target_id = str(row["session_id"])
            if not _actor_is_gm(actor_id) and target_id == actor_id:
                continue
            wake_refs.append(
                {
                    "target_id": target_id,
                    "wake_id": _queue_wake(
                        conn, conversation["id"], seq, actor_id, target_id
                    ),
                }
            )
        result = _mutation_result(
            conversation["id"], conversation["generation"], revision, seq
        )
        result["wake_ids"] = wake_refs
        return result

    result, _ = _immediate(
        conn, lambda: _idempotent(conn, key, "broadcast", payload, write)
    )
    refs = _broadcast_wake_refs(result)
    _deliver_broadcast_wakes(refs, enabled=not args.no_doorbell)
    return result


def cmd_route(args: argparse.Namespace) -> dict[str, Any]:
    conn = _open_db()
    actor_id, actor_title = _identity(args)
    key = _key(args)
    payload = {
        "conversation_id": args.conversation_id,
        "target_id": args.target_id,
        "source_seq": args.sequence,
        "expected_revision": args.expected_revision,
        "actor_id": actor_id,
    }
    cached = _cached_idempotent(conn, key, "route", payload)
    if cached is not None:
        wake = _idempotent_wake(conn, key)
        if wake is not None:
            _deliver_wake(wake["wake_id"], enabled=not args.no_doorbell)
        return cached
    initial = _conversation(conn, args.conversation_id)
    session_snapshot = (
        _sessions(all_profiles=True) if initial["kind"] == "group" else []
    )
    wake_holder: dict[str, str] = {}

    def write() -> dict[str, Any]:
        conversation = _conversation(conn, args.conversation_id)
        if conversation["kind"] != "group":
            raise ProtocolFault(
                "group_conversation_required", "route requires a group conversation"
            )
        _check_revision(conversation, args.expected_revision)
        context = _authoritative_group_for_owner(conversation, session_snapshot)
        if context is None:
            raise ProtocolFault(
                "group_deleted", "the owning AoE group no longer exists"
            )
        conversation = _reconcile_group(conn, conversation, *context)
        _authorize_lifecycle(conn, conversation, actor_id)
        target = _require_participant(
            conn, conversation["id"], args.target_id, active=True
        )
        _source_post(conn, conversation["id"], args.sequence)
        audit = f"Routed sequence {args.sequence} to {target['session_title']}"
        audit_seq, revision = _insert_post(
            conn,
            conversation,
            actor_id,
            actor_title,
            audit,
            kind="route",
            routed_to=args.target_id,
        )
        wake_holder["id"] = _queue_wake(
            conn, conversation["id"], args.sequence, actor_id, args.target_id
        )
        _record_idempotent_wake(conn, key, wake_holder["id"], args.target_id)
        return _mutation_result(
            conversation["id"], conversation["generation"], revision, audit_seq
        )

    result, replay = _immediate(
        conn, lambda: _idempotent(conn, key, "route", payload, write)
    )
    if replay:
        row = _idempotent_wake(conn, key)
        if row is not None:
            wake_holder["id"] = row["wake_id"]
    if "id" in wake_holder:
        _deliver_wake(wake_holder["id"], enabled=not args.no_doorbell)
    return result


def cmd_done(args: argparse.Namespace) -> dict[str, Any]:
    conn = _open_db()
    actor_id, _ = _identity(args)
    key = _key(args)
    payload = {
        "conversation_id": args.conversation_id,
        "expected_revision": args.expected_revision,
        "actor_id": actor_id,
    }
    cached = _cached_idempotent(conn, key, "done", payload)
    if cached is not None:
        return cached
    initial = _conversation(conn, args.conversation_id)
    session_snapshot = (
        _sessions(all_profiles=True) if initial["kind"] == "group" else []
    )

    def write() -> dict[str, Any]:
        conversation = _conversation(conn, args.conversation_id)
        _check_revision(conversation, args.expected_revision)
        if conversation["kind"] == "group":
            context = _authoritative_group_for_owner(conversation, session_snapshot)
            if context is not None:
                conversation = _reconcile_group(conn, conversation, *context)
            elif not _actor_is_gm(actor_id):
                raise ProtocolFault(
                    "group_deleted", "the owning AoE group no longer exists"
                )
        _authorize_lifecycle(conn, conversation, actor_id)
        if conversation["status"] != "active":
            raise ProtocolFault(
                "conversation_already_trashed", "conversation is already in Trash"
            )
        revision = int(conversation["revision"]) + 1
        now = _now()
        conn.execute(
            "UPDATE conversations SET status='trashed',revision=?,done_at=?,trashed_at=? "
            "WHERE id=?",
            (revision, now, now, conversation["id"]),
        )
        if conversation["kind"] == "group":
            _trash_linked_sides_and_wakes(conn, conversation["id"], now)
        else:
            _cancel_conversation_wakes(conn, conversation["id"], now)
        generation = (
            conversation["generation"] if conversation["kind"] == "group" else None
        )
        return _mutation_result(conversation["id"], generation, revision)

    result, _ = _immediate(conn, lambda: _idempotent(conn, key, "done", payload, write))
    return result


def cmd_restore(args: argparse.Namespace) -> dict[str, Any]:
    conn = _open_db()
    actor_id, _ = _identity(args)
    key = _key(args)
    payload = {
        "conversation_id": args.conversation_id,
        "expected_revision": args.expected_revision,
        "actor_id": actor_id,
    }
    cached = _cached_idempotent(conn, key, "restore", payload)
    if cached is not None:
        return cached
    initial = _conversation(conn, args.conversation_id)
    session_snapshot = (
        _sessions(all_profiles=True) if initial["kind"] == "group" else []
    )

    def write() -> dict[str, Any]:
        conversation = _conversation(conn, args.conversation_id)
        _check_revision(conversation, args.expected_revision)
        if conversation["status"] != "trashed":
            raise ProtocolFault(
                "conversation_not_trashed", "conversation is not in Trash"
            )
        if conversation["kind"] == "group":
            context = _authoritative_group_for_owner(conversation, session_snapshot)
            if context is None:
                raise ProtocolFault(
                    "group_deleted",
                    "cannot restore until the owning AoE group exists again",
                )
            desired_profile, desired_path, desired_moderator, _ = context
            if not _actor_is_gm(actor_id) and actor_id != str(desired_moderator["id"]):
                raise ProtocolFault(
                    "moderator_required",
                    "only the current Project Manager or General Manager may restore this group",
                )
            conflict = conn.execute(
                "SELECT id,generation FROM conversations WHERE kind='group' AND status='active' "
                "AND (owner_id=? OR (profile=? AND group_path=?)) LIMIT 1",
                (
                    conversation["owner_id"],
                    desired_profile,
                    desired_path,
                ),
            ).fetchone()
            if conflict is not None:
                raise ProtocolFault(
                    "active_generation_conflict",
                    "a newer group generation is active",
                    details={
                        "active_conversation_id": conflict["id"],
                        "active_generation": conflict["generation"],
                    },
                )
        else:
            _authorize_lifecycle(conn, conversation, actor_id)
        revision = int(conversation["revision"]) + 1
        conn.execute(
            "UPDATE conversations SET status='active',revision=?,restored_at=?,"
            "done_at=NULL,trashed_at=NULL WHERE id=?",
            (revision, _now(), conversation["id"]),
        )
        if conversation["kind"] == "group":
            # Move every historical generation with its immutable owner before
            # reconciling the restored active generation.
            conn.execute(
                "UPDATE conversations SET profile=?,group_path=? WHERE kind='group' AND owner_id=?",
                (desired_profile, desired_path, conversation["owner_id"]),
            )
            conn.execute(
                "UPDATE conversations SET profile=?,group_path=? WHERE kind='side' AND parent_id IN "
                "(SELECT id FROM conversations WHERE kind='group' AND owner_id=?)",
                (desired_profile, desired_path, conversation["owner_id"]),
            )
            restored = _conversation(conn, conversation["id"])
            restored = _reconcile_group(conn, restored, *context)
            revision = int(restored["revision"])
        generation = (
            conversation["generation"] if conversation["kind"] == "group" else None
        )
        return _mutation_result(conversation["id"], generation, revision)

    result, _ = _immediate(
        conn, lambda: _idempotent(conn, key, "restore", payload, write)
    )
    return result


def _resolve_session(spec: str, profile: str | None = None) -> dict[str, Any]:
    rows = _sessions(profile, all_profiles=profile is None)
    if ":" in spec:
        sid, _, title = spec.partition(":")
        matches = [row for row in rows if row.get("id") == sid]
        if matches:
            return matches[0]
        return {"id": sid, "title": title or sid, "profile": profile or ""}
    predicates = (
        lambda row: str(row.get("title", "")).lower() == spec.lower(),
        lambda row: row.get("id") == spec,
        lambda row: str(row.get("id", "")).startswith(spec),
        lambda row: spec.lower() in str(row.get("title", "")).lower(),
    )
    for predicate in predicates:
        matches = [row for row in rows if predicate(row)]
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            raise ProtocolFault(
                "recipient_ambiguous",
                "more than one AoE session matches the recipient",
                details={"recipient": spec},
            )
    raise ProtocolFault(
        "recipient_not_found",
        "no AoE session matches the recipient",
        details={"recipient": spec},
    )


def cmd_sidechat(args: argparse.Namespace) -> dict[str, Any]:
    conn = _open_db()
    actor_id, actor_title = _identity(args)
    if _actor_is_gm(actor_id):
        raise ProtocolFault(
            "agent_identity_required", "General Manager cannot join a side chat"
        )
    profile = _profile(args)
    current = _current() if not args.group else {}
    group_path = args.group or str(current.get("group") or "")
    if not group_path:
        raise ProtocolFault(
            "group_required", "--group is required outside a grouped AoE session"
        )
    body = args.opening
    if not body.strip():
        raise ProtocolFault("empty_message", "opening message cannot be empty")
    key = _key(args)
    payload = {
        "profile": profile,
        "group_path": group_path,
        "actor_id": actor_id,
        "other_session": args.other_session,
        "opening": body,
    }
    cached = _cached_idempotent(conn, key, "sidechat", payload)
    if cached is not None:
        wake = _idempotent_wake(conn, key)
        if wake is not None:
            _deliver_wake(wake["wake_id"], enabled=not args.no_doorbell)
        return cached
    other = _resolve_session(args.other_session, profile)
    if other["id"] == actor_id:
        raise ProtocolFault(
            "invalid_participants", "a side chat requires two different agents"
        )
    session_snapshot = _sessions(all_profiles=True)
    group_members = _members_from_snapshot(session_snapshot, profile, group_path)
    moderators = [
        member for member in group_members if member.get("is_project_manager")
    ]
    if len(moderators) != 1:
        raise ProtocolFault(
            "moderator_not_found", "the group has no unique Project Manager"
        )
    moderator = moderators[0]
    wake_holder: dict[str, str] = {}

    def write() -> dict[str, Any]:
        parent = _active_for_live_group(
            conn,
            session_snapshot,
            profile,
            group_path,
            moderator,
            group_members,
        )
        if parent is None:
            raise ProtocolFault(
                "active_group_required",
                "start the group conversation before starting a side chat",
            )
        actor_member = _require_participant(conn, parent["id"], actor_id, active=True)
        other_member = _require_participant(
            conn, parent["id"], str(other["id"]), active=True
        )
        conversation_id = _new_id()
        created = _now()
        title = f"{actor_member['session_title']} and {other_member['session_title']}"
        conn.execute(
            "INSERT INTO conversations"
            "(id,kind,profile,group_path,generation,title,status,owner_id,"
            "current_moderator_id,parent_id,revision,next_seq,created_at) "
            "VALUES(?,?,?,?,1,?,'active',?,NULL,?,1,1,?)",
            (
                conversation_id,
                "side",
                profile,
                group_path,
                title,
                actor_id,
                parent["id"],
                created,
            ),
        )
        for member in (actor_member, other_member):
            conn.execute(
                "INSERT INTO participants"
                "(conversation_id,session_id,session_title,role,last_read_seq,joined_at,muted) "
                "VALUES(?,?,?,'member',0,?,0)",
                (
                    conversation_id,
                    member["session_id"],
                    member["session_title"],
                    created,
                ),
            )
        side = _conversation(conn, conversation_id)
        seq, revision = _insert_post(conn, side, actor_id, actor_title, body)
        wake_holder["id"] = _queue_wake(
            conn, conversation_id, seq, actor_id, str(other["id"])
        )
        _record_idempotent_wake(conn, key, wake_holder["id"], str(other["id"]))
        return _mutation_result(conversation_id, None, revision, seq)

    result, replay = _immediate(
        conn, lambda: _idempotent(conn, key, "sidechat", payload, write)
    )
    if replay:
        row = _idempotent_wake(conn, key)
        if row is not None:
            wake_holder["id"] = row["wake_id"]
    if "id" in wake_holder:
        _deliver_wake(wake_holder["id"], enabled=not args.no_doorbell)
    return result


def _read_conversation(
    conn: sqlite3.Connection,
    conversation_id: str,
    actor_id: str,
    since: int | None,
    limit: int,
) -> dict[str, Any]:
    conversation = _conversation(conn, conversation_id)
    participant = _require_participant(conn, conversation_id, actor_id)
    after = int(participant["last_read_seq"]) if since is None else max(0, since)
    capped = min(max(1, limit), MAX_PAGE_SIZE)
    rows = conn.execute(
        "SELECT * FROM posts WHERE conversation_id=? AND seq>? ORDER BY seq LIMIT ?",
        (conversation_id, after, capped + 1),
    ).fetchall()
    has_more = len(rows) > capped
    rows = rows[:capped]
    through = int(rows[-1]["seq"]) if rows else int(participant["last_read_seq"])
    if rows:
        conn.execute(
            "UPDATE participants SET last_read_seq=MAX(last_read_seq,?) "
            "WHERE conversation_id=? AND session_id=?",
            (through, conversation_id, actor_id),
        )
        conn.execute(
            "UPDATE wake_requests SET state='acknowledged',acknowledged_at=?,updated_at=?,"
            "lease_until=NULL,lease_token=NULL "
            "WHERE conversation_id=? AND target_id=? AND state!='acknowledged' "
            "AND source_seq<=?",
            (_now(), _now(), conversation_id, actor_id, through),
        )
    return {
        "conversation_id": conversation_id,
        "kind": conversation["kind"],
        "posts": [_post_wire(row) for row in rows],
        "through_seq": through,
        "has_more": has_more,
    }


def cmd_read(args: argparse.Namespace) -> dict[str, Any]:
    conn = _open_db()
    actor_id, _ = _identity(args)
    initial = _conversation(conn, args.conversation_id)
    session_snapshot = (
        _sessions(all_profiles=True) if initial["kind"] == "group" else []
    )

    def read() -> dict[str, Any]:
        conversation = _conversation(conn, args.conversation_id)
        if conversation["kind"] == "group":
            context = _authoritative_group_for_owner(conversation, session_snapshot)
            if context is None:
                raise ProtocolFault(
                    "group_deleted", "the owning AoE group no longer exists"
                )
            _reconcile_group(conn, conversation, *context)
        return _read_conversation(
            conn, args.conversation_id, actor_id, args.since, args.limit
        )

    return _immediate(conn, read)


def cmd_room(args: argparse.Namespace) -> dict[str, Any]:
    conn = _open_db()
    actor_id, _ = _identity(args)
    profile = _profile(args)
    session_snapshot = _sessions(all_profiles=True)
    members = _members_from_snapshot(session_snapshot, profile, args.group)
    moderators = [member for member in members if member.get("is_project_manager")]
    if len(moderators) != 1:
        raise ProtocolFault(
            "moderator_not_found", "the group has no unique Project Manager"
        )

    def read() -> dict[str, Any]:
        conversation = _active_for_live_group(
            conn, session_snapshot, profile, args.group, moderators[0], members
        )
        if conversation is None:
            raise ProtocolFault(
                "active_group_not_found", "the group has no active conversation"
            )
        return _read_conversation(
            conn, conversation["id"], actor_id, args.since, args.limit
        )

    return _immediate(conn, read)


def cmd_rooms(args: argparse.Namespace) -> dict[str, Any]:
    conn = _open_db()
    actor_id, _ = _identity(args)
    rows = conn.execute(
        "SELECT c.*,p.last_read_seq FROM conversations c JOIN participants p "
        "ON p.conversation_id=c.id WHERE p.session_id=? "
        "ORDER BY CASE c.status WHEN 'active' THEN 0 ELSE 1 END,c.created_at DESC,c.id",
        (actor_id,),
    ).fetchall()
    rooms = []
    for row in rows:
        highest = _highest_seq(conn, row["id"])
        rooms.append(
            {
                "conversation_id": row["id"],
                "kind": row["kind"],
                "profile": row["profile"],
                "group_path": row["group_path"],
                "title": row["title"],
                "status": row["status"],
                "generation": row["generation"] if row["kind"] == "group" else None,
                "highest_seq": highest,
                "unread_count": max(0, highest - int(row["last_read_seq"])),
            }
        )
    return {"rooms": rooms}


def _explicit_attention(args: argparse.Namespace, mode: str) -> dict[str, Any]:
    conn = _open_db()
    actor_id, actor_title = _identity(args)
    key = _key(args)
    wake_holder: dict[str, str] = {}
    payload = {
        "mode": mode,
        "conversation_id": args.conversation_id,
        "source_seq": args.sequence,
        "expected_revision": args.expected_revision,
        "actor_id": actor_id,
    }
    cached = _cached_idempotent(conn, key, mode, payload)
    if cached is not None:
        wake = _idempotent_wake(conn, key)
        if wake is not None:
            _deliver_wake(wake["wake_id"], enabled=not args.no_doorbell)
        return cached
    initial = _conversation(conn, args.conversation_id)
    session_snapshot = (
        _sessions(all_profiles=True) if initial["kind"] == "group" else []
    )

    def write() -> dict[str, Any]:
        conversation = _conversation(conn, args.conversation_id)
        _check_revision(conversation, args.expected_revision)
        if conversation["kind"] == "group":
            context = _authoritative_group_for_owner(conversation, session_snapshot)
            if context is None:
                raise ProtocolFault(
                    "group_deleted", "the owning AoE group no longer exists"
                )
            conversation = _reconcile_group(conn, conversation, *context)
        _require_participant(conn, conversation["id"], actor_id, active=True)
        if mode == "notify_moderator":
            if conversation["kind"] != "group":
                raise ProtocolFault(
                    "group_conversation_required", "notify-moderator requires a group"
                )
            target_id = conversation["current_moderator_id"]
            if not target_id:
                raise ProtocolFault(
                    "moderator_not_found", "the group has no current moderator"
                )
            label = "Requested moderator attention"
        else:
            if conversation["kind"] != "side":
                raise ProtocolFault(
                    "side_conversation_required", "nudge requires a side chat"
                )
            other = conn.execute(
                "SELECT session_id FROM participants WHERE conversation_id=? AND session_id!=? "
                "ORDER BY session_id LIMIT 1",
                (conversation["id"], actor_id),
            ).fetchone()
            if other is None:
                raise ProtocolFault(
                    "invalid_participants", "the side chat has no other participant"
                )
            target_id = other["session_id"]
            label = "Nudged side-chat participant"
        source_seq = args.sequence
        if source_seq is None:
            source = conn.execute(
                "SELECT seq FROM posts WHERE conversation_id=? AND kind='message' "
                "ORDER BY seq DESC LIMIT 1",
                (conversation["id"],),
            ).fetchone()
            if source is None:
                raise ProtocolFault(
                    "source_post_not_found", "there is no message to route"
                )
            source_seq = int(source["seq"])
        _source_post(conn, conversation["id"], source_seq)
        audit_seq, revision = _insert_post(
            conn,
            conversation,
            actor_id,
            actor_title,
            f"{label} for sequence {source_seq}",
            kind="route",
            routed_to=target_id,
        )
        wake_holder["id"] = _queue_wake(
            conn, conversation["id"], source_seq, actor_id, target_id
        )
        wake_holder["target"] = target_id
        _record_idempotent_wake(conn, key, wake_holder["id"], target_id)
        generation = (
            conversation["generation"] if conversation["kind"] == "group" else None
        )
        return _mutation_result(conversation["id"], generation, revision, audit_seq)

    result, replay = _immediate(
        conn, lambda: _idempotent(conn, key, mode, payload, write)
    )
    if replay:
        row = _idempotent_wake(conn, key)
        if row is not None:
            wake_holder["id"] = row["wake_id"]
            wake_holder["target"] = row["target_id"]
    if "id" in wake_holder:
        _deliver_wake(wake_holder["id"], enabled=not args.no_doorbell)
    return result


def cmd_notify_moderator(args: argparse.Namespace) -> dict[str, Any]:
    return _explicit_attention(args, "notify_moderator")


def cmd_nudge(args: argparse.Namespace) -> dict[str, Any]:
    return _explicit_attention(args, "nudge")


def _wire_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--json", action="store_true", help="emit the v1 JSON envelope")


def _mutation_options(
    parser: argparse.ArgumentParser,
    *,
    actor: bool = True,
    revision: bool = True,
    doorbell: bool = False,
) -> None:
    if revision:
        parser.add_argument("--expected-revision", type=int)
    parser.add_argument("--idempotency-key")
    if actor:
        parser.add_argument("--actor", choices=("general-manager",))
    if doorbell:
        parser.add_argument("--no-doorbell", action="store_true")
    _wire_options(parser)


def _v2(
    parser: argparse.ArgumentParser,
    handler: Callable[[argparse.Namespace], dict[str, Any]],
) -> None:
    parser.set_defaults(func=dispatch, v2_handler=handler)


def add_parsers(sub: argparse._SubParsersAction) -> None:  # type: ignore[type-arg]
    capabilities = sub.add_parser(
        "capabilities", help="report Agent Chat protocol capabilities"
    )
    _wire_options(capabilities)
    _v2(capabilities, cmd_capabilities)

    panel = sub.add_parser("panel", help="read one group panel without marking it seen")
    panel.add_argument("group")
    panel.add_argument("--profile", required=True)
    panel.add_argument("--viewer", required=True)
    panel.add_argument("--conversation")
    _wire_options(panel)
    _v2(panel, cmd_panel)

    panels = sub.add_parser("panels", help="read lightweight group unread counts")
    panels.add_argument("--profile", required=True)
    panels.add_argument("--viewer", required=True)
    _wire_options(panels)
    _v2(panels, cmd_panels)

    page = sub.add_parser("page", help="page backward through a group conversation")
    page.add_argument("conversation_id")
    page.add_argument("--before", type=int, required=True)
    page.add_argument("--limit", type=int, default=DEFAULT_PAGE_SIZE)
    _wire_options(page)
    _v2(page, cmd_page)

    seen = sub.add_parser("seen", help="advance a human viewer cursor")
    seen.add_argument("conversation_id")
    seen.add_argument("through_seq", type=int)
    seen.add_argument("--viewer", required=True)
    _wire_options(seen)
    _v2(seen, cmd_seen)

    post = sub.add_parser("post", help="post without waking any agent")
    post.add_argument("group", nargs="?")
    post.add_argument("message", nargs="?")
    post.add_argument("--conversation")
    post.add_argument("--profile")
    post.add_argument("--body")
    post.add_argument("--expected-no-active", action="store_true")
    post.add_argument("--expected-generation", type=int)
    _mutation_options(post)
    _v2(post, cmd_post)

    say = sub.add_parser("say", help="post to a joined conversation without a wake")
    say.add_argument("conversation_id")
    say.add_argument("message")
    say.add_argument("--expected-revision", type=int)
    say.add_argument("--idempotency-key")
    _wire_options(say)
    _v2(say, cmd_say)

    broadcast = sub.add_parser(
        "broadcast", help="store one group message and explicitly wake all participants"
    )
    broadcast.add_argument("group")
    broadcast.add_argument("message")
    broadcast.add_argument("--profile")
    _mutation_options(broadcast, revision=False, doorbell=True)
    _v2(broadcast, cmd_broadcast)

    route = sub.add_parser("route", help="explicitly wake one group participant")
    route.add_argument("conversation_id")
    route.add_argument("target_id")
    route.add_argument("--sequence", type=int, required=True)
    _mutation_options(route, doorbell=True)
    _v2(route, cmd_route)

    done = sub.add_parser(
        "done", aliases=["trash"], help="move a conversation to Trash"
    )
    done.add_argument("conversation_id")
    _mutation_options(done)
    _v2(done, cmd_done)

    restore = sub.add_parser("restore", help="restore a trashed conversation")
    restore.add_argument("conversation_id")
    _mutation_options(restore)
    _v2(restore, cmd_restore)

    read = sub.add_parser(
        "read", help="read a joined conversation and advance agent cursor"
    )
    read.add_argument("conversation_id")
    read.add_argument("--since", type=int)
    read.add_argument("--limit", type=int, default=MAX_PAGE_SIZE)
    _wire_options(read)
    _v2(read, cmd_read)

    room = sub.add_parser("room", help="read the active room for a group")
    room.add_argument("group")
    room.add_argument("--profile")
    room.add_argument("--since", type=int)
    room.add_argument("--limit", type=int, default=MAX_PAGE_SIZE)
    _wire_options(room)
    _v2(room, cmd_room)

    rooms = sub.add_parser(
        "rooms", help="list conversations joined by the current agent"
    )
    _wire_options(rooms)
    _v2(rooms, cmd_rooms)

    sidechat = sub.add_parser("sidechat", help="start a private two-agent side chat")
    sidechat.add_argument("other_session")
    sidechat.add_argument("opening")
    sidechat.add_argument("--group")
    sidechat.add_argument("--profile")
    _mutation_options(sidechat, actor=False, revision=False, doorbell=True)
    _v2(sidechat, cmd_sidechat)

    notify = sub.add_parser(
        "notify-moderator", help="explicitly request the group moderator's attention"
    )
    notify.add_argument("conversation_id")
    notify.add_argument("sequence", nargs="?", type=int)
    _mutation_options(notify, actor=False, doorbell=True)
    _v2(notify, cmd_notify_moderator)

    nudge = sub.add_parser(
        "nudge", help="explicitly wake the other side-chat participant"
    )
    nudge.add_argument("conversation_id")
    nudge.add_argument("sequence", nargs="?", type=int)
    _mutation_options(nudge, actor=False, doorbell=True)
    _v2(nudge, cmd_nudge)


def _success(data: dict[str, Any], request_id: str) -> dict[str, Any]:
    return {"v": WIRE_VERSION, "request_id": request_id, "status": "ok", "data": data}


def _error(error: ProtocolFault, request_id: str) -> dict[str, Any]:
    return {
        "v": WIRE_VERSION,
        "request_id": request_id,
        "status": "error",
        "error": error.wire(),
    }


def dispatch(args: argparse.Namespace) -> int:
    request_id = _new_id()
    try:
        data = args.v2_handler(args)
    except ProtocolFault as error:
        if args.json:
            print(json.dumps(_error(error, request_id), separators=(",", ":")))
        else:
            print(f"agent-chat: {error.code}: {error.message}", file=sys.stderr)
        return 1
    except sqlite3.Error as error:
        fault = ProtocolFault(
            "store_error",
            "Agent Chat storage failed",
            retryable=True,
            details={"error": str(error)},
        )
        if args.json:
            print(json.dumps(_error(fault, request_id), separators=(",", ":")))
        else:
            print(f"agent-chat: store_error: {error}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(_success(data, request_id), separators=(",", ":")))
    else:
        print(json.dumps(data, indent=2, sort_keys=True))
    return 0
