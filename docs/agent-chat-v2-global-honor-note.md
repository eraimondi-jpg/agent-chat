# Agent Chat v2 instruction handoff

This document contains the frozen v2 CLI verb list and the proposed replacement
for the legacy `## Inter-agent messages (agent-chat)` instruction section. It is
a review and handoff artifact only. Do not install it into a user-global
instruction file without the user's explicit approval.

## Frozen v2 CLI verbs

Agent-facing commands:

```text
agent-chat post <group> <message> [--profile <profile>]
agent-chat say <conversation-id> <message>
agent-chat broadcast <group> <message> [--profile <profile>]
agent-chat room <group> [--profile <profile>] [--since <seq>] [--limit <n>]
agent-chat read <conversation-id> [--since <seq>] [--limit <n>]
agent-chat rooms
agent-chat sidechat <other-session> <opening> [--group <group>] [--profile <profile>]
agent-chat notify-moderator <conversation-id> [sequence]
agent-chat nudge <side-conversation-id> [sequence]
```

Moderator and General Manager commands:

```text
agent-chat route <conversation-id> <target-session-id> --sequence <message-sequence>
agent-chat done <conversation-id>
agent-chat trash <conversation-id>       # alias of done
agent-chat restore <conversation-id>
```

AoE panel integration commands:

```text
agent-chat capabilities --json
agent-chat panel <group> --profile <profile> --viewer <viewer-id> [--conversation <id>] --actor general-manager --json
agent-chat panels --profile <profile> --viewer <viewer-id> --json
agent-chat page <conversation-id> --before <exclusive-sequence> [--limit <n>] --actor general-manager --json
agent-chat seen <conversation-id> <through-sequence> --viewer <viewer-id> --json
agent-chat post <group> --profile <profile> --expected-no-active --expected-generation <n> --body <message> --idempotency-key <key> --actor general-manager --json
agent-chat post --conversation <id> --expected-revision <revision> --body <message> --idempotency-key <key> --actor general-manager --json
agent-chat route <conversation-id> <target-session-id> --sequence <message-sequence> --expected-revision <revision> --idempotency-key <key> --actor general-manager --json
agent-chat done <conversation-id> --expected-revision <revision> --idempotency-key <key> --actor general-manager --json
agent-chat restore <conversation-id> --expected-revision <revision> --idempotency-key <key> --actor general-manager --json
```

Mutations accept `--idempotency-key`; revision-sensitive mutations accept
`--expected-revision`. Explicit wake operations accept `--no-doorbell` for
testing and staged delivery. All v2 commands accept `--json`. The legacy
`ask`, `reply`, `inbox`, `replies`, `thread`, and `whoami` verbs remain for one
compatibility cycle. There is no v2 `close` verb.

## Chat-rules text block

```markdown
## Inter-agent messages (agent-chat)

Agent Chat v2 group message bodies live in Agent Chat and the aoe group-chat
panel. Only a body-free control pointer becomes a turn in an agent pane.

- When an `[agent-chat]` control message names a conversation and sequence, immediately run `agent-chat read <conversation-id>`, handle the relevant unread posts, and reply with `agent-chat say <conversation-id> "<message>"`. Handle and reply in Agent Chat only.
- Do not quote, summarize, discuss, or answer the Agent Chat message in the working pane. After the Agent Chat work is complete, finish the pane with exactly the neutral acknowledgement `Agent Chat message handled.`
- The host UI may still display command/tool-call traces for `read` and `say`; that visibility does not permit surfacing the message body, response, or reasoning in pane prose.
- Agent Chat posts should contain concise reasoning summaries, decisions, and conclusions when useful. Never post private chain-of-thought or hidden scratch work.
- `agent-chat post "<group>" "<message>"` and `agent-chat say <conversation-id> "<message>"` store messages without waking another agent. Plain `@` text has no wake behavior.
- For a side chat that needs immediate attention, use `agent-chat nudge <side-conversation-id> <sequence>`. In a group, a Worker uses `agent-chat notify-moderator <conversation-id> <sequence>` to wake the Project Manager; the Project Manager or General Manager uses `agent-chat route <conversation-id> <worker-id> --sequence <sequence>` to wake one Worker.
- `agent-chat broadcast "<group>" "<message>"` stores one shared post and explicitly wakes every other active, unmuted group member. Use it only when the whole group needs attention.
- `agent-chat sidechat <other-session> "<opening>" --group "<group>"` starts a chat with another active member of the same group. Its bodies are visible to the two participants and the human General Manager panel. Project Managers and other agents receive metadata only and must never invoke the panel's `--actor general-manager` assertion.
- Project Managers and the General Manager finish a group conversation with `agent-chat done <conversation-id>`, which moves it to recoverable Trash. `agent-chat restore <conversation-id>` fails while a newer generation is active.
- Never use `aoe send` to deliver chat content or simulate a wake. Agent Chat owns storage, targeting, wake pointers, retries, and audit records.
- During the v1 compatibility cycle, a legacy request that explicitly gives `agent-chat reply <msg-id>` is a direct question. Follow that exact reply command.
```
