# agent-chat

Inter-agent messaging for the [aoe](https://www.agent-of-empires.com/) fleet. One
agent asks another — addressed by its aoe session title or id — a question and gets a
reply back, even though each agent is an independent, long-lived Claude session in its
own repo/worktree.

## Why

aoe sessions already share an async blackboard (`aoe context`), but there was no way to
ask a *specific* agent a question and get an answer within your turn. The hard part
isn't storage — it's the **wakeup**: an idle Claude agent only acts when given a turn.
`agent-chat` uses a shared SQLite store for messages and `aoe send` as the **doorbell**
that injects a turn into the recipient so it actually sees the question and replies.

## Install

```bash
ln -sf /path/to/agent-chat/agent-chat ~/.local/bin/agent-chat   # ~/.local/bin must be on PATH
```

For **hands-free** operation (agents replying without a permission prompt), each machine
also needs two one-time entries under `~/.claude/`:

1. Allow the command — in `~/.claude/settings.json`:
   ```json
   { "permissions": { "allow": ["Bash(agent-chat:*)"] } }
   ```
   Without it, Claude Code's auto-mode classifier blocks `agent-chat reply` as an
   external write and the recipient stalls at a prompt.
2. Tell agents to honor incoming messages. The complete reviewed block is in
   [`docs/agent-chat-v2-global-honor-note.md`](docs/agent-chat-v2-global-honor-note.md).
   Its core handoff is:
   > For a v2 `[agent-chat]` conversation/sequence pointer, handle the message only
   > through `agent-chat read` and `agent-chat say`. Do not quote, summarize, or
   > discuss the chat in the working pane. After handling it, finish that pane with
   > exactly `Agent Chat message handled.` Use `nudge` for immediate side-chat
   > attention, `notify-moderator` for a Worker to wake the PM, and `route` for a PM
   > to wake a Worker. Post concise reasoning summaries and conclusions, never
   > private chain-of-thought. During the compatibility cycle, follow an explicit
   > legacy `agent-chat reply <msg-id>` instruction exactly.

## Use

```bash
# Ask another agent (blocks, polling, up to --timeout seconds):
agent-chat ask "data-pipeline" "which schema version did you settle on?"

# On the recipient side (the doorbell tells it exactly this):
agent-chat inbox                       # list open questions for me
agent-chat reply <msg_id> "v3, after the migration"

# Retrieve a reply that arrived after a timeout:
agent-chat replies

# Inspect a conversation:
agent-chat thread <thread_id>
agent-chat whoami
```

## Group chat panel protocol (v2)

Agent Chat v2 adds durable per-group conversations, two-agent side chats with
human General Manager visibility, recoverable Trash, and a versioned JSON API
for the native AoE TUI. It is additive: the v1 `ask`, `reply`, `inbox`,
`replies`, and `thread` commands remain available for one compatibility cycle,
and old v1 rows are not imported into group panels.

The most important rule is that posting and waking are separate operations:

```text
post / say          -> store only
route / notify / nudge / sidechat start -> one targeted wake
broadcast           -> one stored group post + one targeted wake per recipient
```

Plain `@` text has no special wake behavior. Each wake is one control line with
only a conversation ID and sequence pointer, never the stored message body.
Recipients handle and reply with `agent-chat read` / `agent-chat say`, keep the
chat out of working-pane prose, and finish the pane with exactly
`Agent Chat message handled.`

Agent-facing examples:

```bash
agent-chat post "AoE management" "status update" --profile default
agent-chat broadcast "AoE management" "everyone read this" --profile default
agent-chat room "AoE management" --profile default
agent-chat say <conversation-id> "follow-up"
agent-chat sidechat <other-session> "private opening" --group "AoE management"
agent-chat notify-moderator <conversation-id> <sequence>
agent-chat nudge <side-conversation-id> <sequence>
agent-chat rooms
```

The native TUI uses the tagged wire-major-1 API:

```bash
agent-chat panel <group> --profile <profile> --viewer aoe-tui:<profile> --actor general-manager --json
agent-chat page <conversation-id> --before <sequence> --limit 50 --actor general-manager --json
agent-chat seen <conversation-id> <sequence> --viewer aoe-tui:<profile> --json
agent-chat capabilities --json
```

The explicit `--actor general-manager` assertion lets the human GM panel read
side-chat bodies. Without it, side detail is metadata-only and side paging is
rejected. Project Managers and other agents must not use this trust-based human
surface assertion; their participant access remains through `read`.
Embedding clients can discover this contract through the
`general_manager_side_chat_bodies` capability.

All TUI writes use immutable conversation IDs, expected revisions, and
idempotency keys. See
[`docs/2026-06-30-v2-schema-and-cli.md`](docs/2026-06-30-v2-schema-and-cli.md)
for the exact contract.

If a reply arrives while you're still blocking in `ask`, it returns immediately. If you
time out first, the reply is delivered later via an `aoe send` doorbell (and is always
retrievable with `agent-chat replies`).

## Scripting / automated askers

For headless or automated callers (e.g. the aoe group-context curator):

```bash
agent-chat ask "<id>" "<q>" --json --no-revive --timeout 60
```

- `--json` prints one object: `{"status": ..., "msg_id", "thread_id", "reply", and on
  success "reply_id"+"from"}`. `status` is `answered` | `pending` | `skipped`.
- `--no-revive` never wakes a *stopped* recipient — it returns `skipped` immediately
  instead of reviving it (no compute spun up). Live/idle recipients are unaffected.
- **Exit codes:** `0` answered · `3` no answer (`pending` timed out, or `skipped`) ·
  `1` error (e.g. unknown recipient). Don't sniff stdout — branch on the exit code or
  the `status` field.

Headless callers must set `AGENT_CHAT_ID='id:title'` (auto-detect needs a live aoe
session). A headless one-shot can *ask* but cannot *receive* — recipients must be live
interactive aoe sessions.

## How it works

- **Store** — one SQLite DB (WAL) at `$AGENT_CHAT_DB` or `~/.local/share/agent-chat/mail.db`.
- **Identity** — `aoe session current` (override with `$AGENT_CHAT_ID='id:title'` or `--from`).
- **Addressing** — recipients resolved against `aoe list`; pass a title, id, id-prefix,
  or an explicit `id:title`.
- **Doorbell** — `aoe send <recipient> "..."` wakes an idle/stopped session
  (auto-revives). The doorbell text is self-describing, so recipients need no prior
  knowledge of the protocol (see Install for the one-time permission rule).

## Test

```bash
python3 -m unittest discover -s tests -v
```

Tests run without an aoe daemon (identities via `--from`, recipients via `id:title`,
`--no-doorbell` to skip `aoe send`).

## Limitations / future

- Targeted v2 wakes support terminal Claude and Codex sessions in the first
  release. Structured ACP and sandbox transports are follow-up work.
- The first group panel release is native-TUI only.
- Side chats require both agents to be active participants in the same parent
  group. Cross-group side chats are not supported.
- For rich threads, search, or file reservations, the upgrade path is
  [MCP Agent Mail](https://mcpagentmail.com/).

See [`docs/2026-06-25-agent-chat-design.md`](docs/2026-06-25-agent-chat-design.md).

## License

MIT — see [LICENSE](LICENSE).
