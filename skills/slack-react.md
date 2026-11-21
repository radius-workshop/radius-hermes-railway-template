---
name: slack-react
description: Add or remove an emoji reaction, including workspace custom emoji, on a message in the current Slack conversation
published: false
---

# Slack reactions

Use `slack_react` when someone asks you to react to a message, or when a reaction says it better than a reply (an acknowledgement, a 🎉).

- `slack_react({emoji, target, message_ts, remove})`
- `emoji`: the emoji's name, with or without colons: `tada`, `white_check_mark`, or a workspace custom emoji such as `radius`. A pasted emoji character is not accepted; use its name.
- `target`: `message` (default) is the message that started this turn. `thread_parent` is the first message of the thread you are in.
- `message_ts`: the Slack timestamp of another message in this conversation, when you have one. It overrides `target`.
- `remove: true` takes back a reaction you added. You can't remove other people's reactions.

Rules:

- The 👀 that appears while you work, and the ✅ or ❌ that replaces it, are added by Hermes automatically on messages addressed to you. They are yours, but you don't control them. Say that if someone asks; don't claim you can't react.
- You can only react in the conversation you are in. Thread context doesn't include timestamps, so for any message other than the current one or the thread parent, ask for a link to it (the `p1760649990000200` at the end of a Slack link is the timestamp `1760649990.000200`).
- `error: invalid_name` means no emoji has that name in this workspace. Tell the person, and ask for the exact name rather than guessing others.
- One reaction per call. Don't add more than two or three to one message.
- A reaction is not a reply. If someone asked a question, still answer it.
