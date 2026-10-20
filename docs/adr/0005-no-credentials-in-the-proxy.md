# 5. The proxy holds no credentials

Date: 2026-10-20 · Status: accepted (supersedes the first release, which held the Model Studio key and its own token)

## Context
Every other model key in the deployment lives in LiteLLM, with per-client keys, spend logs and model access control.
A proxy holding its own Model Studio key would be a second place to rotate and audit, and it would need a token of
its own for its callers.

Measured with LiteLLM 1.106.0 (`openai/<model>` + `api_base`): the realtime WebSocket reaches the `api_base` host at
`/v1/realtime?model=<model without the openai/ prefix>`, with `Authorization: Bearer <the model's api_key>`. GA
`session.update`, `response.create` (metadata, `conversation: "none"`, instructions), client item ids, delete, cancel
and truncate pass through unchanged in both directions. The client's own LiteLLM key is not forwarded.

## Decision
Forward the caller's `Authorization` header to the upstream unchanged; refuse calls without a bearer token with 401
before connecting upstream; return the upstream's handshake status (e.g. 401) when it refuses. Only `UPSTREAM_URL` is
required configuration.

## Consequences
Anyone who can reach the proxy and holds a valid Model Studio key can use it — so who can reach it must be limited by
the network (in this deployment a NetworkPolicy admitting only LiteLLM). Open WebUI can still connect directly by
putting the Model Studio key in its API Key field.
