"""Native OS folder picker for the ``/patch`` command.

Kept Kivy-free and in its own module so (a) it never gets imported at apworld
load time (generation hosts have no display server) and (b) tests can mock
``ask_directory`` without standing up tkinter.

tkinter ships with standard CPython, but the frozen Archipelago launcher may
not bundle it — callers must catch :class:`FileDialogUnavailable` and fall back
to the text-argument form of ``/patch``.
"""
from __future__ import annotations

from typing import Optional


class FileDialogUnavailable(RuntimeError):
    """Raised when no native file dialog backend (tkinter) is importable."""


def ask_directory(title: str, initialdir: Optional[str] = None) -> Optional[str]:
    """Open a native "choose a folder" dialog and return the chosen path.

    Returns ``None`` if the user cancels. Raises :class:`FileDialogUnavailable`
    if tkinter can't be imported. Blocking — call via ``asyncio.to_thread`` so
    it doesn't stall the event loop.

    A fresh hidden ``Tk`` root is created and destroyed per call: the client's
    asyncio loop owns the main thread, so we can't reuse a long-lived root, and
    leaving one alive would leak a window. ``-topmost`` ensures the dialog
    surfaces above the Kivy window."""
    try:
        import tkinter
        from tkinter import filedialog
    except Exception as exc:  # ImportError, or a broken Tcl/Tk install
        raise FileDialogUnavailable(str(exc)) from exc

    root = tkinter.Tk()
    try:
        root.withdraw()
        root.attributes("-topmost", True)
        chosen = filedialog.askdirectory(
            title=title,
            initialdir=initialdir or "",
            mustexist=True,
            parent=root,
        )
    finally:
        root.destroy()
    # askdirectory returns "" on cancel.
    return chosen or None
