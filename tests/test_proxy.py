"""Replay tests: Translator against the recorded Qwen session, plus an end-to-end socket test against a fake upstream."""
import asyncio
import json
import pathlib

import pytest
from aiohttp import web, WSMsgType
from aiohttp.test_utils import TestServer, TestClient

from app.proxy import Translator, make_app, config_from_env, scrub, BUSY_ERROR, STATUS_LINES, VERBATIM

AUTH = "Bearer "  # test-only dummy credentials below; never real values
FIXTURE = pathlib.Path(__file__).parent / "fixtures" / "bridge-flow.jsonl"
ROWS = [json.loads(line) for line in FIXTURE.read_text().splitlines()]


def replay(tr):
    """Feed the recording through the translator in its recorded order; collect what each side receives."""
    to_up, to_client = [], []
    for row in ROWS:
        ev = json.loads(json.dumps(row["ev"]))
        up, client = (tr.up if row["dir"] == "up" else tr.down)(ev)
        to_up += up
        to_client += client
    return to_up, to_client


def test_audio_events_renamed_and_no_beta_names_reach_client():
    _, out = replay(Translator())
    kinds = {e["type"] for e in out}
    assert "response.output_audio.delta" in kinds
    assert "response.output_audio_transcript.done" in kinds
    assert "response.output_audio_transcript.delta" in kinds
    assert not kinds & {"response.audio.delta", "response.audio.done", "response.audio_transcript.delta",
                        "response.audio_transcript.done"}


def test_metadata_filled_back_in_send_order():
    _, out = replay(Translator())
    created = [e["response"]["metadata"] for e in out if e["type"] == "response.created"]
    done = [e["response"]["metadata"] for e in out if e["type"] == "response.done"]
    sent = [r["ev"]["response"]["metadata"] for r in ROWS if r["dir"] == "up" and r["ev"]["type"] == "response.create"]
    assert created == sent == done
    assert created[0] == {"input_item_id": "item_ERIi6lakjgwjLrlqPhd15"}  # OWUI matches the delegation by this
    assert created[1] == {"status": "working"}
    assert created[2]["call_id"].startswith("call_")


def test_function_call_item_passes_unchanged():
    _, out = replay(Translator())
    calls = [e for e in out if e["type"] == "response.output_item.done" and e["item"]["type"] == "function_call"]
    assert len(calls) == 1 and calls[0]["item"]["name"] == "generate_chat_completion"
    assert json.loads(calls[0]["item"]["arguments"])["request"]


def test_context_item_delete_rewritten_to_upstream_id():
    up, _ = replay(Translator())
    deletes = [e["item_id"] for e in up if e["type"] == "conversation.item.delete"]
    assert deletes == ["item_JEdTlFhnwWGGl9cVQ17u2"]  # Qwen's id for chat_context_1, not the client id
    creates = [e for e in up if e["type"] == "conversation.item.create"]
    assert all("id" not in e["item"] for e in creates)


def test_truncate_answered_locally_not_forwarded():
    up, out = replay(Translator())
    assert not [e for e in up if e["type"] == "conversation.item.truncate"]
    truncated = [e for e in out if e["type"] == "conversation.item.truncated"]
    assert truncated == [{"type": "conversation.item.truncated", "item_id": "item_Zlea7Zgw61XSBDvOkUzkj",
                          "content_index": 0, "audio_end_ms": 500}]


def test_known_status_lines_become_verbatim_chinese_and_normal_turns_untouched():
    up, _ = replay(Translator("请用中文说。"))
    instr = [e["response"].get("instructions") for e in up if e["type"] == "response.create"]
    assert instr[0] is None and instr[2] is None
    assert instr[1] == VERBATIM + STATUS_LINES["working"]
    assert "Say this briefly" not in instr[1]


def test_unknown_status_keeps_owui_text_with_language_prefix():
    tr = Translator("请用中文说。")
    up, _ = tr.up({"type": "response.create", "response": {"conversation": "none", "input": [],
                   "instructions": "Say this: hi", "metadata": {"status": "new-kind"}}})
    assert up[0]["response"]["instructions"] == "请用中文说。Say this: hi"


