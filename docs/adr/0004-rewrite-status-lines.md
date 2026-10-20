# 4. Rewrite out-of-band status lines to a verbatim sentence

Date: 2026-10-20 · Status: accepted

## Context
Open WebUI speaks call status (`working`, `approval`, `deferred`) with
`response.create {conversation: "none", input: [], instructions: "Say this briefly in the user's language: …"}`.
Qwen ignores `conversation: "none"` and reads the full conversation. Measured: without history it spoke the English
sentence; with a pending user request in history it **answered that request instead**, once saying it would
perform the action. A "speak Chinese" prefix fixed the language but not the hijack. An instruction to read one given
Chinese sentence verbatim, ignoring the conversation, was followed 5/5 times with the pending request in history.

## Decision
For the three known statuses (from `metadata.status`) replace the instructions entirely with that verbatim
instruction plus a fixed Chinese sentence. Unknown statuses keep Open WebUI's text with `STATUS_PREFIX`.

## Consequences
Status lines are always Chinese. The sentences must be updated if Open WebUI changes its status set.
