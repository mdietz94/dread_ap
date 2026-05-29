"""Phase 1 — exlaunch wire-up validation.

Connects to a Metroid Dread 2.1.0 Switch (or Ryujinx) running the upstream
``open-dread-rando-exlaunch`` sysmodule, speaks the exact wire protocol used
by ``randovania.game_connection.executor.dread_executor``, and prints what
came back.

If this script succeeds end-to-end the rest of the dread_ap plan stands up
without further Switch-side reverse engineering. If it fails on real
hardware we stop and triage before doing any more work.

The wire protocol (confirmed from the exlaunch C++ sender in
vendor/open-dread-rando-exlaunch/source/program/{remote_api,main}.cpp; MIT):

Requests (PC → Switch):
  * PACKET_HANDSHAKE        b"\\x01" + interests_byte
  * PACKET_REMOTE_LUA_EXEC  b"\\x03" + len.to_bytes(4, "little") + utf-8_lua
  * PACKET_KEEP_ALIVE       b"\\x04"

Replies (Switch → PC) — every frame begins with a 1-byte PacketType:
  * HANDSHAKE ack       [0x01][req_num:1]
  * REMOTE_LUA_EXEC     [0x03][req_num:1][success:1][len:3 LE u24][payload]
  * push frames (0x02/0x05/0x06/0x07/0x08)
                        [type:1][len:4 LE u32][payload]
  * MALFORMED           [0x09][failing_type:1][rcv:4 LE u32][should:4 LE u32]

The ``IntEnum(b"1")`` trick in upstream evaluates as int("1")==1, so the
wire bytes are 0x01/0x02/0x03 etc., NOT ASCII '1'/'2'/'3'.

Usage:
    python scripts/phase1_validate.py <switch-ip>
"""
from __future__ import annotations

import argparse
import asyncio
import struct
import sys

PORT = 6969

# Packet type bytes (subset; full enum lives in
# apworld/dread_archipelago/client/lua_packets.py).
PACKET_HANDSHAKE = 0x01
PACKET_REMOTE_LUA_EXEC = 0x03
PACKET_LOG_MESSAGE = 0x02
PACKET_NEW_INVENTORY = 0x05
PACKET_COLLECTED_INDICES = 0x06
PACKET_RECEIVED_PICKUPS = 0x07
PACKET_GAME_STATE = 0x08
PACKET_MALFORMED = 0x09

PUSH_TYPES = {
    PACKET_LOG_MESSAGE, PACKET_NEW_INVENTORY, PACKET_COLLECTED_INDICES,
    PACKET_RECEIVED_PICKUPS, PACKET_GAME_STATE,
}

INTEREST_MULTIWORLD = 2

READ_TIMEOUT = 15.0
DRAIN_TIMEOUT = 30.0


def build_handshake(interests: int) -> bytes:
    return bytes([PACKET_HANDSHAKE, interests])


def build_lua_exec(source: str) -> bytes:
    payload = source.encode("utf-8")
    return bytes([PACKET_REMOTE_LUA_EXEC]) + len(payload).to_bytes(4, "little") + payload


async def read_frame(reader: asyncio.StreamReader) -> tuple[int, bool, bytes]:
    """Read one Switch→PC frame. Returns (packet_type, success, payload).

    For push frames and handshake ack, success is synthesized True since
    the wire carries no success bit for those types."""
    type_byte = await asyncio.wait_for(reader.readexactly(1), timeout=READ_TIMEOUT)
    ptype = type_byte[0]

    if ptype == PACKET_HANDSHAKE:
        await asyncio.wait_for(reader.readexactly(1), timeout=READ_TIMEOUT)  # req_num
        return ptype, True, b""

    if ptype == PACKET_REMOTE_LUA_EXEC:
        header = await asyncio.wait_for(reader.readexactly(5), timeout=READ_TIMEOUT)
        success = bool(header[1])
        (length,) = struct.unpack("<l", header[2:5] + b"\x00")
        payload = b""
        if length > 0:
            payload = await asyncio.wait_for(reader.readexactly(length), timeout=READ_TIMEOUT)
        return ptype, success, payload

    if ptype in PUSH_TYPES:
        length_bytes = await asyncio.wait_for(reader.readexactly(4), timeout=READ_TIMEOUT)
        (length,) = struct.unpack("<l", length_bytes)
        payload = b""
        if length > 0:
            payload = await asyncio.wait_for(reader.readexactly(length), timeout=READ_TIMEOUT)
        return ptype, True, payload

    if ptype == PACKET_MALFORMED:
        body = await asyncio.wait_for(reader.readexactly(9), timeout=READ_TIMEOUT)
        return ptype, False, body

    raise OSError(f"unknown PacketType 0x{ptype:02x} from Switch")