def test_no_error_reaches_client_in_recorded_flow():
    _, out = replay(Translator())
    assert not [e for e in out if e["type"] == "error"]


def test_response_create_while_active_is_held_then_sent_on_done():
    tr = Translator()
    assert tr.up({"type": "response.create", "response": {"metadata": {"n": 1}}}) == ([{"type": "response.create", "response": {"metadata": {"n": 1}}}], [])
    tr.down({"type": "response.created", "response": {"id": "r1"}})
    held_up, _ = tr.up({"type": "response.create", "response": {"metadata": {"n": 2}}})
    assert held_up == []
    up, out = tr.down({"type": "response.done", "response": {"id": "r1", "status": "completed"}})
    assert out[0]["response"]["metadata"] == {"n": 1}
    assert up == [{"type": "response.create", "response": {"metadata": {"n": 2}}}]
    _, out = tr.down({"type": "response.created", "response": {"id": "r2"}})
    assert out[0]["response"]["metadata"] == {"n": 2}


def test_response_create_after_item_create_is_forwarded_since_qwen_queues_it():
    tr = Translator()
    tr.up({"type": "response.create", "response": {"metadata": {"n": 1}}})
    tr.down({"type": "response.created", "response": {"id": "r1"}})
    tr.up({"type": "conversation.item.create", "item": {"type": "function_call_output", "call_id": "c", "output": "x"}})
    up, _ = tr.up({"type": "response.create", "response": {"metadata": {"n": 2}}})
    assert len(up) == 1
    tr.down({"type": "response.done", "response": {"id": "r1"}})
    _, out = tr.down({"type": "response.created", "response": {"id": "r2"}})
    assert out[0]["response"]["metadata"] == {"n": 2}


def test_busy_error_is_swallowed_and_request_retried():
    tr = Translator()
    tr.up({"type": "response.create", "response": {"metadata": {"n": 1}}})
    tr.down({"type": "response.created", "response": {"id": "r1"}})
    tr.after_item = True  # simulate a request Qwen nevertheless rejected
    rejected = {"type": "response.create", "response": {"tools": [], "instructions": "keep me", "metadata": {"n": 2}}}
    tr.up(rejected)
    up, out = tr.down({"type": "error", "error": {"type": "invalid_request_error", "message": BUSY_ERROR}})
    assert (up, out) == ([], [])
    up, out = tr.down({"type": "response.done", "response": {"id": "r1"}})
    assert out[0]["response"]["metadata"] == {"n": 1}
    assert up == [rejected]  # resent whole, not just its metadata


def test_other_errors_pass_through():
    tr = Translator()
    _, out = tr.down({"type": "error", "error": {"type": "invalid_request_error", "message": "Invalid voice"}})
    assert out[0]["type"] == "error"


# ---- socket-level: OWUI-like client -> proxy -> fake upstream replaying the recording

async def fake_upstream(request):
    assert request.headers["Authorization"] == AUTH + "caller-key"  # passed through unchanged
    assert request.query["model"] == "qwen3.8-omni-flash-realtime"
    ws = web.WebSocketResponse()
    await ws.prepare(request)
    # Emit recorded downstream events in order; advance past each upstream event when the proxy sends it.
    idx = 0

    async def emit_until_next_up():
        nonlocal idx
        while idx < len(ROWS) and ROWS[idx]["dir"] == "down":
            await ws.send_str(json.dumps(ROWS[idx]["ev"]))
            idx += 1

    await emit_until_next_up()
    async for msg in ws:
        if msg.type != WSMsgType.TEXT:
            break
        ev = json.loads(msg.data)
        while idx < len(ROWS) and ROWS[idx]["dir"] == "up" and ROWS[idx]["ev"]["type"] == "conversation.item.truncate":
            idx += 1  # proxy never forwards truncate
        assert idx < len(ROWS) and ROWS[idx]["dir"] == "up", ev["type"]
        assert ROWS[idx]["ev"]["type"] == ev["type"]
        idx += 1
        await emit_until_next_up()
    return ws


