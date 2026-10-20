# 7. Drop bare response.create when the client drives responses

Date: 2026-10-20 · Status: accepted

## Context
LiteLLM 1.106.0's raw realtime relay (`litellm_core_utils/realtime_streaming.py`, `_handle_raw_backend_message`) sends
`{"type": "response.create"}` to the provider after **every** `conversation.item.input_audio_transcription.completed`,
whether or not audio guardrails are configured. Open WebUI's Bridge sets `turn_detection.create_response: false` and
sends its own `response.create` with metadata once it has decided how to answer. Measured: every spoken turn produced
two responses, one with empty metadata, so the user hears an extra reply, Open WebUI stores an extra assistant
message, and the FIFO pairing of ADR 1 can shift.

## Decision
Remember the session's `create_response` setting. When it is `false`, drop a `response.create` that has no `response`
body. Open WebUI never sends one without a body. With auto-response on (or unset) bare creates pass through.

## Consequences
Behind LiteLLM each turn yields exactly the responses the client asked for. Revisit if LiteLLM stops injecting.