async def read_lua_exec_reply(reader: asyncio.StreamReader) -> tuple[bool, bytes]:
    """Read one frame, asserting it's a Lua-exec reply.

    Skips any pushes that arrive first."""
    while True:
        ptype, success, payload = await read_frame(reader)
        if ptype == PACKET_REMOTE_LUA_EXEC:
            return success, payload
        if ptype in PUSH_TYPES:
            print(f"  (skipping push 0x{ptype:02x} {len(payload)} bytes while waiting for reply)")
            continue
        raise OSError(f"unexpected frame type 0x{ptype:02x} while waiting for lua-exec reply")


async def lua_eval(reader: asyncio.StreamReader, writer: asyncio.StreamWriter, source: str) -> tuple[bool, bytes]:
    writer.write(build_lua_exec(source))
    await asyncio.wait_for(writer.drain(), timeout=DRAIN_TIMEOUT)
    return await read_lua_exec_reply(reader)


async def main(host: str) -> int:
    print(f"[phase1] connecting to {host}:{PORT}")
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, PORT), timeout=10.0
        )
    except (OSError, asyncio.TimeoutError) as exc:
        print(f"[phase1] FAIL connect: {exc}")
        print("  -> Switch isn't reachable or exlaunch isn't listening on :6969.")
        print("     Verify (1) the IP, (2) the exlaunch sysmodule installed, (3) Dread is in the title screen or in-game.")
        return 2

    print("[phase1] connected; sending PACKET_HANDSHAKE(interests=MULTIWORLD)")
    writer.write(build_handshake(INTEREST_MULTIWORLD))
    try:
        await asyncio.wait_for(writer.drain(), timeout=DRAIN_TIMEOUT)
        ptype, success, payload = await read_frame(reader)
    except (OSError, asyncio.TimeoutError, asyncio.IncompleteReadError) as exc:
        print(f"[phase1] FAIL handshake read: {exc}")
        return 3
    if ptype != PACKET_HANDSHAKE:
        print(f"[phase1] FAIL handshake: expected 0x01, got 0x{ptype:02x}")
        return 3
    print(f"[phase1] handshake ack received")

    print("[phase1] T1: bare Lua eval (does the runtime answer at all?)")
    try:
        success, payload = await lua_eval(reader, writer, "return 1+1")
    except (OSError, asyncio.TimeoutError, asyncio.IncompleteReadError) as exc:
        print(f"[phase1] FAIL T1 read: {exc}")
        return 4
    print(f"  T1 success={success} payload={payload!r}")
    if not success or payload not in (b"2", b"2.0"):
        print("  -> Lua runtime didn't return 2 from `return 1+1`. Exlaunch may not be patched in.")
        return 5

    print("[phase1] T2: does the Randovania `RL` namespace exist?")
    try:
        success, payload = await lua_eval(reader, writer, "return type(RL)")
    except (OSError, asyncio.TimeoutError, asyncio.IncompleteReadError) as exc:
        print(f"[phase1] FAIL T2 read: {exc}")
        return 6
    print(f"  T2 success={success} payload={payload!r}")
    has_rl = success and payload == b"table"
    if not has_rl:
        print("  -> `RL` is not a table. Game likely isn't patched with open-dread-rando")
        print("     (bootstrap_part_*.lua never ran). T3/T4 will be skipped.")
        return _close_with_status(writer, success=True)

    print("[phase1] T3: Randovania-style API-version handshake")
    api_query = (
        "return string.format('%d,%d,%s,%s,%s', RL.Version, RL.BufferSize,"
        "tostring(RL.Bootstrap), Init.sLayoutUUID, GameVersion)"
    )
    try:
        success, payload = await lua_eval(reader, writer, api_query)
    except (OSError, asyncio.TimeoutError, asyncio.IncompleteReadError) as exc:
        print(f"[phase1] FAIL T3 read: {exc}")
        return 7
    print(f"  T3 success={success} payload={payload!r}")
    if success:
        try:
            api_version, buffer_size, bootstrap, uuid, version = payload.decode("ascii").split(",")
            print(f"    api_version  = {api_version}")
            print(f"    buffer_size  = {buffer_size}")
            print(f"    bootstrap    = {bootstrap}")
            print(f"    layout_uuid  = {uuid}")
            print(f"    game_version = {version}")
        except ValueError:
            print(f"  -> couldn't parse: {payload!r}")

    print("[phase1] T4: read current inventory bitfield (RL.GetInventoryAndSend)")
    try:
        success, payload = await lua_eval(reader, writer, "RL.GetInventoryAndSend()")
    except (OSError, asyncio.TimeoutError, asyncio.IncompleteReadError) as exc:
        print(f"[phase1] FAIL T4 read: {exc}")
        return 8
    print(f"  T4 success={success} payload_bytes={len(payload)}")
    print(f"    (RL.GetInventoryAndSend issues a separate PACKET_NEW_INVENTORY message)")

    return _close_with_status(writer, success=True)


def _close_with_status(writer: asyncio.StreamWriter, success: bool) -> int:
    writer.close()
    print("[phase1] " + ("OK — wire is up. Proceed to Phase 2." if success else "FAIL — see errors above."))
    return 0 if success else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("host", help="Switch (or Ryujinx) IP address")
    args = parser.parse_args()
    sys.exit(asyncio.run(main(args.host)))