@pytest.mark.asyncio
async def test_socket_roundtrip_against_fake_upstream():
    up_app = web.Application()
    up_app.router.add_get("/api-ws/v1/realtime", fake_upstream)
    up_server = TestServer(up_app)
    await up_server.start_server()
    cfg = {"upstream": str(up_server.make_url("/api-ws/v1/realtime")).replace("http", "ws", 1),
           "default_model": "x", "status_prefix": ""}
    client = TestClient(TestServer(make_app(cfg)))
    await client.start_server()
    try:
        bad = await client.get("/v1/realtime?model=qwen3.8-omni-flash-realtime")
        assert bad.status == 401  # no bearer: refused before any upstream connection
        empty = await client.get("/v1/realtime", headers={"Authorization": AUTH.strip()})
        assert empty.status == 401
        ws = await client.ws_connect("/v1/realtime?model=qwen3.8-omni-flash-realtime",
                                     headers={"Authorization": AUTH + "caller-key"})
        received = []

        async def reader():
            async for msg in ws:
                received.append(json.loads(msg.data))

        rt = asyncio.create_task(reader())
        for row in ROWS:
            if row["dir"] == "up":
                await asyncio.sleep(0.02)
                await ws.send_str(json.dumps(row["ev"]))
        await asyncio.sleep(0.3)
        await ws.close()
        rt.cancel()
        kinds = [e["type"] for e in received]
        assert kinds[0] == "session.created" and "session.updated" in kinds
        assert kinds.count("response.created") == 3 and kinds.count("response.done") == 3
        assert "conversation.item.truncated" in kinds and "error" not in kinds
        assert all(e["response"]["metadata"] for e in received if e["type"] == "response.created")
    finally:
        await client.close()
        await up_server.close()


def test_config_requires_only_upstream_url_and_holds_no_credentials():
    with pytest.raises(SystemExit) as exc:
        config_from_env({"UPSTREAM_URL": "  "})
    assert str(exc.value) == "missing required environment variables: UPSTREAM_URL"
    with pytest.raises(SystemExit) as exc:
        config_from_env({"UPSTREAM_URL": "https://x"})
    assert "wss://" in str(exc.value)
    cfg = config_from_env({"UPSTREAM_URL": "wss://x/r"})
    assert cfg == {"upstream": "wss://x/r", "default_model": "qwen3.8-omni-flash-realtime", "status_prefix": "请用中文说。"}


@pytest.mark.asyncio
async def test_upstream_refusal_status_is_returned_to_caller():
    async def refuse(request):
        return web.Response(status=401)
    up_app = web.Application(); up_app.router.add_get("/r", refuse)
    up = TestServer(up_app); await up.start_server()
    cfg = {"upstream": str(up.make_url("/r")).replace("http", "ws", 1), "default_model": "m", "status_prefix": ""}
    client = TestClient(TestServer(make_app(cfg))); await client.start_server()
    try:
        r = await client.get("/v1/realtime", headers={"Authorization": AUTH + "bad-key", "Connection": "Upgrade",
                             "Upgrade": "websocket", "Sec-WebSocket-Version": "13", "Sec-WebSocket-Key": "dGhlIHNhbXBsZSBub25jZQ=="})
        assert r.status == 401
    finally:
        await client.close(); await up.close()


def test_scrub_keeps_error_text_but_not_credentials():
    assert scrub("InternalError: Internal service error, please retry.") == "InternalError: Internal service error, please retry."
    fake_key = "sk" + "-abcdef123456"
    out = scrub("bad key " + fake_key + " or " + AUTH + "xyz.secret.value, api_key=foo, token " + "A" * 40)
    assert fake_key not in out and "xyz.secret" not in out and "foo" not in out and "A" * 40 not in out
    assert scrub("x" * 500 + "\n") == "<redacted>"
    assert len(scrub("word " * 100)) == 200
    assert scrub(None) is None


OVERLAP = [json.loads(line) for line in (FIXTURE.parent / "overlap-internalerror.jsonl").read_text().splitlines()]


