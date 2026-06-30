# Agent Chat v2 storage and CLI contract

Date: 2026-06-30  
Status: approved v1 implementation contract

## Boundary and compatibility

Agent Chat is the only owner of `mail.db`. AoE invokes this CLI and consumes a
versioned JSON response; AoE never opens the database. The v2 tables are
additive. The v1 `messages` table and the `ask`, `reply`, `inbox`, `replies`,
`thread`, and `whoami` commands remain unchanged for one compatibility cycle.
Legacy rows are not imported into a group panel because they have no stable
profile and group identity.

Every v2 connection installs the additive schema idempotently. The schema version
in `meta` is separate from the JSON wire major.
An older marker advances only after its additive setup completes. A marker
newer than this CLI supports is never decremented or opened optimistically; the
command returns `unsupported_schema_version` and asks the caller to upgrade.

## Non-negotiable wake rule

Posting and giving an agent a turn are different operations.

```text
post / say -> durable storage only
explicit route / notify-moderator / sidechat start / nudge -> wake outbox
```

An ordinary post, including one containing plain `@name` text, never invokes
`aoe send`. A targeted wake contains only an immutable conversation ID, a
source sequence, and an `agent-chat read` command. It never quotes or previews
the message body.

Only the Project Manager or General Manager may route a group message directly
to a worker. The General Manager may also explicitly route the first stored
group message to a dormant Project Manager; the normal AoE terminal send path
can revive and permanently activate that PM. A worker can explicitly notify
the moderator. Either side-chat participant can explicitly nudge the other
participant. Starting a side chat has one explicit opening wake.

The first release dispatches wakes only to terminal Claude and Codex sessions.
Structured ACP and sandbox transports are follow-up work. Delivery uses a
coalescing transactional outbox and a lease. The lease suppresses concurrent
duplicate sends, and the send subprocess is bounded below the lease duration.
Because the current `aoe send` command has no transport idempotency token, a
crash after AoE accepts a send but before Agent Chat records delivery can still
produce a duplicate on a later retry. Delivery is best-effort, not externally
exactly-once or guaranteed at-least-once. There is no automatic outbox drainer
in this release. Retrying the same mutation key re-drives its exact pending or
failed wake when no live lease owns it; a live lease can defer that retry, and
delivery is not eventually guaranteed unless a caller retries after a failure
or expired lease. Delivery acceptance is not read acknowledgement; the target
participant cursor acknowledges a wake.
A read can acknowledge while a leased send is already in flight; it clears the
lease metadata, but the already-started control message may still arrive.

## Conversation model

A group is identified by `(profile, group_path)` at the AoE boundary and by an
immutable owner session ID in history. The current profile/path can move when a
group is renamed or moved between profiles. The immutable owner prevents a
reused path from exposing or appending to another group's history.

The store contains:

- `conversations`: stable ID, `group|side`, current group key, immutable owner,
  current moderator, generation, `active|trashed`, revision, next sequence,
  parent side-chat link, and lifecycle timestamps;
- `participants`: title snapshot, `moderator|member`, join/leave state, mute,
  and the agent-owned read cursor;
- `posts`: immutable per-conversation sequence, sender snapshot,
  `message|route|system`, optional route target, body, and timestamp;
- `viewer_cursors`: a separate monotonic cursor for a human TUI viewer;
- `wake_requests`: one coalesced open outbox row per conversation and target,
  including lease, attempts, delivery state, and acknowledgement;
- `idempotency_results`: committed mutation responses keyed by request key;
- `group_bindings`: the current group key to immutable owner binding;
- `meta`: the internal schema version.

The default dedicated Agent Chat data directory is created and repaired to
mode `0700`; `mail.db` and existing SQLite WAL/SHM sidecars are created and
repaired to `0600`. `AGENT_CHAT_DB` may point into a custom parent directory.
Agent Chat always secures the database files but does not chmod an unrelated
pre-existing shared parent such as `/tmp`; securing that custom parent remains
the caller's responsibility.

There is at most one active group conversation per current group key and per
immutable owner. `Done` moves the active generation directly to Trash. The
first later post lazily creates the next generation. Restoring an older
generation while a newer generation is active returns
`active_generation_conflict` and never replaces the newer room.
Finishing or quarantining a group generation also moves its linked active side
chats to Trash and closes their pending wakes, so no orphan active side room
survives a stale parent generation.

Merely opening a panel is a pure read. It never creates a conversation and
never advances either cursor. `seen` advances only the named human viewer
cursor, clamped to the stored high-water mark. `read` and `room` advance only
the calling participant cursor.

Exact AoE membership is reconciled atomically on group mutations and agent
reads. A newly joined agent starts at the current highest sequence. Departed
agents keep history and their cursor but cannot add new group content. A PM
replacement updates the current moderator and preserves the immutable owner
when the historical owner session remains authoritatively linked to the group.
If that owner has disappeared and another PM occupies the same text path, the
store fails closed and treats it as path reuse. Group rename/profile moves
rebind all generations and linked side chats. A reused path owned by another
live PM is isolated from the previous owner.

This v1 continuity rule is necessarily heuristic because AoE does not yet give
groups a stable UUID. A demoted owner still present at the same path is treated
as a PM replacement; an absent owner with a different PM is treated as path
reuse. Fully unambiguous rebinds require a future stable AoE group ID or an
explicit audited rebind operation.

