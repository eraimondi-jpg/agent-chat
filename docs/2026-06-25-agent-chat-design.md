# agent-chat — inter-agent messaging for the aoe fleet

**Date:** 2026-06-25
**Status:** Implemented
**Author:** Elia

## Problem

Agents run as long-lived, independent aoe sessions across different repos/worktrees
(e.g. `Agile Sysid`, `Agile Newton`, `Isaac Sim general`). Today they share knowledge
only through the async blackboard (`aoe context`). There is no way for one agent to
**ask a specific other agent a question and get a reply back within its own turn** —
e.g. a "presentation" agent gathering what was done in other projects.

The hard part is **not** message storage. It is the **wakeup**: a Claude agent only
acts when given a turn, so an idle recipient never sees a queued message, and a blocked
asker can't receive a reply mid-turn. The only primitive that can inject a turn into
another session is `aoe send`.

## Decision

- **Custom, aoe-native broker** (not MCP Agent Mail). Any solution needs the aoe
  wakeup glue + aoe-session addressing anyway; adopting Agent Mail would add a Rust
  dependency, an MCP server in 13 sessions, and an identity-mapping shim on top. A
  tailored single-file tool is smaller and integrates natively. Trade-off: we
  reimplement basic threading/concurrency Agent Mail already hardened. Revisit Agent
  Mail if rich threads/search become a requirement.
- **Blocking ask with async fallback**: `ask` blocks and polls until a reply or a
  timeout; on timeout the reply still lands later and wakes the asker via `aoe send`.
- **Location: general / home**, not tied to the agile repo. Source lives in
  `~/agent-chat/` (its own git repo); installed to `~/.local/bin/agent-chat`.

## Architecture

Three moving parts, all local to the machine:

1. **Store** — single SQLite DB (WAL) at `~/.local/share/agent-chat/mail.db`
   (`$AGENT_CHAT_DB` override). SQLite gives safe concurrent multi-session access for
   free. One `messages` table.
2. **Identity & resolution** — built on the aoe daemon:
   - self: `aoe session current --json` → `{id, session}` (override via `$AGENT_CHAT_ID`
     / `--from '<id>:<title>'` outside an aoe session / in tests).
   - recipient: resolve an id-or-title against `aoe list --json` (exact title
     case-insensitive, id, id-prefix, then substring). An explicit `id:title` bypasses
     the lookup. Ambiguous/unknown → error listing candidates.
3. **Doorbell** — `aoe send <recipient> "<self-describing text>"` injects a turn into
   the recipient's Claude session (default revive, so a stopped recipient auto-respawns).
   The text fully describes how to reply, so a recipient needs no prior protocol setup.

### Data model (`messages`)

| column | meaning |
|---|---|
| `id` | uuid4 hex, message id |
| `thread_id` | uuid4 of the root question; shared by its replies |
| `from_id`, `from_title` | sender aoe session |
| `to_id`, `to_title` | recipient aoe session |
| `kind` | `question` \| `reply` |
| `in_reply_to` | message id this answers (null for questions) |
| `body` | text |
| `status` | `unread` \| `read` \| `answered` \| `consumed` |
| `created_at` | ISO-8601 UTC |
| `blocking_until` | (questions) epoch until which the asker is actively polling; suppresses the reply doorbell |

### CLI

Single self-contained Python 3 (stdlib only). Verbs: `ask`, `reply`, `inbox`,
`replies`, `thread`, `whoami`. Test flags: `--from`, `--no-doorbell`,
`$AGENT_CHAT_DB`, `$AGENT_CHAT_DOORBELL_FAIL`.

**Automated-asker support** (added for the curator integration):
- `ask --json` → one object `{status, msg_id, thread_id, reply, reply_id?, from?}`,
  `status` ∈ `answered|pending|skipped`.
- `ask --no-revive` → don't wake a stopped recipient; return `skipped` immediately.
- Exit codes: `0` answered · `3` no answer (pending/skipped) · `1` error.

### Flow (happy path)

```
A: agent-chat ask "Agile Newton" "which restitution for G1 feet?"
   -> insert question(thread=T, blocking_until=now+120s)
   -> aoe send "Agile Newton" "[agent-chat] Question ... reply with: agent-chat reply <id> \"...\""
   -> poll DB ...
B: (turn injected) reads doorbell, runs:  agent-chat reply <id> "restitution=0.4"
   -> insert reply; question.blocking_until still future -> skip doorbell
A: poll sees reply -> prints it, marks consumed, exits 0
```

Slow path: no reply within timeout → `ask` returns `pending` and arms async delivery
(`blocking_until=0`); when B later replies, A is doorbelled and can read it next turn
(or via `agent-chat replies`).

## Reliability / edge cases

- **Dead recipient** → `aoe send` auto-revives (default).
- **Concurrency** → SQLite WAL + short transactions + uuid ids.
- **Outside an aoe session** → `$AGENT_CHAT_ID` / `--from`, else clear error.
- **Ambiguous/unknown recipient** → error listing candidates.
- **No spam loops** → doorbells fire only on `ask` and on the single reply.

## Making recipients reliably answer

The doorbell is self-describing, so it works with zero setup. To make agents treat
these as first-class and answer promptly, optionally add a one-line note to
`~/.claude/CLAUDE.md` (global, user-approved): *"If you receive an `[agent-chat]`
message, treat it as a question from another agent and answer it by running the
`agent-chat reply ...` command it shows; run `agent-chat inbox` to check for messages."*

## Testing

`python3 -m unittest discover -s tests` — store/logic + blocking-poll round-trip +
async round-trip + thread rendering + error path, all without an aoe daemon.

## Out of scope (v1)

Group broadcast, multi-turn beyond question→reply, search, attachments, file
reservations (these are where MCP Agent Mail would be the upgrade path).
