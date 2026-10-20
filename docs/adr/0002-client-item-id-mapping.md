# 2. Map client item ids to Qwen item ids

Date: 2026-10-20 · Status: accepted

## Context
Open WebUI re-sends a chat snapshot before each turn as a system item with a client-chosen id
(`chat_context_N`) and deletes the previous one by that id. Qwen ignores client ids and assigns its own, and acks
`conversation.item.delete` with `conversation.item.deleted` for **any** id, even one that does not exist. Without a
fix every snapshot (up to 64 k characters) stays in the upstream context.

## Decision
Strip `id` from upstream `conversation.item.create` and remember it. Pair it with the next
`conversation.item.created` whose status is `completed` (client item acks arrive completed and in order; items Qwen
creates itself — response output, VAD speech — arrive `in_progress`). Rewrite `conversation.item.delete` to the
upstream id.

## Consequences
Downstream events keep Qwen's ids; Open WebUI only uses its own ids for deletes and does not read them back.