def test_snapshot_delete_deferred_while_speech_or_response_in_flight():
    """Real session that ended in InternalError "previous item id does not exist" (second utterance began while the
    first turn's response was queued). Replay up to the point Qwen failed: the proxy must not have deleted the
    snapshot that the in-flight speech / queued response were anchored on."""
    tr = Translator()
    sent_deletes = []
    for row in OVERLAP:
        ev = json.loads(json.dumps(row["ev"]))
        if row["dir"] == "down" and ev["type"] == "error":
            break  # Qwen failed here in the recording
        up, _ = (tr.up if row["dir"] == "up" else tr.down)(ev)
        for out in up:
            if out["type"] == "conversation.item.delete":
                assert not tr.speaking and not tr.active, "delete sent while speech/response in flight"
                sent_deletes.append(out["item_id"])
    # The first delete (snapshot 1) happened in a quiet moment in the original run too... it must now wait as well,
    # because the first user turn's response was already requested when it arrived.
    culprit = "item_ZZ1uMp72iB3IHnyWw2bOn"  # snapshot 2, the id Qwen reported as missing
    assert culprit not in sent_deletes
    assert tr.deferred, "deletes are queued, not dropped"
    # Once every queued response has finished and no speech is open, the deferred deletes go out (no context leak).
    flushed = []
    while tr.pending:
        rid = f"r-late-{len(tr.pending)}"
        tr.down({"type": "response.created", "response": {"id": rid}})
        up, _ = tr.down({"type": "response.done", "response": {"id": rid, "status": "completed"}})
        flushed += [e["item_id"] for e in up if e["type"] == "conversation.item.delete"]
    assert culprit in flushed and not tr.deferred and not tr.speaking


def test_delete_goes_out_immediately_when_quiet():
    tr = Translator()
    tr.up({"type": "conversation.item.create", "item": {"id": "chat_context_1", "type": "message", "role": "system", "content": []}})
    tr.down({"type": "conversation.item.created", "item": {"id": "item_S1", "status": "completed", "role": "system"}})
    up, _ = tr.up({"type": "conversation.item.delete", "item_id": "chat_context_1"})
    assert up == [{"type": "conversation.item.delete", "item_id": "item_S1"}]


def test_delete_waits_for_open_speech_then_flushes_on_transcription():
    tr = Translator()
    tr.down({"type": "input_audio_buffer.speech_started", "item_id": "u1"})
    up, _ = tr.up({"type": "conversation.item.delete", "item_id": "x"})
    assert up == []
    up, _ = tr.down({"type": "conversation.item.input_audio_transcription.completed", "item_id": "u1", "transcript": "hi"})
    assert up == [{"type": "conversation.item.delete", "item_id": "x"}]


def _session(create_response):
    detection = {"type": "server_vad"} if create_response is None else {"type": "server_vad", "create_response": create_response}
    return {"type": "session.update", "session": {"type": "realtime", "audio": {"input": {"turn_detection": detection}}}}


def test_bare_response_create_dropped_when_client_drives_responses():
    """LiteLLM 1.106.0 sends {"type": "response.create"} after every transcription; Open WebUI sends its own with
    metadata. Only the client's must reach Qwen, or each turn is answered twice and metadata pairing drifts."""
    tr = Translator()
    tr.up(_session(False))
    assert tr.up({"type": "response.create"}) == ([], [])
    assert tr.up({"type": "response.create", "response": None}) == ([], [])
    up, _ = tr.up({"type": "response.create", "response": {"metadata": {"input_item_id": "u1"}}})
    assert len(up) == 1 and len(tr.pending) == 1
    _, out = tr.down({"type": "response.created", "response": {"id": "r1"}})
    assert out[0]["response"]["metadata"] == {"input_item_id": "u1"}


def test_bare_response_create_kept_when_server_or_client_left_auto_response_on():
    for create_response in (None, True):
        tr = Translator()
        tr.up(_session(create_response))
        up, _ = tr.up({"type": "response.create"})
        assert up == [{"type": "response.create"}]
