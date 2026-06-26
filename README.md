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
ln -sf ~/agent-chat/agent-chat ~/.local/bin/agent-chat   # ~/.local/bin must be on PATH
```

## Use

```bash
# Ask another agent (blocks, polling, up to --timeout seconds):
agent-chat ask "Agile Newton" "which restitution did you use for the G1 feet?"

# On the recipient side (the doorbell tells it exactly this):
agent-chat inbox                       # list open questions for me
agent-chat reply <msg_id> "0.4"        # answer one

# Retrieve a reply that arrived after a timeout:
agent-chat replies

# Inspect a conversation:
agent-chat thread <thread_id>
agent-chat whoami
```

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
  (auto-revives). The doorbell text is self-describing, so recipients need no setup.

## Test

```bash
python3 -m unittest discover -s tests -v
```

Tests run without an aoe daemon (identities via `--from`, recipients via `id:title`,
`--no-doorbell` to skip `aoe send`).

## Limitations / future

- 1:1 question→reply. Group broadcast (ask a whole aoe group) is a natural v2.
- For rich threads, search, or file reservations, the upgrade path is
  [MCP Agent Mail](https://mcpagentmail.com/).

See [`docs/2026-06-25-agent-chat-design.md`](docs/2026-06-25-agent-chat-design.md).
