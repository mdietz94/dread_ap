"""apworld root — registers ``DreadWorld`` and the "Dread Client" Launcher
Component with Archipelago.

Exposes the full apworld scaffolding (World subclass, items / locations /
regions tables, Rules, Options) plus the Kivy-free client Launcher entry
point, mirroring the smo_archipelago/__init__.py shape. The World import is
lazy so the Launcher Component still registers when the AP stack isn't on
sys.path (unit-test isolation).
"""
from __future__ import annotations


__version__ = "0.7.0"


# Re-export the World subclass so Archipelago's autodiscovery
# (``worlds.AutoWorld.AutoWorldRegister``) finds it. Lazy-imported so
# the Launcher Component still registers even when ``BaseClasses`` /
# the rest of the AP stack isn't on sys.path (unit-test isolation).
try:
    from .World import DreadWorld  # noqa: F401
except ImportError:
    pass


def _setup_state_incomplete() -> bool:
    """True when the /setup wizard hasn't persisted enough state for the
    sysmodule + per-seed patcher to work.

    Three signals count as "incomplete" and trigger the first-run wizard
    pop on launch:
      - ``setup_state.json`` missing entirely
      - JSON unreadable / malformed
      - ``romfs_path`` or ``deploy_target`` absent (a partial run that
        didn't reach the RomFS picker or Deploy page)

    Always returns False if the ``_setup`` package isn't available — the
    Launcher Component still needs to register on unit-test isolation
    without an AP install on sys.path.
    """
    try:
        from ._setup import setup_state_path
    except ImportError:
        return False
    import json
    path = setup_state_path()
    if not path.is_file():
        return True
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return True
    if not isinstance(state, dict):
        return True
    return not state.get("romfs_path") or not state.get("deploy_target")


def _spawn_setup_wizard() -> None:
    """Spawn the Kivy setup wizard as a parallel subprocess so it runs
    alongside DreadClient on first launch.

    Subprocess (not in-process) because ``run_setup_wizard`` blocks on
    its own ``App().run()`` — running it inline would freeze the AP
    Launcher until the user closes the wizard. Goes through Archipelago's
    ``launch_subprocess`` (the same path the ``/setup`` slash command
    uses) so there's one canonical wizard-spawn flow and the
    multiprocessing.Process child inherits Kivy's data-dir setup that
    the bare ``subprocess.Popen([sys.executable, "-c", ...])`` form lacks
    under the PyInstaller-frozen Launcher. Failures here are
    logged-and-swallowed: a broken wizard launch must not block
    DreadClient from coming up.
    """
    import logging
    try:
        from worlds.LauncherComponents import launch_subprocess
    except Exception as e:  # noqa: BLE001
        logging.getLogger(__name__).warning(
            "could not import launch_subprocess for wizard spawn: %s", e)
        return
    try:
        launch_subprocess(_run_setup_wizard_no_dreadap, name="DreadSetup")
    except Exception as e:  # noqa: BLE001
        logging.getLogger(__name__).warning(
            "could not spawn setup wizard subprocess: %s", e)


# ---------------------------------------------------------------------------
# Wizard subprocess entry point
# ---------------------------------------------------------------------------

# Imported here (not lazily) so module-level decoration runs at import time
# — without it the decorated function below couldn't be looked up by
# qualified name when ``launch_subprocess`` pickles the target.
from ._setup.launcher_errors import visible_errors as _visible_errors


@_visible_errors("Setup wizard")
def _run_setup_wizard_no_dreadap() -> None:
    """Module-level subprocess entry: open the Kivy setup wizard.

    Mirrors smo_archipelago's ``_run_setup_wizard_no_smoap`` shape so
    the ``/setup`` slash command and the first-run gate both target the
    same callable. Must stay module-level (not a closure) because
    ``worlds.LauncherComponents.launch_subprocess`` pickles its target
    by qualified name — closures would fail to import in the child.

    Invoked by:
      - ``DreadClientCommandProcessor._cmd_setup`` (the ``/setup``
        slash command in DreadClient)
      - ``_spawn_setup_wizard`` (the first-run gate on
        ``launch_dread_client``)

    The ``@visible_errors`` wrapper surfaces import-time / Kivy-launch
    crashes via a Tk messagebox + ``%APPDATA%/dread_ap/launch-crash.log``
    so a wizard that explodes during early init doesn't just vanish into
    the windowed-Launcher void.
    """
    from ._setup.wizard import run_setup_wizard
    run_setup_wizard()


def launch_dread_client(*args: str) -> None:
    """Archipelago Launcher entry point for the Dread Client.

    Triggered by clicking the "Dread Client" button in the Launcher, or by
    double-clicking a ``.dreadap`` file (the Component's
    ``SuffixIdentifier('.dreadap')`` registers the extension). When a
    ``.dreadap`` is passed, its slot_name (+ optional server/password) are
    expanded into CLI args so the client lands pre-filled; the ``.dreadap``
    arg itself is dropped (the client's argparser doesn't know it). A parse
    failure never blocks the launch — we log and open the client unfilled.

    First-run gate: if the /setup wizard hasn't recorded the romfs path
    + deploy target yet, spawn the Kivy wizard as a parallel subprocess
    so the user is guided through prereqs / build / deploy without having
    to discover the /setup slash command. The wizard runs ALONGSIDE
    DreadClient — the user can close it any time; DreadClient stays up.
    """
    from worlds.LauncherComponents import launch as launch_or_subprocess
    from .client.main import launch as dread_client_launch

    final_args = list(args)
    dreadap_path = next((a for a in final_args if a.endswith(".dreadap")), None)
    if dreadap_path:
        final_args = [a for a in final_args if not a.endswith(".dreadap")]
        try:
            from .client.dreadap_file import parse_dreadap, dreadap_to_launch_args
            from pathlib import Path
            final_args = dreadap_to_launch_args(parse_dreadap(Path(dreadap_path))) + final_args
        except Exception as e:  # noqa: BLE001 — never block the launch
            import logging
            logging.getLogger(__name__).warning(
                "could not parse %s: %s; launching client without pre-fill",
                dreadap_path, e)

    if _setup_state_incomplete():
        _spawn_setup_wizard()

    launch_or_subprocess(dread_client_launch, name="DreadClient", args=final_args)


def add_client_to_launcher() -> None:
    """Register the "Dread Client" Component with the Archipelago Launcher.

    Idempotent. Silently skips when the ``worlds`` module isn't available
    (unit-test isolation — pytest imports this package without a full
    Archipelago install on sys.path).
    """
    try:
        from worlds.LauncherComponents import (
            Component, SuffixIdentifier, Type, components,
        )
    except ImportError:
        return
    for c in components:
        if c.display_name == "Dread Client":
            return
    components.append(Component(
        "Dread Client",
        func=launch_dread_client,
        component_type=Type.CLIENT,
        file_identifier=SuffixIdentifier('.dreadap'),
        # Matches DreadWorld.game so the Launcher groups the client under the
        # right game and a .dreadap file auto-routes to this Component.
        game_name="Metroid Dread",
    ))


add_client_to_launcher()
