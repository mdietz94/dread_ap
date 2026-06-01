"""On-wire JSON envelope for the Switch ↔ PC bridge.

One persistent TCP connection per Switch (Switch is the dialer). Each message
is one line of UTF-8 JSON terminated by ``\\n``; ``t`` is the type discriminator.
Max line: 8 KiB (a single bootstrap Lua chunk targets ≤4 KiB, plus envelope).

The Lua-eval RPC of the prior binary protocol is preserved as a typed message
(``LuaExec`` / ``LuaExecReply``) — the entire Randovania ``RL.*`` bootstrap
layer is unchanged, only the transport.
"""
from __future__ import annotations

import enum
import json
from dataclasses import dataclass, field, asdict
from typing import Any, Iterable, Optional

MAX_LINE_BYTES = 8 * 1024


class WireType(str, enum.Enum):
    HELLO = "hello"
    HELLO_ACK = "hello_ack"
    LUA_EXEC = "lua_exec"
    LUA_EXEC_REPLY = "lua_exec_reply"
    LOG = "log"
    INVENTORY = "inventory"
    COLLECTED = "collected"
    RECEIVED_PICKUPS = "received_pickups"
    GAME_STATE = "game_state"
    PING = "ping"
    PONG = "pong"
    KICK = "kick"
    LAYOUT_UUID = "layout_uuid"


# ---------------------------------------------------------------------------
# Switch → PC
# ---------------------------------------------------------------------------

@dataclass
class Hello:
    """First message after TCP connect. ``layout_uuid`` may be empty at HELLO
    time (the worker thread can't read Lua state); a separate ``LayoutUuid``
    push follows on the first poll once Lua land is up."""
    t: str = WireType.HELLO.value
    mod_ver: str = ""
    dread_ver: str = ""
    layout_uuid: str = ""
    device_id: str = ""


@dataclass
class LuaExecReply:
    t: str = WireType.LUA_EXEC_REPLY.value
    seq: int = 0
    ok: bool = False
    result: str = ""


@dataclass
class Log:
    t: str = WireType.LOG.value
    level: str = "info"
    msg: str = ""


@dataclass
class Inventory:
    """``index`` mirrors ``RL.InventoryIndex()``; ``inventory`` is the
    per-tracked-item amount array RL.GetInventoryAndSend composes in Lua."""
    t: str = WireType.INVENTORY.value
    index: int = 0
    inventory: list = field(default_factory=list)


@dataclass
class Collected:
    """``hex`` is the lowercase hex encoding of the pickup-collected bitfield
    (bit ``b`` of byte ``i`` ⇒ pickup_index ``i*8 + b`` collected). Hex picked
    over base64 because the Lua bootstrap's string composition is easier with
    ``string.format("%02x",...)`` than base64."""
    t: str = WireType.COLLECTED.value
    hex: str = ""


@dataclass
class ReceivedPickups:
    """``Blackboard.ReceivedPickups`` cursor. Drives the delivery state machine."""
    t: str = WireType.RECEIVED_PICKUPS.value
    count: int = 0


@dataclass
class GameState:
    t: str = WireType.GAME_STATE.value
    scenario: str = ""
    beaten: bool = False


@dataclass
class Pong:
    t: str = WireType.PONG.value
    ts_ms: int = 0


@dataclass
class LayoutUuid:
    """Pushed once on first poll after HELLO once Lua has populated
    ``Init.sLayoutUUID``. PC overwrites the connection's device_id with
    ``sw-<value tail>`` upon receipt."""
    t: str = WireType.LAYOUT_UUID.value
    value: str = ""


# ---------------------------------------------------------------------------
# PC → Switch
# ---------------------------------------------------------------------------

@dataclass
class HelloAck:
    """``subs`` advertises which push streams the PC wants. Currently both
    ``logging`` and ``multiWorld`` are always true; the field exists for
    future selective subscription. ``err`` populated only when ``ok=False``."""
    t: str = WireType.HELLO_ACK.value
    ok: bool = True
    slot: str = ""
    seed: str = ""
    subs: dict = field(default_factory=lambda: {"logging": True, "multiWorld": True})
    err: Optional[str] = None


