"""Kivy multi-page setup wizard for the Dread Archipelago client.

Entry point: ``run_setup_wizard(dreadap_path: str | None = None)``. Surfaced
via the ``/setup`` slash command in DreadClient (which spawns this in a new
window via ``launch_subprocess`` while DreadClient stays open). Covers both
first-time setup and re-runs — apworld updates, switching deploy targets
between Ryujinx / SD card / custom folder.

Pages (sequenced; each calls ``goto("next-page")`` when its work completes):

  1. WelcomePage      — overview of what the wizard does (exlaunch
                        sysmodule build + romfs patcher hookup)
  2. PrereqPage       — runs ``_setup.prereqs.check_all()``; auto-install
                        Python 3.12 via winget; link to devkitPro installer;
                        pip-install hint for ``open_dread_rando``
  3. RomFSPickerPage  — folder picker for the user's pre-extracted vanilla
                        Dread 2.1.0 romfs; persisted as ``romfs_path`` in
                        ``setup_state.json``. The per-seed patcher reads
                        it at AP-connect time (PR-A2 wires that path)
  4. BuildPage        — drives ``ensure_exlaunch_checkout`` →
                        ``apply_ryujinx_patch`` → ``run_exlaunch_build``;
                        streams each subprocess's log line-by-line
  5. DeployPage       — radio: Ryujinx / SD card / Custom folder;
                        auto-detects each; calls ``deploy_to_*``
  6. DonePage         — success banner + "Launch DreadClient" handoff

Smo-baseline pages this wizard intentionally drops:

  - ``DumpPickerPage``/``ExtractPage``: dread has no NSP extraction step.
    ``open_dread_rando`` overlays an already-extracted romfs at AP-connect.
  - ``BridgeIpPage``: dread inverts the wire topology — the PC dials the
    Switch on :6969, so the PC's LAN IP is never baked into the build.

Kivy is imported lazily INSIDE this module — never at apworld-import time —
because AP generation hosts (Linux servers running ``ap_generate.py``)
do not have Kivy installed. Anyone running the wizard already has Kivy
installed because DreadClient itself uses it.

The wizard is intentionally synchronous on the UI thread for navigation;
long-running shells (git clone, msys2 build) run in ``threading.Thread``
workers that push line-events back via Kivy's ``Clock.schedule_once``.
"""

from __future__ import annotations

import json
import logging
import threading
import webbrowser
from pathlib import Path
from typing import Any

from . import appdata_root, setup_state_path
from .build import (
    apply_ryujinx_patch,
    build_ready,
    collect_build_outputs,
    ensure_exlaunch_checkout,
    run_exlaunch_build,
)
from .deploy import (
    DeployResult,
    deploy_to_custom_folder,
    deploy_to_ryujinx,
    deploy_to_sd,
    detect_ryujinx_path,
    detect_sd_candidates,
)
from .prereqs import PrereqResult, all_ok, check_all
from .dreadap_file import parse_dreadap

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# State that persists between wizard runs (last deploy target, romfs path, …)
# ---------------------------------------------------------------------------

def load_setup_state() -> dict[str, Any]:
    p = setup_state_path()
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        log.warning("setup_state.json unreadable; starting fresh")
        return {}


def save_setup_state(state: dict[str, Any]) -> None:
    p = setup_state_path()
    try:
        p.write_text(json.dumps(state, indent=2), encoding="utf-8")
    except OSError as e:
        log.warning("failed to write setup_state.json: %s", e)


def _wizard_log_path() -> Path:
    return appdata_root() / "wizard.log"


def wizard_log(line: str) -> None:
    """Append a breadcrumb to ``%APPDATA%/dread_ap/wizard.log``.

    Captures page transitions, deploy handler entry/exit, populate()
    execution, and exception tracebacks so a "black screen, no text"
    report has SOMETHING for us to read after the fact.
    """
    import time
    try:
        p = _wizard_log_path()
        with open(p, "a", encoding="utf-8") as f:
            f.write(f"[{time.strftime('%H:%M:%S')}] {line}\n")
    except OSError:
        pass  # best-effort; log losing a line shouldn't break the wizard


# Cap how many times a single wizard page can re-run its worker before
# we stop offering the Retry button. Picked so a flaky network or AV
# scan can retry a handful of times without manual intervention, but a
# persistently broken step (missing devkitPro, msys2 not installed) eventually
# surfaces a clear "give up — fix the underlying cause" message instead of
# letting the user click Retry forever. Counts reset on a fresh page entry,
# so navigating Back→Forward gives a clean slate.
MAX_STEP_ATTEMPTS = 5


# ---------------------------------------------------------------------------
# Wizard entry point
# ---------------------------------------------------------------------------

