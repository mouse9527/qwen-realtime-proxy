# qwen-realtime-proxy

A small WebSocket proxy that lets **Open WebUI v0.12 Realtime voice calls** ("Bridge" call mode, which speaks the
OpenAI Realtime **GA** protocol) run on **Alibaba Cloud Model Studio Qwen-Omni-Realtime**
(`qwen3.8-omni-flash-realtime`).

Qwen-Omni-Realtime accepts the GA `session.update`, honours `create_response: false`, transcribes input and calls
functions, but differs from what Open WebUI needs in a few places. The proxy fixes exactly those, each verified
against the live service (see `docs/adr/`):

| Open WebUI expects | Qwen does | Proxy |
|---|---|---|
| `response.output_audio.*`, `response.output_audio_transcript.*` | beta names `response.audio.*`, `response.audio_transcript.*` | renames downstream events |
| `response.created/done` echo the `metadata` sent with `response.create` | metadata dropped | refills it (FIFO; Qwen never runs two responses at once) — [ADR 1](docs/adr/0001-fifo-response-metadata.md) |
| `conversation.item.create` keeps the client-chosen `id`, so `delete` by that id works | assigns its own id; `delete` of an unknown id still acks | maps client id → Qwen id and rewrites deletes — [ADR 2](docs/adr/0002-client-item-id-mapping.md) |
| `conversation.item.truncate` → `conversation.item.truncated` | never answers | answers locally, does not forward — [ADR 3](docs/adr/0003-synthesise-truncated.md) |
| `conversation: "none"` status lines are spoken out of band | answers from the full history instead | rewrites the three Open WebUI status lines to a verbatim Chinese sentence — [ADR 4](docs/adr/0004-rewrite-status-lines.md) |
| a second `response.create` while one is active is fine | rejects it unless it follows an item create | holds it until `response.done` |

The proxy **holds no credentials**: it forwards the caller's `Authorization` header to Qwen unchanged and refuses a
call without one ([ADR 5](docs/adr/0005-no-credentials-in-the-proxy.md)). The intended deployment puts LiteLLM in
front, so the Model Studio key lives in LiteLLM like every other model key:

```
Open WebUI ──/v1/realtime──► LiteLLM (model key) ──► qwen-realtime-proxy ──► Qwen-Omni-Realtime
```

Everything else is passed through unchanged. Any other upstream `error` is forwarded (Open WebUI ends the call on
any error, so only the one error shown above to be harmless is absorbed).

## Configuration

| Variable | Required | Default | Meaning |
|---|---|---|---|
| `UPSTREAM_URL` | yes | — | `wss://<workspace-id>.cn-beijing.maas.aliyuncs.com/api-ws/v1/realtime` (no query string; the model comes from the client) |
| `DEFAULT_MODEL` | no | `qwen3.8-omni-flash-realtime` | Used when the client sends no `?model=` |
| `STATUS_PREFIX` | no | `请用中文说。` | Prepended to unknown out-of-band status lines |
| `HOST` / `PORT` | no | `0.0.0.0` / `8080` | Listen address |
| `LOG_LEVEL` | no | `INFO` | |

The process exits immediately, naming the variable, if `UPSTREAM_URL` is unset or empty.

Endpoints: `GET /v1/realtime` and `GET /realtime` (WebSocket), `GET /healthz`.

## LiteLLM model

Add a model (UI or DB; LiteLLM ≥ 1.106.0 verified):

| Field | Value |
|---|---|
| Public model name | `qwen3.8-omni-flash-realtime` |
| LiteLLM model | `openai/qwen3.8-omni-flash-realtime` (the `openai/` prefix is stripped before it reaches the proxy) |
| API base | `http://<proxy-host>:8080` — **no `/v1`**: LiteLLM's OpenAI realtime handler always sets the path to `/v1/realtime` |
| API key | the Model Studio key for the workspace in `UPSTREAM_URL` |
| Mode | `realtime` |

LiteLLM sends that key upstream as `Authorization: Bearer …`; the caller's LiteLLM key never reaches the proxy.
LiteLLM also probes `GET <api base>/v1/models`; the proxy answers 404, which is harmless.

## Open WebUI settings

Admin → Settings → Audio → Voice calls:

| Field | Value |
|---|---|
| Call mode | Realtime |
| Provider | OpenAI |
| API Base URL | your LiteLLM base, e.g. `https://litellm.example/v1` (Open WebUI appends `/realtime?model=…`) |
| API Key | a LiteLLM key allowed to use the model |
| Voice Model | the LiteLLM model name, e.g. `qwen3.8-omni-flash-realtime` (`qwen3.5-omni-*-realtime` also work; **not** `qwen-audio-3.1-realtime-plus`, which ignores `create_response: false`) |
| Transcription Model | `qwen3-asr-flash-realtime` (sent inside the session, not routed by LiteLLM) |
| Voice | e.g. `Tina` |

Without LiteLLM, point Open WebUI straight at `http://<proxy-host>:8080/v1` and put the Model Studio key in its API Key
field.

## Security

- No secrets in the proxy: the caller's bearer token is forwarded to `UPSTREAM_URL` only, never logged, never stored.
  Restrict who can reach the proxy at the network level (e.g. a NetworkPolicy that admits only LiteLLM).
- Logs contain event types, counts and timings only — no audio, transcripts, tokens or keys. The HTTP access log is
  disabled.
- The image runs as uid 1000, needs no writable filesystem and no capabilities.
- Messages are capped at 512 KiB; upstream connect errors are reported by type, never by text.

## Development

```sh
pip install -r requirements-dev.txt
pytest -q
```

`tests/fixtures/bridge-flow.jsonl` is a real Qwen session shaped like an Open WebUI call (audio payloads blanked).
