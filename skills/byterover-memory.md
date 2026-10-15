---
name: byterover-memory
description: Use ByteRover as structured long-term memory for dated sessions, top-level topics, and wallet records
published: true
---

# ByteRover Memory Skill

Use this skill whenever the user asks the agent to remember something, recall prior context, manage long-term memory, or keep track of wallet metadata and transactions.

## When to use this skill

Use this skill for requests like:

- "remember this"
- "save this for later"
- "what do you know from past sessions"
- "store this wallet address"
- "track this transaction"
- "recall what we decided"
- "what have we said about this before"

## Opinionated memory policy

ByteRover should be used intentionally.

- Organize memory by session date.
- Within each date, save only top-level topics that are important enough to retrieve later.
- Prefer durable summaries over raw transcripts.

## What to store

Persist:

- major decisions
- durable user preferences
- important topics discussed in the session
- wallet addresses plus a human-readable description
- wallet ownership or purpose
- meaningful transactions, especially with date, direction, asset, amount, counterparty, and purpose

## What to avoid

Do not store:

- trivial back-and-forth chatter
- noisy logs
- incidental debugging attempts
- repetitive command output

## Wallet guidance

For wallet-related memory, use ByteRover to maintain:

- address
- description
- owner or system role
- related project or agent
- significant incoming and outgoing transactions

If a wallet already exists in memory, update the existing mental record instead of creating fragmented duplicates.
