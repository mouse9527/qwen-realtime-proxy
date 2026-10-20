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

Everything else is passed through unchanged. Any other upstream `error` is forwarded (Open WebUI ends the call on
any error, so only the one error shown above to be harmless is absorbed).

## Configuration

| Variable | Required | Default | Meaning |
|---|---|---|---|
| `UPSTREAM_URL` | yes | — | `wss://<workspace-id>.cn-beijing.maas.aliyuncs.com/api-ws/v1/realtime` (no query string; the model comes from the client) |
| `UPSTREAM_KEY` | yes | — | Model Studio (DashScope) API key. Sent only to `UPSTREAM_URL` |
| `PROXY_TOKEN` | yes | — | Shared secret Open WebUI presents as `Authorization: Bearer …`. Never forwarded upstream |
| `DEFAULT_MODEL` | no | `qwen3.8-omni-flash-realtime` | Used when the client sends no `?model=` |
| `STATUS_PREFIX` | no | `请用中文说。` | Prepended to unknown out-of-band status lines |
| `HOST` / `PORT` | no | `0.0.0.0` / `8080` | Listen address |
| `LOG_LEVEL` | no | `INFO` | |

The process exits immediately, naming only the missing variables, if a required one is unset or empty.

Endpoints: `GET /v1/realtime` and `GET /realtime` (WebSocket), `GET /healthz`.

## Open WebUI settings

Admin → Settings → Audio → Voice calls:

| Field | Value |
|---|---|
| Call mode | Realtime |
| Provider | OpenAI |
| API Base URL | `http://<proxy-host>:8080/v1` (Open WebUI appends `/realtime?model=…`) |
| API Key | the `PROXY_TOKEN` value |
| Voice Model | `qwen3.8-omni-flash-realtime` (`qwen3.5-omni-*-realtime` also work; **not** `qwen-audio-3.1-realtime-plus`, which ignores `create_response: false`) |
| Transcription Model | `qwen3-asr-flash-realtime` |
| Voice | e.g. `Tina` |

## Security

- The upstream key lives only in the proxy's environment; the browser and Open WebUI never see it.
- Logs contain event types, counts and timings only — no audio, transcripts, tokens or keys. The HTTP access log is
  disabled.
- The image runs as uid 1000, needs no writable filesystem and no capabilities.
- Messages are capped at 512 KiB; upstream connect errors are reported by type, never by text.
- Put it on a private network: it is meant to be reached by Open WebUI only.

## Development

```sh
pip install -r requirements-dev.txt
pytest -q
```

`tests/fixtures/bridge-flow.jsonl` is a real Qwen session shaped like an Open WebUI call (audio payloads blanked).
