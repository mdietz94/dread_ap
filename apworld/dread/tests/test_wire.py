"""Unit tests for the line-delimited JSON wire envelope.

Verifies encode/decode roundtrips, ``iter_lines`` framing, and ``from_dict``
dispatch — the lowest layer of the new bridge protocol.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))

from dread.client import wire as W  # noqa: E402


def test_encode_emits_json_terminated_by_newline():
    msg = W.Hello(mod_ver="x", dread_ver="2.1.0",
                  layout_uuid="abc", device_id="sw-1")
    data = W.encode(msg)
    assert data.endswith(b"\n")
    assert b'"t":"hello"' in data
    assert b'"mod_ver":"x"' in data


def test_encode_strips_none_fields():
    """HelloAck.err defaults to None; it should not appear in the JSON output."""
    msg = W.HelloAck(ok=True, slot="Samus", seed="x")
    data = W.encode(msg)
    assert b'"err"' not in data


def test_decode_roundtrip():
    msg = W.LuaExec(seq=42, src='return "hi"')
    data = W.encode(msg)
    # encoded includes the trailing newline; decode wants the line without it
    decoded = W.decode(data[:-1])
    assert decoded == {"t": "lua_exec", "seq": 42, "src": 'return "hi"'}


def test_from_dict_instantiates_correct_class():
    d = {"t": "collected", "hex": "deadbeef"}
    obj = W.from_dict(d)
    assert isinstance(obj, W.Collected)
    assert obj.hex == "deadbeef"


def test_from_dict_unknown_t_returns_none():
    assert W.from_dict({"t": "nope"}) is None


def test_from_dict_tolerates_extra_fields():
    """Forward-compatible: an updated Switch may send fields older PCs don't
    know about — those extras get ignored, not raised."""
    d = {"t": "hello", "mod_ver": "v1", "future_field": 123}
    obj = W.from_dict(d)
    assert isinstance(obj, W.Hello)
    assert obj.mod_ver == "v1"


def test_iter_lines_yields_complete_lines_only():
    buf = bytearray(b'{"t":"ping","ts_ms":1}\n{"t":"pong","ts_ms":2}\npar')
    lines = list(W.iter_lines(buf))
    assert len(lines) == 2
    assert lines[0] == b'{"t":"ping","ts_ms":1}'
    assert lines[1] == b'{"t":"pong","ts_ms":2}'
    # incomplete trailing 'par' remains in the buffer
    assert bytes(buf) == b"par"


def test_iter_lines_drops_oversize_garbage_with_no_newline():
    """No \\n in 8KB+ data ⇒ buffer is dropped (corrupt stream resync)."""
    buf = bytearray(b"x" * (W.MAX_LINE_BYTES + 1024))
    list(W.iter_lines(buf))
    assert len(buf) == 0


def test_encode_rejects_oversize():
    big = W.Log(msg="x" * (W.MAX_LINE_BYTES + 10))
    with pytest.raises(ValueError):
        W.encode(big)


def test_response_from_reply():
    reply = W.LuaExecReply(seq=1, ok=True, result="42")
    r = W.response_from_reply(reply)
    assert r.success is True
    assert r.payload == b"42"
