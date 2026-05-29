"""Unit tests for lua_packets — pure functions, no network.

Run with:  python -m pytest apworld/dread/tests/test_lua_packets.py -v
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))

from dread.client import lua_packets as lp  # noqa: E402


def test_encode_handshake_multiworld():
    assert lp.encode_handshake(lp.ClientInterest.MULTIWORLD) == b"\x01\x02"


def test_encode_handshake_logging_or_multiworld():
    assert lp.encode_handshake(lp.ClientInterest.LOGGING | lp.ClientInterest.MULTIWORLD) == b"\x01\x03"


def test_encode_handshake_rejects_out_of_range():
    with pytest.raises(ValueError):
        lp.encode_handshake(0x100)


def test_encode_lua_exec_short():
    out = lp.encode_lua_exec("return 1+1")
    assert out[0:1] == b"\x03"
    assert out[1:5] == (10).to_bytes(4, "little")
    assert out[5:] == b"return 1+1"


def test_encode_lua_exec_utf8():
    src = "return '€'"  # 5-byte UTF-8 encoding
    out = lp.encode_lua_exec(src)
    expected_payload = src.encode("utf-8")
    assert out[0:1] == b"\x03"
    assert out[1:5] == len(expected_payload).to_bytes(4, "little")
    assert out[5:] == expected_payload


def test_encode_lua_exec_empty():
    out = lp.encode_lua_exec("")
    assert out == b"\x03\x00\x00\x00\x00"


def test_encode_keep_alive():
    assert lp.encode_keep_alive() == b"\x04"


def test_parse_lua_exec_reply_header_success_zero_length():
    # After the 0x03 type byte: [req_num=7][success=1][len=0,0,0]
    success, length = lp.parse_lua_exec_reply_header(b"\x07\x01\x00\x00\x00")
    assert success is True
    assert length == 0


def test_parse_lua_exec_reply_header_success_with_payload():
    success, length = lp.parse_lua_exec_reply_header(b"\x00\x01\x05\x00\x00")
    assert success is True
    assert length == 5


def test_parse_lua_exec_reply_header_failure():
    success, length = lp.parse_lua_exec_reply_header(b"\x42\x00\x10\x00\x00")
    assert success is False
    assert length == 16


def test_parse_lua_exec_reply_header_max_three_byte_length():
    # 0x00FFFFFF — the maximum a 3-byte little-endian uint can hold
    success, length = lp.parse_lua_exec_reply_header(b"\x00\x01\xff\xff\xff")
    assert success is True
    assert length == 0x00FFFFFF


def test_parse_lua_exec_reply_header_rejects_bad_size():
    with pytest.raises(ValueError, match="lua-exec reply header must be"):
        lp.parse_lua_exec_reply_header(b"\x01\x00\x00")


def test_parse_push_length_zero():
    assert lp.parse_push_length(b"\x00\x00\x00\x00") == 0


def test_parse_push_length_full_u32():
    # 4-byte u32, not the 3-byte u24 used in lua-exec replies
    assert lp.parse_push_length(b"\xff\xff\xff\x00") == 0x00FFFFFF
    assert lp.parse_push_length(b"\x10\x00\x01\x00") == 0x00010010


def test_parse_push_length_rejects_bad_size():
    with pytest.raises(ValueError, match="push length must be"):
        lp.parse_push_length(b"\x00\x00")


def test_parse_malformed_body():
    body = bytes([0x03]) + (5).to_bytes(4, "little") + (8).to_bytes(4, "little")
    failing_type, received, should = lp.parse_malformed_body(body)
    assert failing_type == 0x03
    assert received == 5
    assert should == 8


def test_packet_type_values_match_wire():
    assert int(lp.PacketType.HANDSHAKE) == 0x01
    assert int(lp.PacketType.LOG_MESSAGE) == 0x02
    assert int(lp.PacketType.REMOTE_LUA_EXEC) == 0x03
    assert int(lp.PacketType.KEEP_ALIVE) == 0x04
    assert int(lp.PacketType.NEW_INVENTORY) == 0x05
    assert int(lp.PacketType.COLLECTED_INDICES) == 0x06
    assert int(lp.PacketType.RECEIVED_PICKUPS) == 0x07
    assert int(lp.PacketType.GAME_STATE) == 0x08
    assert int(lp.PacketType.MALFORMED) == 0x09


def test_push_and_reply_type_sets_are_disjoint():
    assert lp.PUSH_TYPES.isdisjoint(lp.REPLY_TYPES)