@dataclass
class LuaExec:
    """``seq`` is a monotonically increasing counter; the Switch echoes it
    on the reply so concurrent ``run_lua`` callers can resolve their futures
    in arrival order without a per-connection lock."""
    t: str = WireType.LUA_EXEC.value
    seq: int = 0
    src: str = ""


@dataclass
class Ping:
    t: str = WireType.PING.value
    ts_ms: int = 0


@dataclass
class Kick:
    t: str = WireType.KICK.value
    reason: str = ""


# ---------------------------------------------------------------------------
# Back-compat shim — keeps context.py's existing ``Response`` callsite intact
# while we invert lifecycle. The old binary protocol returned a ``Response``
# from ``executor.run_lua``; the new ``BridgeServer.run_lua`` returns the
# same shape (translated from LuaExecReply).
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Response:
    success: bool
    payload: bytes


def response_from_reply(reply: LuaExecReply) -> Response:
    return Response(success=reply.ok, payload=reply.result.encode("utf-8"))


# ---------------------------------------------------------------------------
# (de)serialization
# ---------------------------------------------------------------------------

def _strip_none(d: dict) -> dict:
    return {k: v for k, v in d.items() if v is not None}


def encode(msg: Any) -> bytes:
    """Serialize one dataclass message to a UTF-8 ``json\\n`` line."""
    d = _strip_none(asdict(msg))
    line = json.dumps(d, separators=(",", ":"), ensure_ascii=False)
    if len(line.encode("utf-8")) > MAX_LINE_BYTES:
        raise ValueError(f"encoded message exceeds {MAX_LINE_BYTES} bytes")
    return (line + "\n").encode("utf-8")


def decode(line: bytes | str) -> dict:
    """Decode one wire line into a dict. Caller dispatches on ``t``."""
    if isinstance(line, bytes):
        line = line.decode("utf-8", errors="replace")
    return json.loads(line)


def iter_lines(buffer: bytearray) -> Iterable[bytes]:
    """Yield complete ``\\n``-terminated lines from ``buffer``; consume in place.

    Lines longer than MAX_LINE_BYTES are skipped (resync to next ``\\n``).
    If the buffer accumulates >MAX_LINE_BYTES of data with no newline at all,
    the whole buffer is dropped (corrupt stream — wait for next ``\\n``).
    """
    while True:
        nl = buffer.find(b"\n")
        if nl < 0:
            if len(buffer) > MAX_LINE_BYTES:
                buffer.clear()
            return
        line = bytes(buffer[:nl])
        del buffer[: nl + 1]
        if len(line) > MAX_LINE_BYTES:
            continue
        if line.strip():
            yield line


# ---------------------------------------------------------------------------
# Dispatch helpers — map a decoded dict to its dataclass instance.
# ---------------------------------------------------------------------------

_CLASS_BY_T: dict[str, type] = {
    WireType.HELLO.value: Hello,
    WireType.HELLO_ACK.value: HelloAck,
    WireType.LUA_EXEC.value: LuaExec,
    WireType.LUA_EXEC_REPLY.value: LuaExecReply,
    WireType.LOG.value: Log,
    WireType.INVENTORY.value: Inventory,
    WireType.COLLECTED.value: Collected,
    WireType.RECEIVED_PICKUPS.value: ReceivedPickups,
    WireType.GAME_STATE.value: GameState,
    WireType.PING.value: Ping,
    WireType.PONG.value: Pong,
    WireType.KICK.value: Kick,
    WireType.LAYOUT_UUID.value: LayoutUuid,
}


def from_dict(d: dict) -> Any:
    """Instantiate the dataclass matching ``d['t']``. Unknown ``t`` → None.

    Extra fields are ignored; missing fields take dataclass defaults so a
    forward-compatible Switch can add fields without breaking older clients.
    """
    t = d.get("t")
    cls = _CLASS_BY_T.get(t)
    if cls is None:
        return None
    valid = {f for f in cls.__dataclass_fields__.keys()}
    kwargs = {k: v for k, v in d.items() if k in valid and k != "t"}
    return cls(**kwargs)