Side-chat bodies are readable only by the two fixed participants. The GM/PM
panel receives participant and activity metadata but an empty message window.
There are no observer grants in this release.

## Transactions, CAS, and idempotency

Writers use `BEGIN IMMEDIATE`, WAL mode, a five-second busy timeout, bounded
lock retries, and `UNIQUE(conversation_id, seq)` as the allocation backstop.
Each content or audit write increments the conversation revision.

TUI mutations carry an idempotency key. The store checks and returns a matching
committed idempotency result before evaluating expected revision or generation.
This makes an ambiguous retry return its original success even though the
conversation revision has advanced. Reusing a key for different input returns
`idempotency_conflict`.

Existing-conversation writes carry `--expected-revision`. Lazy room creation
carries both `--expected-no-active` and the exact
`--expected-generation`. Conflicts return typed errors instead of silently
retargeting typed content.

## Tagged JSON wire major 1

All v2 `--json` responses have one envelope. Unknown fields are allowed within
wire major 1.

Success:

```json
{
  "v": 1,
  "request_id": "opaque-id",
  "status": "ok",
  "data": {}
}
```

Error, emitted on stdout with exit code 1:

```json
{
  "v": 1,
  "request_id": "opaque-id",
  "status": "error",
  "error": {
    "code": "revision_conflict",
    "message": "the conversation changed; refresh and retry",
    "retryable": true,
    "details": {}
  }
}
```

Mutation data has a stable shape:

```json
{
  "conversation_id": "immutable-id",
  "generation": 2,
  "revision": 9,
  "post_seq": 14
}
```

`generation` and `post_seq` are `null` when the operation has no corresponding
value.

## AoE TUI commands

```text
agent-chat capabilities --json

agent-chat panel <group>
  --profile <profile>
  --viewer aoe-tui:<profile>
  [--conversation <id>]
  --json

agent-chat panels
  --profile <profile>
  --viewer aoe-tui:<profile>
  --json

agent-chat page <conversation-id>
  --before <exclusive-sequence>
  --limit <1..200>
  --json

agent-chat seen <conversation-id> <through-sequence>
  --viewer aoe-tui:<profile>
  --json

agent-chat post <group>
  --profile <profile>
  --expected-no-active
  --expected-generation <n>
  --body <message>
  --idempotency-key <key>
  --actor general-manager
  --json

agent-chat post
  --conversation <id>
  --expected-revision <revision>
  --body <message>
  --idempotency-key <key>
  --actor general-manager
  --json

agent-chat route <conversation-id> <target-session-id>
  --sequence <message-sequence>
  --expected-revision <revision>
  --idempotency-key <key>
  --actor general-manager
  --json

agent-chat done <conversation-id>
  --expected-revision <revision>
  --idempotency-key <key>
  --actor general-manager
  --json

agent-chat restore <conversation-id>
  --expected-revision <revision>
  --idempotency-key <key>
  --actor general-manager
  --json
```

`panel` returns the requested group key, current moderator metadata,
deterministically ordered conversation summaries, optional selected detail, and
total viewer unread. Detail includes participants, wakes, and a bounded message
window with `first_seq`, `through_seq`, `has_more_before`, and ascending posts.
Side detail always reports `body_visibility: "metadata_only"` and an empty
window to a GM viewer.

Embedding clients should allow at least 60 seconds for a mutating command. A
route can perform an authoritative AoE preflight and then a bounded targeted
delivery; each delivery attempt remains shorter than its 45-second lease.

## Agent-facing commands

```text
post <group> <message> [--profile <profile>]
say <conversation-id> <message>
room <group> [--profile <profile>] [--since <seq>]
read <conversation-id> [--since <seq>]
rooms
sidechat <other-session> <opening> [--group <group>] [--profile <profile>]
notify-moderator <conversation-id> [sequence]
nudge <side-conversation-id> [sequence]
```

`post` and `say` are always storage-only. `room` and `read` return bodies only
to stored participants and advance that participant's cursor. `rooms` reports
joined conversations and unread counts. Side-chat creation,
`notify-moderator`, and `nudge` are explicit wake operations.

## Stable first-release errors

The implementation uses typed codes including:

- `active_generation_conflict`, `generation_conflict`, and
  `revision_conflict`;
- `conversation_not_found`, `conversation_trashed`, and
  `conversation_not_trashed`;
- `group_not_found`, `group_deleted`, `moderator_not_found`, and
  `moderator_required`;
- `not_a_participant`, `side_chat_private`, and
  `source_post_not_found`;
- `idempotency_conflict`;
- transport diagnostics such as `unsupported_wake_transport`,
  `wake_target_not_found`, and `wake_delivery_failed` are persisted in the
  outbox row's `last_error`; the storage mutation itself still returns success.

The exact error message may improve; clients branch on `code`.

## First-release trust boundary

Agent Chat v1/v2 is a cooperative same-user tool. Agent processes, the AoE TUI,
the CLI, and `mail.db` currently run under one operating-system account. The
legacy global `--from` override and the TUI's `--actor general-manager` marker
are therefore assertions, not cryptographic capabilities. Authorization and
participant checks prevent accidental policy violations and provide a clear
protocol boundary, but they cannot defend against a malicious local peer that
can invoke the CLI directly or edit the shared database.

A hardened multi-user or adversarial deployment requires a daemon-owned store
and unforgeable caller capabilities. That is explicitly outside this first
release; no partial authentication scheme is implied here.
