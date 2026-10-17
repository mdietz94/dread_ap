"""Opt-in Eden metadata compatibility; never alter the bundled sysmodule."""
from __future__ import annotations

import struct


def compatible_npdm(original: bytes) -> bytes:
    """Clear Eden 0.2.1's reserved debug bit in both capability arrays.

    Accept the shipped descriptor or an already compatible one. Validate all
    offsets before editing, and reject unfamiliar metadata rather than guessing.
    The returned bytes are private to the Eden destination; callers must not
    write them back to the shared prebuilt/source paths.
    """
    if len(original) < 0x80 or original[:4] != b"META":
        raise ValueError("Eden compatibility requires a valid META/NPDM header")
    positions = []
    for field, magic_offset, magic, cap_field in (
        (0x70, 0, b"ACI0", 0x30),
        (0x78, 0x200, b"ACID", 0x230),
    ):
        start, section_size = struct.unpack_from("<II", original, field)
        end = start + section_size
        if (start < 0x80 or end > len(original)
                or section_size < cap_field + 8
                or original[start + magic_offset:start + magic_offset + 4] != magic):
            raise ValueError(f"Invalid {magic.decode()} section in Eden NPDM")
        offset, size = struct.unpack_from("<II", original, start + cap_field)
        if offset < cap_field + 8 or size % 4 or start + offset + size > end:
            raise ValueError(f"Invalid {magic.decode()} capability array in Eden NPDM")
        debug = []
        for pos in range(start + offset, start + offset + size, 4):
            cap = struct.unpack_from("<I", original, pos)[0]
            # DebugFlags has exactly sixteen trailing ones (bit 16 is zero).
            if cap & 0x1ffff == 0xffff:
                if cap not in (0x0008ffff, 0x0000ffff):
                    raise ValueError("Unsupported Eden NPDM debug capability")
                debug.append(pos)
        if len(debug) != 1:
            raise ValueError(f"Expected one debug capability in {magic.decode()}")
        positions.extend(debug)
    if len(set(positions)) != 2:
        raise ValueError("Overlapping Eden NPDM capability arrays")
    result = bytearray(original)
    for pos in positions:
        struct.pack_into("<I", result, pos, 0x0000ffff)
    return bytes(result)
