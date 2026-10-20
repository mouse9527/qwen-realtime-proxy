"""OpenAI Realtime GA (as spoken by Open WebUI v0.12 "Bridge") <-> Qwen-Omni-Realtime proxy.

A client (LiteLLM, or Open WebUI directly) connects to  ws(s)://<proxy>/v1/realtime?model=<name>  with
Authorization: Bearer <Model Studio key>. The proxy holds no credentials: it opens  UPSTREAM_URL?model=<name>  with the
caller's Authorization header unchanged (rejecting calls without one) and translates:

  up   response.create    remember metadata (FIFO); serialise while a response is active; OWUI status lines are
                          rewritten to a verbatim Chinese sentence (Qwen ignores conversation:'none')
  up   item.create(id)    remember the client id; Qwen assigns its own id
  up   item.delete        rewrite client id -> Qwen id (Qwen acks any id, so an unmapped delete would silently leak)
  up   item.truncate      not forwarded (Qwen never answers); a synthetic conversation.item.truncated is returned
  down response.audio.*             -> response.output_audio.*
  down response.audio_transcript.*  -> response.output_audio_transcript.*
  down response.created / done      metadata filled back in
  down error "already has an active response"   swallowed (the request is queued instead; see PROXY.md step 1)

No audio, transcripts or keys are logged; only event types, counts and latencies.
"""
import asyncio
import collections
import json
import logging
import os
import time
from urllib.parse import urlencode

from aiohttp import ClientSession, ClientWSTimeout, WSMsgType, web

log = logging.getLogger("qwen-realtime-proxy")

RENAME = {
    "response.audio.delta": "response.output_audio.delta",
    "response.audio.done": "response.output_audio.done",
    "response.audio_transcript.delta": "response.output_audio_transcript.delta",
    "response.audio_transcript.done": "response.output_audio_transcript.done",
}
BUSY_ERROR = "Conversation already has an active response"
# Qwen ignores conversation:'none' and answers from the full history (PROXY.md step 3), so out-of-band status lines
# are replaced by a verbatim-read instruction. Keys are OWUI's CALL_STATUSES (realtime.py:29-33).
VERBATIM = ("这是一条系统提示，不是用户的新请求。不要回答、不要评论对话中的任何问题，也不要声称做了任何操作。"
            "只用中文原样说出下面这一句话，说完立即停止：")
STATUS_LINES = {
    "working": "我正在处理你的请求。",
    "approval": "请在聊天界面里查看需要你确认的内容，我在那边等你。",
    "deferred": "请先在聊天界面里完成需要的设置或确认，然后再试一次。",
}
MAX_MESSAGE = 512 * 1024
CFG = web.AppKey("cfg", dict)
HTTP = web.AppKey("http", ClientSession)


class Translator:
    """Per-connection state. Pure: takes one event, returns (to_upstream, to_client) lists."""

    def __init__(self, status_prefix=""):
        self.status_prefix = status_prefix
        self.pending = collections.deque()  # response.create events forwarded, awaiting response.created
        self.meta = {}  # response id -> metadata
        self.active = False  # a response is created or requested and not yet done
        self.held = collections.deque()  # response.create waiting for the active response to finish
        self.after_item = False  # last upstream command was an item.create (Qwen queues those itself)
        self.pending_ids = collections.deque()  # client item ids awaiting conversation.item.created
        self.ids = {}  # client item id -> upstream item id

    # ---- client -> upstream
    def up(self, ev):
        kind = ev.get("type")
        if kind == "response.create":
            return self._response_create(ev), []
        self.after_item = kind == "conversation.item.create"
        if kind == "conversation.item.create":
            self.pending_ids.append(ev.get("item", {}).pop("id", None))
            return [ev], []
        if kind == "conversation.item.delete":
            item_id = ev.get("item_id")
            if item_id in self.ids:
                ev = {**ev, "item_id": self.ids.pop(item_id)}
            return [ev], []
        if kind == "conversation.item.truncate":
            return [], [{"type": "conversation.item.truncated", "item_id": ev.get("item_id"),
                         "content_index": ev.get("content_index", 0), "audio_end_ms": ev.get("audio_end_ms", 0)}]
        return [ev], []

    def _response_create(self, ev):
        response = ev.get("response") or {}
        if response.get("conversation") == "none" and response.get("instructions"):
            line = STATUS_LINES.get((response.get("metadata") or {}).get("status"))
            instructions = VERBATIM + line if line else self.status_prefix + response["instructions"]
            ev = {**ev, "response": {**response, "instructions": instructions}}
        if self.active and not self.after_item:
            self.held.append(ev)
            return []
        self.after_item = False
        self.active = True
        self.pending.append(ev)
        return [ev]

    # ---- upstream -> client
    def down(self, ev):
        kind = ev.get("type", "")
        to_up = []
        if kind == "error" and (ev.get("error") or {}).get("message") == BUSY_ERROR:
            # Defensive: should not happen because such requests are held.
            # Qwen rejected the most recent request: take it back (unchanged) and send it after the active response.
            if self.pending:
                self.held.appendleft(self.pending.pop())
            return [], []
        if kind in RENAME:
            ev = {**ev, "type": RENAME[kind]}
        elif kind == "conversation.item.created":
            item = ev.get("item") or {}
            # Items Qwen creates itself (response output, VAD speech) arrive in_progress; client item.create acks
            # arrive completed, in send order.
            if self.pending_ids and item.get("status") == "completed":
                client_id = self.pending_ids.popleft()
                if client_id:
                    self.ids[client_id] = item.get("id")
        elif kind == "response.created":
            rid = ev["response"]["id"]
            sent = self.pending.popleft() if self.pending else {}
            self.meta[rid] = dict((sent.get("response") or {}).get("metadata") or {})
            ev = {**ev, "response": {**ev["response"], "metadata": self.meta[rid]}}
        elif kind == "response.done":
            rid = ev["response"]["id"]
            ev = {**ev, "response": {**ev["response"], "metadata": self.meta.pop(rid, {})}}
            self.active = bool(self.pending)
            if not self.active and self.held:
                to_up = self._response_create(self.held.popleft())
        return to_up, [ev]


