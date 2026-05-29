"""Native OS folder picker for the ``/patch`` command.

Thin wrapper over Archipelago's ``Utils.open_directory`` — the same helper
every other AP client/launcher uses, so we get the real native dialog on each
platform (kdialog/zenity on Linux, the macOS Kivy-safe subprocess path, the
native folder dialog via tkinter on Windows) instead of Kivy's file-oriented
FileChooser. Kept in its own module so it's never imported at apworld load time
(generation hosts have no display server) and so tests can mock it.

``Utils`` is the AP runtime module, available in the Launcher subprocess the
client runs in. Callers must catch :class:`FileDialogUnavailable` (raised when
``Utils`` or its tkinter fallback can't load) and fall back to the
text-argument form of ``/patch``.
"""
from __future__ import annotations

from typing import Optional


class FileDialogUnavailable(RuntimeError):
    """Raised when no native file dialog backend is available."""


def ask_directory(title: str, initialdir: Optional[str] = None) -> Optional[str]:
    """Open a native "choose a folder" dialog and return the chosen path.

    Returns ``None`` if the user cancels (or no GUI is available). Raises
    :class:`FileDialogUnavailable` if the dialog backend can't load. Blocking —
    call via ``asyncio.to_thread`` so it doesn't stall the event loop."""
    try:
        from Utils import open_directory
    except Exception as exc:  # Utils not importable (no AP runtime on path)
        raise FileDialogUnavailable(f"Archipelago Utils unavailable: {exc}") from exc

    try:
        chosen = open_directory(title, suggest=initialdir or "")
    except Exception as exc:  # tkinter/Tcl missing inside open_directory
        raise FileDialogUnavailable(str(exc)) from exc

    # open_directory returns "" (tk) / None on cancel.
    return chosen or None
