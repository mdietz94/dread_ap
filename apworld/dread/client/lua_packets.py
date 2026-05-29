"""Frame encode / decode for the open-dread-rando-exlaunch wire protocol.

Replaces SMO's ``protocol.py`` (line-delimited JSON over TCP).

Authoritative source for the wire format is the exlaunch C++ sender:
  vendor/open-dread-rando-exlaunch/source/program/remote_api.cpp
  vendor/open-dread-rando-exlaunch/source/program/main.cpp
The upstream Randovania Python parser is at:
  https://github.com/randovania/randovania/blob/main/randovania/game_connection/executor/dread_executor.py

Every Switch→PC frame begins with a 1-byte ``PacketType``. After that, the
layout depends on the type:

  HANDSHAKE (0x01)         [0x01][req_num:1]                                  2 bytes total
  REMOTE_LUA_EXEC (0x03)   [0x03][req_num:1][success:1][len:3 LE u24][payload]
  LOG_MESSAGE (0x02)       \\
  NEW_INVENTORY (0x05)      |
  COLLECTED_INDICES (0x06)  +-- push: [type:1][len:4 LE u32][payload]
  RECEIVED_PICKUPS (0x07)   |
  GAME_STATE (0x08)        /
  MALFORMED (0x09)         [0x09][failing_type:1][received:4 LE u32][should:4 LE u32]

PC→Switch requests are emitted by ``encode_*`` below; we never see ``KEEP_ALIVE``
or our own requests come back.

The upstream ``IntEnum(b"1")`` trick evaluates as ``int("1") == 1``, so wire
bytes are ``0x01`` … ``0x09`` (raw), not ASCII '1' … '9'.
"""
from __future__ import annotations

import enum
import re
import struct
from dataclasses import dataclass
from typing import Optional


class PacketType(enum.IntEnum):
    HANDSHAKE = 0x01
    LOG_MESSAGE = 0x02
    REMOTE_LUA_EXEC = 0x03
    KEEP_ALIVE = 0x04
    NEW_INVENTORY = 0x05
    COLLECTED_INDICES = 0x06
    RECEIVED_PICKUPS = 0x07
    GAME_STATE = 0x08
    MALFORMED = 0x09


# Frame types the Switch sends unsolicited (i.e. NOT a reply to a request
# the PC just issued). They share a fixed shape: [type][len:4 LE u32][payload].
PUSH_TYPES = frozenset({
    PacketType.LOG_MESSAGE,
    PacketType.NEW_INVENTORY,
    PacketType.COLLECTED_INDICES,
    PacketType.RECEIVED_PICKUPS,
    PacketType.GAME_STATE,
})

# Frame types the Switch sends as a reply to a request we just issued.
REPLY_TYPES = frozenset({
    PacketType.HANDSHAKE,
    PacketType.REMOTE_LUA_EXEC,
})


class ClientInterest(enum.IntFlag):
    """Sent in the PACKET_HANDSHAKE body. Bitwise-OR to combine."""
    LOGGING = 0x01
    MULTIWORLD = 0x02


# Tail lengths after the 1-byte type prefix has already been consumed.
HANDSHAKE_REPLY_TAIL = 1            # [req_num]
LUA_EXEC_REPLY_HEADER = 5           # [req_num][success][len:3 LE u24]
PUSH_LENGTH_PREFIX = 4              # [len:4 LE u32]
MALFORMED_BODY = 9                  # [failing_type][rcv:4][should:4]


@dataclass(frozen=True)
class Response:
    """Logical decoded result of one Switch→PC frame.

    For ``REMOTE_LUA_EXEC`` replies, ``success`` is the wire success bit.
    For pushes and handshake acks, the wire carries no success bit; we
    synthesize ``success=True`` so call sites can treat all frames uniformly.
    For ``MALFORMED`` we synthesize ``success=False`` and stash the body in
    ``payload`` for diagnostics."""
    success: bool
    payload: bytes


# ---- Request encoders (PC → Switch) ---------------------------------------

