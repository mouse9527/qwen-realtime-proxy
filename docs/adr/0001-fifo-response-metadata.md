# 1. Refill response metadata in FIFO order

Date: 2026-10-20 · Status: accepted

## Context
Open WebUI attaches `metadata` to every `response.create` (`input_item_id`, `call_id`, `status`) and reads it back
from `response.created` / `response.done` to pair a delegation with the user's utterance. Qwen-Omni-Realtime drops it.

Measured on `qwen3.8-omni-flash-realtime` (2026-10-20): Qwen never runs two responses concurrently on one
connection. A `response.create` sent while another response is active is **queued** if it directly follows a
`conversation.item.create`, and **rejected** otherwise with
`error {type: invalid_request_error, message: "Conversation already has an active response"}` (no code, no id).
Accepted requests produce `response.created` in send order.

## Decision
Keep a FIFO of metadata per connection: push on each forwarded `response.create`, pop on each `response.created`,
and re-attach by response id on `response.done`. Hold a `response.create` that Qwen would reject until the active
response is done, then send it. Absorb the busy error defensively (re-queue its request) — the only error the proxy
swallows.

## Consequences
Correct as long as Qwen keeps responses strictly serial. If Qwen ever runs responses in parallel the FIFO would
mis-pair metadata; a replay test pins the observed order.