def run_setup_wizard(dreadap_path: str | None = None) -> bool:
    """Open the Kivy wizard window. Blocks until the user closes it.

    ``dreadap_path`` is the ``.dreadap`` file the user opened (if any) —
    used to pre-fill DreadClient on the "Launch now" button at the end.
    Pass None when the wizard is invoked standalone (e.g. via ``/setup``).

    Returns True if the user clicked "Launch DreadClient" on the Done page
    (so the caller should hand off to DreadClient now that Kivy has shut
    down) and False otherwise. The caller — not this function — performs
    the DreadClient launch, because spawning a Kivy app from inside a still-
    running Kivy ``App().run()`` recurses into a broken state in frozen
    PyInstaller builds (multiprocessing.Process child can't read its
    bundled ``kivy/data/style.kv`` out of library.zip).
    """
    # IMPORTANT: kvui MUST be imported before any kivy.* module. kvui asserts
    # ``"kivy" not in sys.modules`` at its top and, as a side effect, sets
    # KIVY_DATA_DIR to point at AP's frozen-installer-extracted data dir.
    # Without this side effect, ``from kivy.app import App`` cascades into
    # kivy.lang.builder which open()s style.kv at the package-relative
    # default — inside library.zip in frozen builds — and aborts with
    # FileNotFoundError ("kivy\data\style.kv"). Same import-order rule
    # client/gui.py follows.
    import kvui  # noqa: F401

    # Lazy Kivy import — keeps the apworld importable on headless gen hosts.
    from kivy.app import App
    from kivy.uix.boxlayout import BoxLayout
    from kivy.uix.button import Button
    from kivy.uix.checkbox import CheckBox
    from kivy.uix.gridlayout import GridLayout
    from kivy.uix.label import Label
    from kivy.uix.popup import Popup
    from kivy.uix.screenmanager import Screen, ScreenManager
    from kivy.uix.scrollview import ScrollView
    from kivy.uix.textinput import TextInput
    from kivy.uix.widget import Widget

    # --------------------------- shared widgets / helpers -----------------

    def _label(text: str, **kw) -> Label:
        kw.setdefault("size_hint_y", None)
        kw.setdefault("height", 32)
        kw.setdefault("halign", "left")
        kw.setdefault("valign", "middle")
        lab = Label(text=text, **kw)
        lab.bind(size=lambda inst, val: setattr(inst, "text_size", val))
        return lab

    def _h1(text: str) -> Label:
        return _label(f"[size=24][b]{text}[/b][/size]", markup=True, height=48)

    def _nav_row(on_back, on_next, *, next_text="Next", next_enabled=True):
        row = BoxLayout(orientation="horizontal", size_hint_y=None, height=48, spacing=8)
        back_btn = Button(text="Back", size_hint_x=0.3)
        if on_back is None:
            back_btn.disabled = True
        else:
            back_btn.bind(on_release=lambda _i: on_back())
        next_btn = Button(text=next_text, size_hint_x=0.7)
        next_btn.disabled = not next_enabled
        if on_next is not None:
            next_btn.bind(on_release=lambda _i: on_next())
        row.add_widget(back_btn)
        row.add_widget(next_btn)
        return row, back_btn, next_btn

    def _ask_directory(title: str, initial: str | None = None) -> str:
        """Open a native folder picker; return the chosen path or "" on cancel.

        Tk's askdirectory is the cleanest cross-platform folder picker —
        Kivy's FileChooserListView is file-oriented and awkward for picking
        dirs. Tk is stdlib so no extra dep.
        """
        try:
            import tkinter
            import tkinter.filedialog
            tkroot = tkinter.Tk()
            tkroot.withdraw()
            tkroot.attributes("-topmost", True)
            kw: dict[str, Any] = {"title": title, "parent": tkroot}
            if initial:
                kw["initialdir"] = initial
            chosen = tkinter.filedialog.askdirectory(**kw)
            tkroot.destroy()
            return chosen or ""
        except Exception as e:
            wizard_log(f"folder picker failed: {e!r}")
            return ""

    # ----------------------------- shared state ---------------------------

    saved_state = load_setup_state()
    saved_romfs = saved_state.get("romfs_path")
    initial_romfs = (
        Path(saved_romfs) if saved_romfs and Path(saved_romfs).is_dir() else None
    )
    wizard_state: dict[str, Any] = {
        "dreadap_path": dreadap_path,
        "dreadap": parse_dreadap(Path(dreadap_path)) if dreadap_path else None,
        "romfs_path": initial_romfs,
        "build_done": build_ready(),  # carry across pages; reflect prior runs
        "deploy_target": saved_state.get("deploy_target", "ryujinx"),
        "ryujinx_root": str(detect_ryujinx_path() or saved_state.get("ryujinx_root", "")),
        "sd_root": saved_state.get("sd_root", ""),
        "custom_root": saved_state.get("custom_root", ""),
        # Set by the Done page's "Launch DreadClient" button before stopping
        # Kivy. The caller (in DreadClient's /setup handler) reads this after
        # ``App().run()`` returns and performs the actual DreadClient launch.
        "launch_smoclient_after_close": False,
    }
    wizard_log(f"=== wizard start (dreadap={dreadap_path!r}) ===")

    sm = ScreenManager()

    def goto(page_name: str) -> None:
        wizard_log(f"goto({page_name!r})")
        sm.current = page_name

    # ----------------------------- pages ----------------------------------

    # --- 1. Welcome
    def build_welcome() -> Screen:
        s = Screen(name="welcome")
        root = BoxLayout(orientation="vertical", padding=20, spacing=12)
        root.add_widget(_h1("Dread Archipelago — Setup"))
        msg = (
            "This wizard prepares everything DreadClient needs to run a "
            "Metroid Dread 2.1.0 Archipelago seed against Ryujinx or a "
            "modded Switch. Run it the first time you set up the client, "
            "and again whenever you switch deploy targets or update the "
            "apworld.\n\n"
            "REQUIREMENTS — confirm these BEFORE continuing:\n"
            "  - Metroid Dread version 2.1.0 (the newest retail patch). The "
            "Ryujinx + Atmosphere CFW path both expect 2.1.0 — patcher "
            "outputs against other versions are not supported.\n"
            "  - A pre-extracted vanilla Dread 2.1.0 romfs folder on this PC. "
            "If you do not have one yet, dump from a clean retail source with "
            "NXDumpTool and extract with hactool / nxgameuncomp before "
            "running this wizard. (We never read your NSP/XCI directly — "
            "the per-seed patcher overlays the extracted romfs.)\n"
            "  - For a real Switch deploy: Atmosphere CFW set up and an SD "
            "card you can write to from this PC. See "
            "https://nh-server.github.io/switch-guide/ if you are starting "
            "from scratch.\n\n"
            "This wizard will:\n"
            "  - Check that you have devkitPro (devkitA64 + msys2 bash) and "
            "Python 3.12 with open-dread-rando importable.\n"
            "  - Record the path to your extracted Dread 2.1.0 romfs so the "
            "per-seed patcher can run automatically when you connect to an "
            "Archipelago server.\n"
            "  - Clone open-dread-rando-exlaunch at the pinned commit, apply "
            "our Ryujinx-fix patch, and build the subsdk9 sysmodule under "
            "devkitPro's msys2 bash (~30-60 seconds on a warm cache).\n"
            "  - Copy the compiled subsdk9 + main.npdm to your Ryujinx mods "
            "directory, your Switch's SD card, or a custom folder you stage "
            "from.\n\n"
            "Changing AP server or slot does NOT require re-running this "
            "wizard — those go through DreadClient's Connect bar."
        )
        m = Label(text=msg, halign="left", valign="top", text_size=(600, None))
        m.bind(size=lambda inst, val: setattr(inst, "text_size", (val[0], None)))
        root.add_widget(m)
        nav, _, _ = _nav_row(None, lambda: goto("prereqs"), next_text="Begin")
        root.add_widget(nav)
        s.add_widget(root)
        return s

    # --- 2. Prereqs
    def build_prereqs() -> Screen:
        s = Screen(name="prereqs")
        root = BoxLayout(orientation="vertical", padding=20, spacing=12)
        root.add_widget(_h1("Prerequisites"))

        # Mode toggle: "auto" silently installs missing prereqs via winget;
        # "manual" surfaces install-page links. Both modes share the
        # underlying detector + winget-path probing, so a manual-mode user
        # who runs ``winget install`` in a separate terminal still has
        # Re-check turn rows green without restarting the wizard. Default
        # to auto; persist the choice across wizard restarts.
        persisted_mode = saved_state.get("prereq_mode", "auto")
        mode_state: dict[str, str] = {"mode": persisted_mode}
        mode_row = BoxLayout(orientation="horizontal", size_hint_y=None,
                             height=40, spacing=8)
        auto_cb = CheckBox(group="prereq_mode",
                           active=(persisted_mode == "auto"),
                           size_hint_x=None, width=30)
        mode_row.add_widget(auto_cb)
        mode_row.add_widget(_label(
            "Install them for me (recommended)",
            size_hint_x=None, width=300,
        ))
        manual_cb = CheckBox(group="prereq_mode",
                             active=(persisted_mode == "manual"),
                             size_hint_x=None, width=30)
        mode_row.add_widget(manual_cb)
        mode_row.add_widget(_label("I'll install them myself"))
        root.add_widget(mode_row)

        rows_box = BoxLayout(orientation="vertical", spacing=4, size_hint_y=None)
        rows_box.bind(minimum_height=rows_box.setter("height"))
        scroller = ScrollView()
        scroller.add_widget(rows_box)
        root.add_widget(scroller)

        next_btn_holder: dict[str, Any] = {}
        # Hold the most-recent results so a mode change can re-render
        # without re-running the detectors (which can take ~1 s).
        render_state: dict[str, Any] = {"last_results": []}

        def run_installer_popup(keys: list[str], *, preflight: bool) -> None:
            """Spawn a worker thread that runs ``_setup.installers`` for the
            given prereq keys, streaming output into a modal popup.

            ``preflight=True`` runs ``check_internet`` (always) and
            ``check_winget`` (if any winget-installable key is in the batch)
            before kicking off the first installer. Used by the "Install all
            missing" button. Per-row Auto-install bypasses preflight to keep
            the single-tool case snappy."""
            log_widget = TextInput(text="", readonly=True, size_hint=(1, 1))
            popup_box = BoxLayout(orientation="vertical", spacing=8)
            popup_box.add_widget(log_widget)
            close_btn = Button(text="Installing... (close button enabled when done)",
                               size_hint_y=None, height=40, disabled=True)
            popup_box.add_widget(close_btn)
            popup = Popup(
                title=f"Installing {', '.join(keys)}",
                content=popup_box,
                size_hint=(0.9, 0.85),
                auto_dismiss=False,
            )
            close_btn.bind(on_release=lambda _i: popup.dismiss())
            popup.open()

            log_lines: list[str] = []

            def append_log(line: str) -> None:
                log_lines.append(line)
                if len(log_lines) > 2000:
                    del log_lines[:1000]
                log_widget.text = "\n".join(log_lines[-400:])

            def on_line(line: str) -> None:
                from kivy.clock import Clock as _Clock
                _Clock.schedule_once(lambda dt: append_log(line))

            def worker() -> None:
                # Lazy-import installers — pulls in urllib + the winget
                # subprocess machinery, none of which the apworld layer
                # should drag onto a headless gen host.
                from .installers import (
                    INSTALLERS, check_internet, check_winget,
                )
                if preflight:
                    on_line("[wizard] preflight: checking internet...")
                    r = check_internet(on_line)
                    if not r.ok:
                        on_line("[wizard] preflight failed; aborting.")
                        from kivy.clock import Clock as _Clock
                        _Clock.schedule_once(
                            lambda dt: (
                                setattr(close_btn, "disabled", False),
                                setattr(close_btn, "text", "Close (install failed)"),
                            ),
                        )
                        return
                    if any(k == "python312" for k in keys):
                        on_line("[wizard] preflight: checking winget...")
                        r = check_winget(on_line)
                        if not r.ok:
                            on_line("[wizard] preflight failed; aborting.")
                            from kivy.clock import Clock as _Clock
                            _Clock.schedule_once(
                                lambda dt: (
                                    setattr(close_btn, "disabled", False),
                                    setattr(close_btn, "text", "Close (install failed)"),
                                ),
                            )
                            return
                any_failed = False
                for key in keys:
                    fn = INSTALLERS.get(key)
                    if fn is None:
                        on_line(f"[wizard] no installer registered for {key!r}; skipping")
                        continue
                    on_line(f"[wizard] === starting installer for {key!r} ===")
                    try:
                        result = fn(on_line)
                    except Exception as e:  # pragma: no cover — surface to UI
                        import traceback
                        on_line(f"[wizard] installer for {key!r} crashed: "
                                f"{type(e).__name__}: {e}")
                        on_line(traceback.format_exc())
                        any_failed = True
                        break
                    on_line(
                        f"[wizard] installer {key!r}: ok={result.ok} "
                        f"detail={result.detail!r}"
                    )
                    if not result.ok:
                        any_failed = True
                        on_line(f"[wizard] stopping install run after {key!r} failure")
                        break
                on_line("[wizard] === install run complete; re-running prereq check ===")
                from kivy.clock import Clock as _Clock

                def finish(_dt):
                    close_btn.disabled = False
                    close_btn.text = (
                        "Close" if not any_failed
                        else "Close (some installs failed — see log above)"
                    )
                    # Always re-check after install regardless of failure —
                    # rows that DID succeed still need their green flip.
                    do_check()
                _Clock.schedule_once(finish)

            threading.Thread(target=worker, daemon=True).start()

        def render(results: list[PrereqResult]) -> None:
            render_state["last_results"] = results
            rows_box.clear_widgets()
            current_mode = mode_state["mode"]
            for r in results:
                row = BoxLayout(orientation="horizontal", size_hint_y=None, height=36, spacing=8)
                mark = "[color=00aa00][b]OK[/b][/color]" if r.ok else "[color=cc0000][b]X[/b][/color]"
                row.add_widget(Label(text=mark, markup=True, size_hint_x=0.1))
                row.add_widget(Label(text=r.name, size_hint_x=0.25, halign="left", text_size=(150, None)))
                row.add_widget(Label(text=r.detail[:80], size_hint_x=0.5, halign="left", text_size=(360, None)))
                # Mode-dependent action button: Auto-install in auto mode
                # (when the detector is auto-installable), Install link in
                # manual mode or as a fallback when no auto-install path
                # exists.
                if not r.ok:
                    if current_mode == "auto" and r.auto_installable:
                        auto_btn = Button(text="Auto-install", size_hint_x=0.15)
                        auto_btn.bind(
                            on_release=lambda _i, key=r.key: run_installer_popup(
                                [key], preflight=False,
                            ),
                        )
                        row.add_widget(auto_btn)
                    elif r.install_url:
                        link = Button(text="Install...", size_hint_x=0.15)
                        link.bind(on_release=lambda _i, url=r.install_url: webbrowser.open(url))
                        row.add_widget(link)
                    else:
                        row.add_widget(Label(text="", size_hint_x=0.15))
                else:
                    row.add_widget(Label(text="", size_hint_x=0.15))
                rows_box.add_widget(row)
                # When a detector provides multi-line install guidance
                # (e.g. the open_dread_rando pip command), surface it as a
                # sub-row below the main row. Auto-size the height to the
                # wrapped text so the message can't be silently clipped.
                if not r.ok and r.note:
                    note_lbl = Label(
                        text=r.note,
                        size_hint_y=None,
                        halign="left",
                        valign="top",
                        color=(0.85, 0.7, 0.2, 1),
                    )
                    def _resize_note(inst, val, _lbl=note_lbl):
                        _lbl.text_size = (val[0], None)
                        _lbl.height = _lbl.texture_size[1] + 8
                    note_lbl.bind(size=_resize_note)
                    rows_box.add_widget(note_lbl)
            ok = all_ok(results)
            if "next_btn" in next_btn_holder:
                next_btn_holder["next_btn"].disabled = not ok

        recheck = Button(text="Re-check", size_hint_y=None, height=40)
        check_in_progress: dict[str, bool] = {"running": False}

        def do_check() -> None:
            # ``check_all`` shells out to a few detectors — ~1 second of
            # frozen UI on the main thread if run inline. Off-load to a
            # worker so the button visibly changes state and the Kivy frame
            # loop keeps running.
            if check_in_progress["running"]:
                return
            check_in_progress["running"] = True
            recheck.disabled = True
            recheck.text = "Checking..."

            def worker() -> None:
                results = check_all()
                def finish(_dt):
                    render(results)
                    recheck.text = "Re-check"
                    recheck.disabled = False
                    check_in_progress["running"] = False
                from kivy.clock import Clock as _Clock
                _Clock.schedule_once(finish)

            threading.Thread(target=worker, daemon=True).start()

        # "Install all missing" — auto mode only. Disabled (greyed out) in
        # manual mode so the UI element stays visible without accidentally
        # firing. For dread, only Python 3.12 is auto-installable; devkitPro
        # remains user-driven (its installer is interactive Inno Setup) and
        # open_dread_rando is a pip command surfaced as a row note.
        def on_install_all_missing(_i) -> None:
            from .installers import INSTALL_ORDER
            results = render_state.get("last_results", [])
            failed_keys = {
                r.key for r in results if not r.ok and r.auto_installable
            }
            ordered = [k for k in INSTALL_ORDER if k in failed_keys]
            if not ordered:
                wizard_log("Install all missing: no failed auto-installable rows")
                return
            wizard_log(f"Install all missing: {ordered}")
            run_installer_popup(ordered, preflight=True)

        install_all_btn = Button(
            text="Install all missing (Python 3.12 via winget; ~25 MB)",
            size_hint_y=None, height=40,
            disabled=(persisted_mode != "auto"),
        )
        install_all_btn.bind(on_release=on_install_all_missing)
        root.add_widget(install_all_btn)

        recheck.bind(on_release=lambda _i: do_check())
        root.add_widget(recheck)

        def on_mode_change(_inst, _val) -> None:
            new_mode = "auto" if auto_cb.active else "manual"
            if new_mode == mode_state["mode"]:
                return
            mode_state["mode"] = new_mode
            state = load_setup_state()
            state["prereq_mode"] = new_mode
            save_setup_state(state)
            install_all_btn.disabled = (new_mode != "auto")
            # Re-render without re-running detectors so button swap is
            # instant. The cached results are still authoritative because
            # a mode change doesn't affect detection.
            render(render_state.get("last_results", []))
        auto_cb.bind(active=on_mode_change)
        manual_cb.bind(active=on_mode_change)

        nav, _, next_btn = _nav_row(lambda: goto("welcome"), lambda: goto("romfs"))
        next_btn.disabled = True
        next_btn_holder["next_btn"] = next_btn
        root.add_widget(nav)
        s.add_widget(root)
        # Run the initial check when the page is first shown.
        s.bind(on_pre_enter=lambda _i: do_check())
        return s

    # --- 3. RomFS picker
    def build_romfs() -> Screen:
        s = Screen(name="romfs")
        root = BoxLayout(orientation="vertical", padding=20, spacing=12)
        root.add_widget(_h1("Pick your extracted Dread 2.1.0 romfs"))
        root.add_widget(_label(
            "Browse to your pre-extracted Dread 2.1.0 romfs folder — the "
            "directory that contains ``system/`` and ``packs/``. The per-seed "
            "patcher reads this at AP-connect time and overlays placements / "
            "options into your Ryujinx (or Switch) mod install.\n\n"
            "If you haven't extracted yet, dump from a clean retail source "
            "with NXDumpTool and extract with hactool / nxgameuncomp; we "
            "never read the NSP/XCI directly.",
            height=128,
        ))

        picker_row = BoxLayout(orientation="horizontal", size_hint_y=None,
                               height=48, spacing=8)
        initial_path = wizard_state.get("romfs_path")
        path_input = TextInput(
            text=(str(initial_path) if initial_path
                  else "(no folder picked — click Browse...)"),
            readonly=True,
            multiline=False,
        )
        picker_row.add_widget(path_input)
        browse_btn = Button(text="Browse...", size_hint_x=None, width=120)
        picker_row.add_widget(browse_btn)
        root.add_widget(picker_row)

        err_label = _label("", color=(0.85, 0.7, 0.2, 1))
        root.add_widget(err_label)

        nav, _, next_btn = _nav_row(lambda: goto("prereqs"),
                                    lambda: goto("build"))
        # If a valid romfs path was restored from saved state, the user can
        # advance immediately without re-Browsing.
        next_btn.disabled = initial_path is None

        def on_browse(_i) -> None:
            current = wizard_state.get("romfs_path")
            picked = _ask_directory(
                "Select your extracted Dread 2.1.0 romfs folder",
                str(current) if current else None,
            )
            if not picked:
                return
            picked_path = Path(picked)
            if not picked_path.is_dir():
                err_label.text = f"Not a folder: {picked}"
                return
            # Light sanity check — a Dread 2.1.0 romfs has these subdirs.
            # We warn (not block) because a user with an unusual extractor
            # layout (subfolder, hardlink farm) might still produce a tree
            # that works with the patcher.
            warn_missing = [
                name for name in ("system", "packs")
                if not (picked_path / name).is_dir()
            ]
            if warn_missing:
                err_label.text = (
                    f"Note: {', '.join(warn_missing)} not found under this "
                    f"folder. If the patcher fails at AP-connect, re-pick a "
                    f"folder whose contents look like an extracted romfs."
                )
            else:
                err_label.text = ""
            wizard_state["romfs_path"] = picked_path
            path_input.text = str(picked_path)
            next_btn.disabled = False
            # Persist so the next wizard run pre-fills this path. Merge into
            # existing state to preserve sibling keys.
            state = load_setup_state()
            state["romfs_path"] = str(picked_path)
            save_setup_state(state)

        browse_btn.bind(on_release=on_browse)
        root.add_widget(nav)
        s.add_widget(root)
        return s

    # --- 4. Build
    def build_build() -> Screen:
        s = Screen(name="build")
        root = BoxLayout(orientation="vertical", padding=20, spacing=12)
        root.add_widget(_h1("Build patched exlaunch sysmodule"))
        status = _label("Preparing...")
        root.add_widget(status)
        log_lines: list[str] = []
        log_box = TextInput(text="", readonly=True, size_hint=(1, 1))
        root.add_widget(log_box)

        nav, _, next_btn = _nav_row(lambda: goto("romfs"), lambda: goto("deploy"))
        next_btn.disabled = True
        retry_btn = Button(text="Retry", size_hint_y=None, height=40, disabled=True)
        root.add_widget(retry_btn)
        root.add_widget(nav)
        s.add_widget(root)

        def append_line(line: str) -> None:
            log_lines.append(line)
            if len(log_lines) > 2000:
                del log_lines[:1000]
            log_box.text = "\n".join(log_lines[-400:])

        def on_line(line: str) -> None:
            from kivy.clock import Clock as _Clock
            _Clock.schedule_once(lambda dt: append_line(line))

        def update_status(text: str) -> None:
            from kivy.clock import Clock as _Clock
            _Clock.schedule_once(lambda dt: status.setter("text")(status, text))

        def run_in_worker() -> None:
            steps: list[tuple[str, Any]] = [
                ("Cloning open-dread-rando-exlaunch at pinned commit...",
                 lambda: ensure_exlaunch_checkout(on_line=on_line)),
                ("Applying Ryujinx-fix patch (idempotent)...",
                 lambda: apply_ryujinx_patch(on_line=on_line)),
                ("Compiling subsdk9 under devkitPro msys2 bash "
                 "(~30-60s on a warm cache)...",
                 lambda: run_exlaunch_build(on_line=on_line)),
            ]
            for label, fn in steps:
                update_status(label)
                try:
                    result = fn()
                except FileNotFoundError as e:
                    update_status(f"Failed: {e}")
                    from kivy.clock import Clock as _Clock
                    _Clock.schedule_once(lambda dt: setattr(retry_btn, "disabled", False))
                    return
                except Exception as e:  # pragma: no cover — surface to UI
                    import traceback
                    on_line(f"[wizard] step {label!r} crashed: "
                            f"{type(e).__name__}: {e}")
                    on_line(traceback.format_exc())
                    update_status(f"Failed: {type(e).__name__}: {e}")
                    from kivy.clock import Clock as _Clock
                    _Clock.schedule_once(lambda dt: setattr(retry_btn, "disabled", False))
                    return
                if not result.ok:
                    # Prefer the detail field (often a one-liner with a
                    # remediation hint) over the raw subprocess exit code.
                    summary = result.detail or f"exit {result.returncode}"
                    update_status(f"Failed at: {label} — {summary}")
                    from kivy.clock import Clock as _Clock
                    _Clock.schedule_once(lambda dt: setattr(retry_btn, "disabled", False))
                    return
            # Verify outputs landed where deploy expects them. ``./exlaunch.sh
            # build`` returning 0 is necessary but not sufficient on Windows
            # subprocess plumbing in rare circumstances — confirm the two
            # files are on disk before claiming success.
            outputs = collect_build_outputs()
            if len(outputs) != 2:
                missing = [k for k in ("subsdk9", "main.npdm") if k not in outputs]
                update_status(
                    f"Build returned 0 but {missing} missing from the "
                    f"exlaunch deploy directory — see log above."
                )
                from kivy.clock import Clock as _Clock
                _Clock.schedule_once(lambda dt: setattr(retry_btn, "disabled", False))
                return
            wizard_state["build_done"] = True
            update_status(
                f"Build complete: subsdk9 ({outputs['subsdk9'].stat().st_size} bytes), "
                f"main.npdm ({outputs['main.npdm'].stat().st_size} bytes)."
            )
            from kivy.clock import Clock as _Clock
            _Clock.schedule_once(lambda dt: setattr(next_btn, "disabled", False))

        # Bounded-retry counter for the build page. A wedged checkout / wrong
        # devkitPro / missing msys2 won't fix itself by retrying, so eventually
        # we surface a "diagnose and re-open setup" message instead of letting
        # the user click Retry forever.
        build_state: dict[str, Any] = {"attempt_count": 0}

        def start_worker() -> None:
            if build_state["attempt_count"] >= MAX_STEP_ATTEMPTS:
                msg = (
                    f"Build failed {build_state['attempt_count']} times in "
                    f"a row. Common causes: missing devkitPro msys2 component, "
                    f"git clone blocked by AV, pinned exlaunch sha "
                    f"force-pushed upstream. Re-run the prereq Re-check, "
                    f"then re-open the wizard."
                )
                update_status(msg)
                on_line(f"[wizard] {msg}")
                retry_btn.disabled = True
                return
            build_state["attempt_count"] += 1
            on_line(
                f"[wizard] === build attempt "
                f"{build_state['attempt_count']}/{MAX_STEP_ATTEMPTS} ==="
            )
            log_lines.clear()
            log_box.text = ""
            next_btn.disabled = True
            retry_btn.disabled = True
            threading.Thread(target=run_in_worker, daemon=True).start()

        def _reset_and_start(*_):
            # Fresh page entry resets the attempt counter so navigating
            # Back→Forward doesn't immediately hit the cap. If a previous
            # wizard run already produced build outputs (build_done was
            # carried in from build_ready()), skip the worker and let the
            # user proceed straight to Deploy — re-running the build wastes
            # 30-60s on a no-op.
            build_state["attempt_count"] = 0
            if wizard_state.get("build_done") and build_ready():
                outputs = collect_build_outputs()
                msg = (
                    f"Build already complete from a previous wizard run "
                    f"(subsdk9 + main.npdm present at "
                    f"{outputs['subsdk9'].parent}). Click Next to deploy, "
                    f"or Retry to rebuild from scratch."
                )
                update_status(msg)
                on_line(f"[wizard] {msg}")
                next_btn.disabled = False
                retry_btn.disabled = False
                return
            start_worker()

        retry_btn.bind(on_release=lambda _i: start_worker())
        s.bind(on_pre_enter=_reset_and_start)
        return s

    # --- 5. Deploy target
    def build_deploy() -> Screen:
        s = Screen(name="deploy")
        root = BoxLayout(orientation="vertical", padding=20, spacing=12)
        root.add_widget(_h1("Deploy target"))
        root.add_widget(_label(
            "Where should we copy the compiled subsdk9 + main.npdm?"
        ))

        # Three radio rows in a 4-column GridLayout so checkbox / label /
        # input / browse-button align vertically across rows.
        sd_candidates = detect_sd_candidates()
        sd_default = str(sd_candidates[0]) if sd_candidates else ""
        wizard_state["sd_root"] = sd_default or wizard_state.get("sd_root", "")
        wizard_state.setdefault("custom_root", "")

        grid = GridLayout(
            cols=4,
            size_hint_y=None,
            spacing=8,
            row_default_height=48,
            row_force_default=True,
        )
        grid.bind(minimum_height=grid.setter("height"))

        _CB_W = 40
        _LBL_W = 200
        _BROWSE_W = 100

        # Ryujinx row
        ryu_cb = CheckBox(group="target",
                          active=(wizard_state["deploy_target"] == "ryujinx"),
                          size_hint_x=None, width=_CB_W)
        ryu_input = TextInput(text=wizard_state["ryujinx_root"] or "(not detected)",
                              multiline=False)
        grid.add_widget(ryu_cb)
        grid.add_widget(_label("Ryujinx (emulator):",
                               size_hint_x=None, width=_LBL_W))
        grid.add_widget(ryu_input)
        grid.add_widget(Widget(size_hint_x=None, width=_BROWSE_W))

        # SD row
        sd_cb = CheckBox(group="target",
                         active=(wizard_state["deploy_target"] == "sd"),
                         size_hint_x=None, width=_CB_W)
        sd_input = TextInput(
            text=wizard_state["sd_root"] or "(plug SD card in, then click Re-detect)",
            multiline=False,
        )
        grid.add_widget(sd_cb)
        grid.add_widget(_label("Real Switch (SD card):",
                               size_hint_x=None, width=_LBL_W))
        grid.add_widget(sd_input)
        grid.add_widget(Widget(size_hint_x=None, width=_BROWSE_W))

        # Custom-folder row — for users who want to manage the SD-card
        # sync themselves (UMS, DBI / Goldleaf transfer, staging on a
        # network share). Writes the same atmosphere/contents/...
        # subtree the SD-card deploy produces.
        custom_cb = CheckBox(group="target",
                             active=(wizard_state["deploy_target"] == "custom"),
                             size_hint_x=None, width=_CB_W)
        custom_input = TextInput(
            text=wizard_state["custom_root"] or "(click Browse to pick a folder)",
            multiline=False,
        )
        browse_btn = Button(text="Browse...", size_hint_x=None, width=_BROWSE_W)
        grid.add_widget(custom_cb)
        grid.add_widget(_label("Custom folder:",
                               size_hint_x=None, width=_LBL_W))
        grid.add_widget(custom_input)
        grid.add_widget(browse_btn)

        root.add_widget(grid)

        def open_custom_picker(_i):
            chosen = _ask_directory(
                "Select custom deploy folder",
                wizard_state.get("custom_root") or None,
            )
            if chosen:
                custom_input.text = chosen
                custom_cb.active = True
            else:
                status.text = "Folder picker cancelled or unavailable."
        browse_btn.bind(on_release=open_custom_picker)

        redetect = Button(text="Re-detect", size_hint_y=None, height=40)
        def do_redetect(_i):
            cands = detect_sd_candidates()
            if cands:
                sd_input.text = str(cands[0])
            ryu_input.text = str(detect_ryujinx_path() or "")
        redetect.bind(on_release=do_redetect)
        root.add_widget(redetect)

        status = _label("")
        root.add_widget(status)

        nav, _, _ = _nav_row(lambda: goto("build"), lambda: do_deploy_and_continue())
        root.add_widget(nav)
        s.add_widget(root)

        def do_deploy_and_continue() -> None:
            wizard_log("do_deploy_and_continue: entered")
            outputs = collect_build_outputs()
            if len(outputs) != 2:
                missing = [k for k in ("subsdk9", "main.npdm") if k not in outputs]
                wizard_log(f"do_deploy_and_continue: build outputs missing: {missing}")
                status.text = (
                    f"Build outputs missing ({missing}). Go back to the "
                    f"Build step and re-run."
                )
                return
            if ryu_cb.active:
                target = Path(ryu_input.text.strip())
                if not target.is_dir():
                    status.text = f"Ryujinx folder does not exist: {target}"
                    return
                wizard_log(f"deploy_to_ryujinx target={target}")
                result = deploy_to_ryujinx(target, outputs)
                wizard_state["deploy_target"] = "ryujinx"
                wizard_state["ryujinx_root"] = str(target)
            elif sd_cb.active:
                target = Path(sd_input.text.strip())
                if not target.exists():
                    status.text = f"SD card path does not exist: {target}"
                    return
                wizard_log(f"deploy_to_sd target={target}")
                result = deploy_to_sd(target, outputs)
                wizard_state["deploy_target"] = "sd"
                wizard_state["sd_root"] = str(target)
            elif custom_cb.active:
                target = Path(custom_input.text.strip())
                # Custom folder needn't already exist — we'll create it.
                # But the parent must exist and be a directory; otherwise
                # we'd silently create folder trees in surprising places.
                if not target.parent.exists():
                    status.text = (
                        f"Custom folder parent does not exist: {target.parent}"
                    )
                    return
                target.mkdir(parents=True, exist_ok=True)
                wizard_log(f"deploy_to_custom_folder target={target}")
                result = deploy_to_custom_folder(target, outputs)
                wizard_state["deploy_target"] = "custom"
                wizard_state["custom_root"] = str(target)
            else:
                status.text = "Pick a deploy target (Ryujinx, SD card, or Custom folder)."
                return
            wizard_log(
                f"deploy result ok={result.ok} target={result.target!r} "
                f"files={len(result.files)} error={result.error!r}"
            )
            if not result.ok:
                status.text = f"Deploy failed: {result.error}"
                return
            # Merge into existing state instead of replacing — sibling keys
            # (romfs_path, prereq_mode, etc.) live in the same file.
            state = load_setup_state()
            state.update({
                "deploy_target": wizard_state["deploy_target"],
                "ryujinx_root": wizard_state["ryujinx_root"],
                "sd_root": wizard_state["sd_root"],
                "custom_root": wizard_state["custom_root"],
            })
            save_setup_state(state)
            wizard_state["deploy_result"] = result
            wizard_log("deploy succeeded; transitioning to 'done' page")
            goto("done")

        return s

    # --- 6. Done
    def build_done() -> Screen:
        s = Screen(name="done")
        root = BoxLayout(orientation="vertical", padding=20, spacing=12)
        root.add_widget(_h1("Setup complete"))

        def _success_banner() -> Label:
            # Bright green, larger-than-h1 confirmation so the user has an
            # unmistakable "you did it" cue even if the deploy summary
            # below fails to render.
            return _label(
                "[size=22][b]Installation successful.[/b][/size]",
                markup=True,
                color=(0.2, 0.8, 0.2, 1),
                height=56,
            )

        def _populate_inner() -> None:
            root.clear_widgets()
            root.add_widget(_success_banner())
            root.add_widget(_h1("Setup complete"))
            result: DeployResult | None = wizard_state.get("deploy_result")
            target_kind = wizard_state.get("deploy_target", "ryujinx")
            if result:
                if target_kind == "ryujinx":
                    next_step = (
                        "  - In Ryujinx: close Dread completely (back out to "
                        "the game list, not just the title screen) and "
                        "relaunch it. Ryujinx applies exefs mods at process "
                        "start, so the new subsdk9 only loads on a fresh "
                        "boot of the game.\n"
                    )
                elif target_kind == "sd":
                    next_step = (
                        "  - For a real Switch: eject the SD card and plug it "
                        "back into the console, then boot Dread. Atmosphere "
                        "loads the sysmodule automatically.\n"
                    )
                else:
                    next_step = (
                        "  - For Custom folder: the files are laid out under "
                        "``atmosphere/contents/010093801237c000/exefs/`` "
                        "inside the folder you picked. Copy that whole "
                        "subtree to your SD card's root (or onto the Switch "
                        "however you prefer).\n"
                    )
                summary = (
                    f"Copied {len(result.files)} file(s) to {result.target}.\n\n"
                    "What to do next:\n"
                    f"{next_step}"
                    "  - Connect DreadClient to your AP server. The per-seed "
                    "romfs patcher runs automatically on AP-connect once "
                    "your romfs path is recorded (it is — you set it on the "
                    "RomFS picker page).\n\n"
                    "Re-run this wizard if you switch deploy targets, update "
                    "the apworld, or want to rebuild from scratch. AP server "
                    "or slot changes don't need a rebuild — type /connect or "
                    "use the Connect bar in DreadClient."
                )
                summary_lbl = _label(summary, height=288)
                root.add_widget(summary_lbl)
            launch_row = BoxLayout(orientation="horizontal", size_hint_y=None, height=48)
            if wizard_state.get("dreadap") is not None:
                launch_btn = Button(text=f"Launch DreadClient as {wizard_state['dreadap'].slot_name}")
                launch_btn.bind(on_release=lambda _i: launch_smoclient_now())
                launch_row.add_widget(launch_btn)
            close_btn = Button(text="Close")
            close_btn.bind(on_release=lambda _i: App.get_running_app().stop())
            launch_row.add_widget(close_btn)
            root.add_widget(launch_row)

        def populate(*_):
            """Done-page builder. Wrapped in try/except so a render error
            becomes a visible "setup is done but the wizard couldn't render
            the summary" page rather than a black screen with no text.
            Either way, the user is free to close — setup IS complete; this
            is just the post-deploy UI."""
            wizard_log(f"populating Done page; deploy_result={wizard_state.get('deploy_result')!r}")
            try:
                _populate_inner()
            except Exception as e:
                import traceback
                tb = traceback.format_exc()
                wizard_log(f"populate() crashed: {e!r}\n{tb}")
                root.clear_widgets()
                root.add_widget(_success_banner())
                root.add_widget(_h1("Setup complete (rendering issue)"))
                root.add_widget(_label(
                    f"Setup finished but the Done page failed to render: {e}\n\n"
                    f"This does NOT mean setup failed — your build is deployed.\n"
                    f"Full traceback at {_wizard_log_path()}.",
                    height=120,
                ))
                close_btn = Button(text="Close", size_hint_y=None, height=48)
                close_btn.bind(on_release=lambda _i: App.get_running_app().stop())
                root.add_widget(close_btn)

        s.bind(on_pre_enter=populate)
        s.add_widget(root)
        return s

    def launch_smoclient_now() -> None:
        """Mark for post-wizard launch, then stop Kivy.

        We can't spawn DreadClient from here because (a) ``launch_subprocess``
        is broken in frozen builds (Kivy bootstrap fails in the child) and
        (b) running DreadClient inline would try to start a second Kivy App
        while ours is still alive. The caller performs the handoff after
        ``App().run()`` returns and Kivy is fully torn down."""
        wizard_state["launch_smoclient_after_close"] = True
        App.get_running_app().stop()

    # ----------------------- assemble ---------------------------------

    sm.add_widget(build_welcome())
    sm.add_widget(build_prereqs())
    sm.add_widget(build_romfs())
    sm.add_widget(build_build())
    sm.add_widget(build_deploy())
    sm.add_widget(build_done())

    class SetupApp(App):
        title = "Dread Archipelago — Setup"
        def build(self_app):  # type: ignore[override]
            return sm

    SetupApp().run()
    return bool(wizard_state["launch_smoclient_after_close"])