def _bearer(request):
    header = request.headers.get("Authorization", "")
    return header if header.startswith("Bearer ") and len(header) > len("Bearer ") else None


async def realtime(request):
    cfg = request.app[CFG]
    authorization = _bearer(request)
    if authorization is None:
        return web.Response(status=401, text="missing bearer token")
    model = request.query.get("model") or cfg["default_model"]
    upstream_url = f'{cfg["upstream"]}?{urlencode({"model": model})}'
    client = web.WebSocketResponse(heartbeat=20, max_msg_size=MAX_MESSAGE)
    started = time.monotonic()
    session: ClientSession = request.app[HTTP]
    try:
        upstream = await session.ws_connect(upstream_url, headers={"Authorization": authorization},
                                            heartbeat=20, max_msg_size=MAX_MESSAGE,
                                            timeout=ClientWSTimeout(ws_receive=None, ws_close=10))
    except Exception as exc:  # handshake status only; never the exception text (could echo headers)
        log.warning("upstream connect failed model=%s error=%s", model, type(exc).__name__)
        return web.Response(status=getattr(exc, "status", 502), text="upstream connect failed")
    await client.prepare(request)
    tr = Translator(cfg["status_prefix"])
    counts = collections.Counter()
    log.info("call start model=%s connect_ms=%d", model, (time.monotonic() - started) * 1000)

    async def pump_up():
        async for msg in client:
            if msg.type != WSMsgType.TEXT:
                break
            ev = json.loads(msg.data)
            to_up, to_client = tr.up(ev)
            counts["up:" + ev.get("type", "?")] += 1
            for out in to_up:
                await upstream.send_str(json.dumps(out, ensure_ascii=False))
            for out in to_client:
                await client.send_str(json.dumps(out, ensure_ascii=False))

    async def pump_down():
        async for msg in upstream:
            if msg.type != WSMsgType.TEXT:
                break
            ev = json.loads(msg.data)
            kind = ev.get("type", "?")
            counts["down:" + kind] += 1
            if kind == "error":
                log.info("upstream error type=%s code=%s", (ev.get("error") or {}).get("type"),
                         (ev.get("error") or {}).get("code"))
            to_up, to_client = tr.down(ev)
            for out in to_up:
                await upstream.send_str(json.dumps(out, ensure_ascii=False))
            for out in to_client:
                await client.send_str(json.dumps(out, ensure_ascii=False))

    tasks = [asyncio.create_task(pump_up()), asyncio.create_task(pump_down())]
    try:
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await upstream.close()
        await client.close()
        top = ", ".join(f"{k}={v}" for k, v in counts.most_common(12))
        log.info("call end model=%s seconds=%.1f events: %s", model, time.monotonic() - started, top)
    return client


async def health(_):
    return web.Response(text="ok")


def make_app(cfg):
    app = web.Application()
    app[CFG] = cfg

    async def http(app):
        app[HTTP] = ClientSession()
        yield
        await app[HTTP].close()

    app.cleanup_ctx.append(http)
    for path in ("/v1/realtime", "/realtime"):
        app.router.add_get(path, realtime)
    app.router.add_get("/healthz", health)
    return app


REQUIRED = ("UPSTREAM_URL",)


def config_from_env(env=os.environ):
    missing = [name for name in REQUIRED if not env.get(name, "").strip()]
    if missing:
        # Names only; values are never printed.
        raise SystemExit(f"missing required environment variables: {', '.join(missing)}")
    if not env["UPSTREAM_URL"].startswith(("wss://", "ws://")):
        raise SystemExit("UPSTREAM_URL must start with wss:// or ws://")
    return {
        "upstream": env["UPSTREAM_URL"].strip(),  # wss://<workspace>.cn-beijing.maas.aliyuncs.com/api-ws/v1/realtime
        "default_model": env.get("DEFAULT_MODEL", "qwen3.8-omni-flash-realtime"),
        "status_prefix": env.get("STATUS_PREFIX", "请用中文说。"),
    }


if __name__ == "__main__":
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(message)s")
    cfg = config_from_env()
    web.run_app(make_app(cfg), access_log=None, host=os.environ.get("HOST", "0.0.0.0"), port=int(os.environ.get("PORT", "8080")))
