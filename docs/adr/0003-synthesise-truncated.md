# 3. Answer conversation.item.truncate locally

Date: 2026-10-20 · Status: accepted

## Context
On barge-in Open WebUI cancels the response and sends `conversation.item.truncate` with the played position.
Qwen never answers truncate (no ack, no error). Deleting the cancelled item and inserting a truncated text item is
accepted by the protocol, but it was not shown that the model then treats the shorter text as what it said. Qwen
also deletes the turn's `function_call_output` by itself after a cancel.

## Decision
Do not forward truncate; reply with a synthetic `conversation.item.truncated` echoing `item_id`, `content_index`
and `audio_end_ms`.

## Consequences
Upstream still believes the whole cancelled reply was spoken. No conversational problem was observed; Open WebUI's
chat snapshot carries the authoritative history. Revisit if Qwen adds truncate.
