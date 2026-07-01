# Agent Chat v2 global honor-note draft

This is the proposed replacement for the legacy `## Inter-agent messages
(agent-chat)` section in `~/.claude/CLAUDE.md`. It is a review artifact only.
Do not install it into a user-global instruction file without the user's
explicit approval.

```markdown
## Inter-agent messages (agent-chat)

Agent Chat v2 group messages live in Agent Chat and the aoe group-chat panel.
They never become turns in agent panes.

- When an `[agent-chat]` control message names a conversation and sequence, immediately run `agent-chat read <conversation-id>` and act on the relevant unread posts. Use `agent-chat rooms` to list joined conversations or `agent-chat room "<group>"` to read a group room.
- `agent-chat post "<group>" "<message>"` and `agent-chat say <conversation-id> "<message>"` store messages without waking another agent. Plain `@` text has no wake behavior.
- Use `agent-chat notify-moderator <conversation-id> <sequence>` when the Project Manager must act. Only the Project Manager or General Manager may use `agent-chat route <conversation-id> <worker-id> --sequence <sequence>` to wake one Worker.
- `agent-chat broadcast "<group>" "<message>"` stores one shared post and explicitly wakes every other active, unmuted group member. Use it only when the whole group needs attention.
- `agent-chat sidechat <other-session> "<opening>" --group "<group>"` starts a private chat with another active member of the same group. Only the two participants may read its bodies; the Project Manager and General Manager see metadata only.
- Project Managers and the General Manager finish a group conversation with `agent-chat done <conversation-id>`, which moves it to recoverable Trash. `agent-chat restore <conversation-id>` fails while a newer generation is active.
- Never use `aoe send` to deliver chat content or simulate a wake. Agent Chat owns storage, targeting, wake pointers, retries, and audit records.
- During the v1 compatibility cycle, a legacy request that explicitly gives `agent-chat reply <msg-id>` is a direct question. Follow that exact reply command.
```
