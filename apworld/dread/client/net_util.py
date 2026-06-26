"""Network helpers — lifted verbatim from smo_archipelago.

``detect_lan_ip`` powers both the deploy-time ``rom:/ap_config.json``
``bridge_host`` write (the Switch sysmodule reads this as the /24 subnet
sweep seed — there is no compile-time bake) and the runtime UDP discovery
reply payload (so the Switch dials our actual LAN IP).
"""

from __future__ import annotations

import socket

_PROBE_HOST = "8.8.8.8"
_PROBE_PORT = 80

_LOOPBACK = "127.0.0.1"


def detect_lan_ip() -> str:
    """Best-effort LAN IP. Returns 127.0.0.1 when no usable interface
    is available. Never sends a packet — ``connect()`` on a UDP socket
    only triggers kernel route resolution."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect((_PROBE_HOST, _PROBE_PORT))
        ip, _port = s.getsockname()
        if ip and not ip.startswith("0."):
            return ip
        return _LOOPBACK
    except OSError:
        return _LOOPBACK
    finally:
        s.close()


def is_plausible_ipv4(s: str) -> bool:
    if not s:
        return False
    parts = s.split(".")
    if len(parts) != 4:
        return False
    for p in parts:
        if not p or not p.isdigit():
            return False
        n = int(p)
        if n < 0 or n > 255:
            return False
    return True
