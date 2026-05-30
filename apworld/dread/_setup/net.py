"""LAN-IP autodetect — unused stub kept for wizard import compatibility.

smo_archipelago bakes the PC's LAN IP into its subsdk9 at build time so the
in-game module can phone back to the bridge. Dread inverts that topology:
the PC connects TO the Switch's exlaunch sysmodule on :6969, so we never
need to know the PC's IP at build time.

The smo wizard imports `detect_lan_ip` + `is_plausible_ipv4` at module load
and references them in a few code paths (BridgeIpPage, the configure step's
log line) that the dread wizard surgery will eventually remove. Until that
surgery lands, these stubs let `from .net import ...` succeed and return
benign fallback values if the paths actually execute.
"""

from __future__ import annotations

import re

_DOTTED_QUAD = re.compile(r"^(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})$")


def detect_lan_ip() -> str:
    """Best-effort: returns "" (empty) on dread. The bridge-IP concept does
    not apply to dread's topology; the smo wizard reads this for a UI label
    that the dread version no longer renders."""
    return ""


def is_plausible_ipv4(s: str) -> bool:
    """True iff `s` looks like an IPv4 dotted quad with each octet 0-255."""
    m = _DOTTED_QUAD.match(s.strip())
    if not m:
        return False
    return all(0 <= int(g) <= 255 for g in m.groups())


__all__ = ["detect_lan_ip", "is_plausible_ipv4"]