def encode_handshake(interests: int) -> bytes:
    """``[0x01][interests_byte]`` — no length prefix."""
    if not 0 <= interests <= 0xFF:
        raise ValueError(f"interests must fit in one byte, got {interests}")
    return bytes([PacketType.HANDSHAKE, interests])


def encode_lua_exec(source: str) -> bytes:
    """``[0x03][len:4 LE u32][utf-8 lua source]``."""
    payload = source.encode("utf-8")
    return bytes([PacketType.REMOTE_LUA_EXEC]) + len(payload).to_bytes(4, "little") + payload


def encode_keep_alive() -> bytes:
    """``[0x04]`` — single-byte ping; the Switch never replies to it."""
    return bytes([PacketType.KEEP_ALIVE])


# ---- Response decoders (Switch → PC, after the type byte) -----------------
#
# Each parser expects the leading PacketType byte to have ALREADY been read
# by the caller. The caller dispatches based on that byte, then reads the
# appropriate number of trailing bytes, then hands them to the right parser.

def parse_lua_exec_reply_header(header: bytes) -> tuple[bool, int]:
    """Decode the 5 bytes after a 0x03 prefix: ``[req_num][success][len:3 LE u24]``.

    Returns ``(success, payload_length)``. The request_number is consumed
    but not returned — upstream uses it to detect frame reordering, but
    our single-pending-future design serializes Lua-exec requests through
    a lock, so we already match replies positionally.

    Length is a 3-byte little-endian unsigned int; we decode it as signed
    ``<l`` with a synthetic zero high byte (same trick upstream uses) so
    that a negative length surfaces immediately instead of getting masked
    into a giant positive number."""
    if len(header) != LUA_EXEC_REPLY_HEADER:
        raise ValueError(
            f"lua-exec reply header must be {LUA_EXEC_REPLY_HEADER} bytes, got {len(header)}"
        )
    success = bool(header[1])
    (length,) = struct.unpack("<l", header[2:5] + b"\x00")
    if length < 0:
        raise ValueError(f"negative lua-exec response length: {length}")
    return success, length


def parse_push_length(length_field: bytes) -> int:
    """Decode the 4 bytes after a push-type prefix: ``[len:4 LE u32]``."""
    if len(length_field) != PUSH_LENGTH_PREFIX:
        raise ValueError(
            f"push length must be {PUSH_LENGTH_PREFIX} bytes, got {len(length_field)}"
        )
    (length,) = struct.unpack("<l", length_field)
    if length < 0:
        raise ValueError(f"negative push payload length: {length}")
    return length


_RECEIVED_PICKUPS_DIGITS = re.compile(rb"-?\d+")


def parse_received_pickups_count(payload: bytes) -> Optional[int]:
    """Decode a ``PACKET_RECEIVED_PICKUPS`` payload into the game's confirmed
    ``Blackboard.ReceivedPickups`` counter.

    The bootstrap we send emits this as ``RL.SendReceivedPickups(tostring(
    RL.ReceivedPickups()))`` (``bootstrap_part_2.lua``) — i.e. a bare decimal
    string, the same family as the JSON ``NEW_INVENTORY`` and the
    ``locations:``-prefixed ``COLLECTED_INDICES`` pushes. We still parse
    defensively (first run of decimal digits, tolerating any prefix) and return
    ``None`` on a non-integer payload so callers log-and-skip rather than crash.
    This count is the delivery cursor — see ``DreadContext._handle_received_pickups``
    and [[dread-delivery-protocol]]."""
    m = _RECEIVED_PICKUPS_DIGITS.search(payload)
    if m is None:
        return None
    return int(m.group(0))


def parse_malformed_body(body: bytes) -> tuple[int, int, int]:
    """Decode the 9 bytes after a 0x09 prefix.

    Returns ``(failing_type, received_bytes, should_bytes)``. Diagnostic
    only — surfaced as a warning when the Switch reports we sent it junk."""
    if len(body) != MALFORMED_BODY:
        raise ValueError(
            f"malformed body must be {MALFORMED_BODY} bytes, got {len(body)}"
        )
    failing_type = body[0]
    (received,) = struct.unpack("<l", body[1:5])
    (should,) = struct.unpack("<l", body[5:9])
    return failing_type, received, should
