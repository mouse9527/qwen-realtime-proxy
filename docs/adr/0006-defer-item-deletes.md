# 6. Defer item deletes while speech or a response is in flight

Date: 2026-10-20 · Status: accepted (amends ADR 2)

## Context
The first production call ended after 3 s with
`error {code: InternalError, message: "Internal service error: previous item id does not exist: item_…"}`, which
closes the Qwen session. Reproduced with two short utterances 0.3 s apart (Open WebUI pacing; LiteLLM in front or not).

Captured between proxy and Qwen: while the second utterance was still being spoken, Open WebUI's chat-snapshot item
was inserted and Qwen anchored it after the in-progress user audio item; the next turn's snapshot refresh deleted that
snapshot (ADR 2 makes deletes real); a queued response / committed item still referenced it as its previous item, and
Qwen failed. Talking straight to Qwen never failed only because unmapped deletes were silently ignored there.

## Decision
Queue every `conversation.item.delete`. Send queued deletes only when no response is requested, active or held and no
VAD speech is open (between `input_audio_buffer.speech_started` and the transcription result). Check again after each
`response.done` and each transcription result. Ids whose create has not been acknowledged yet stay queued.

## Consequences
Old snapshots live a little longer (until the turn settles) instead of leaking forever. Replay test
`tests/fixtures/overlap-internalerror.jsonl` is the failing session.
