"""Kivy UI for DreadClient.

THIS MODULE PULLS KIVY. Never import it from anywhere that runs at apworld
load time — generation hosts may not have a display server. Only
``DreadContext.run_gui()`` reaches it, and run_gui is only called from
client/main.py inside the Launcher subprocess.

Subclasses CommonClient's GameManager, which already provides:
  - top bar: AP server-address input + Connect button + a thin progress bar
    bound to checked/missing AP locations
  - log tab: "Archipelago" (AP/Client-side logger output)
  - "Hints" tab (built-in)
  - bottom bar: Command: button + command prompt

We add:
  * ONE custom tab ("Dread"), split 50/50 horizontally —
      left  : at-a-glance state (AP + Switch wires, slot/seed, scenario,
              item/check counts, goal) refreshed every 1.5 s
      right : a UILog tailing the client logger tree, so context/executor
              diagnostics AND Switch-forwarded device logs land in one pane.
  * ONE top-bar widget — a Switch-status pill next to the AP Connect button.
      Clicking it opens a small popup with the current host:port, an editable
      Switch IP field, and a Reconnect button. This is the recovery hatch for
      the Switch dial losing the race with Dreadvania's own startup. The same
      action is available as ``/dread_connect [ip[:port]]``.
"""
from __future__ import annotations

import asyncio
import logging
import typing

# IMPORTANT: kvui MUST be imported before any kivy.* module. kvui asserts
# `"kivy" not in sys.modules` at module top (for frozen-build compatibility),
# so any prior `from kivy.X import Y` here would trip the assert and prevent
# the GUI from starting. Same reason Wargroove (and SMO) imports kvui first.
from kvui import GameManager, UILog

from kivy.clock import Clock
from kivy.metrics import dp
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.button import Button
from kivy.uix.label import Label
from kivy.uix.popup import Popup
from kivy.uix.scrollview import ScrollView
from kivy.uix.textinput import TextInput

from .commands import parse_switch_target
from .display import format_status_panel, format_switch_pill

if typing.TYPE_CHECKING:  # pragma: no cover
    from .context import DreadContext


# Parent package logger ("<pkg>.client"); the right-hand log pane tails it so
# child-module records (context, lua_executor) and Switch-forwarded device
# logs propagate in. Computed from __name__ so it matches whether installed as
# worlds.dread.* or apworld.dread.* — same derivation context.py uses.
_CLIENT_LOGGER = __name__.rpartition(".")[0]

# Panel + pill refresh cadence. State changes at human speed (item arrivals,
# check collects, connect/disconnect), so 1.5 s mirrors SMO and keeps Kivy's
# frame budget free.
_REFRESH_INTERVAL = 1.5


class _StatusLabel(Label):
    """Markup Label sized to its text content, pinned top-left in a ScrollView.

    Kivy's default Label is fixed-height and clips; this binds height to
    texture_size so a growing status block scrolls instead of truncating.
    Copied from SMO's _LiveLabel.
    """

    def __init__(self, **kwargs):
        super().__init__(
            markup=True,
            valign="top",
            halign="left",
            size_hint_y=None,
            padding=(dp(10), dp(10)),
            **kwargs,
        )
        self.bind(width=self._refit, texture_size=self._refit)

    def _refit(self, *_):
        self.text_size = (self.width - dp(20), None)
        self.height = max(self.texture_size[1] + dp(20), dp(60))


def _bind_pill_layout(pill: Label) -> None:
    """Auto-fit pill width to its text; center text vertically.

    Width binds to the text texture (+pad) so the pill can't overflow the top
    bar — the AP server input absorbs the rest. Vertical centering needs
    text_size[1] set (to the widget height) while text_size[0] stays None.
    Binding both axes causes a width⇄texture feedback runaway (see SMO's
    _bind_switch_pill_layout for the full explanation).
    """
    pill.bind(
        texture_size=lambda lbl, sz: setattr(lbl, "width", sz[0] + dp(16)),
        height=lambda lbl, h: setattr(lbl, "text_size", (None, h)),
    )


class DreadManager(GameManager):
    """Window for the DreadClient.

    One AP-side log tab ("Archipelago", via logging_pairs) plus one custom
    "Dread" tab (state + client/device log tail) and a top-bar Switch-status
    pill that opens the reconnect popup.
    """

    logging_pairs = [
        ("Client", "Archipelago"),
        # The client logger tree (_CLIENT_LOGGER) is intentionally NOT a
        # logging_pairs entry — it's rendered in the right half of the Dread
        # tab via a manually-managed UILog (see build()), co-locating state
        # and diagnostics in one eye-line.
    ]
    base_title = "Archipelago Metroid Dread Client"

    def __init__(self, ctx: "DreadContext"):
        super().__init__(ctx)
        self._status_label: _StatusLabel | None = None
        self._switch_pill: Button | None = None
        self._client_log: UILog | None = None
        self._reconnect_popup: "ReconnectPopup | None" = None

    def build(self):
        container = super().build()

        # Dread tab: horizontal 50/50 split.
        split = BoxLayout(orientation="horizontal", spacing=dp(4))

        left_scroll = ScrollView(do_scroll_x=False, do_scroll_y=True,
                                 size_hint_x=0.5)
        self._status_label = _StatusLabel(text="(connecting…)")
        left_scroll.add_widget(self._status_label)
        split.add_widget(left_scroll)

        # UILog attaches a LogtoUI handler to the passed logger; records on
        # child loggers propagate up to it, so context/executor diagnostics
        # and Switch-forwarded device logs all tail here.
        self._client_log = UILog(logging.getLogger(_CLIENT_LOGGER))
        self._client_log.size_hint_x = 0.5
        split.add_widget(self._client_log)

        self.add_client_tab("Dread", split)

        # Switch status pill, appended to the top connect_layout (which holds
        # the AP server input + Connect button). Mirrors LADX's "Open Tracker"
        # placement — AP users expect connection state for all wires up here.
        # Height + pos_hint mirror the Connect button so the pill lines up.
        pill_h = self.server_connect_bar.height
        self._switch_pill = Button(
            text="Switch: off",
            markup=True,
            size_hint_x=None,
            size_hint_y=None,
            width=dp(110),
            height=pill_h,
            halign="center",
            valign="middle",
            padding=(dp(8), 0),
            pos_hint={"center_y": 0.55},
            text_size=(None, pill_h),
            # Flatten the button chrome so color carries the state, not an
            # outline (matches the SMO pill look).
            background_normal="",
            background_down="",
            background_color=(0, 0, 0, 0),
        )
        _bind_pill_layout(self._switch_pill)
        self._switch_pill.bind(on_release=self._open_reconnect_popup)
        self.connect_layout.add_widget(self._switch_pill)

        Clock.schedule_interval(self._refresh_panels, _REFRESH_INTERVAL)
        return container

    def _refresh_panels(self, _dt) -> None:
        try:
            snap = self.ctx.state.snapshot()
            if self._status_label is not None:
                self._status_label.text = format_status_panel(snap)
            if self._switch_pill is not None:
                self._switch_pill.text = format_switch_pill(snap)
            if self._reconnect_popup is not None and self._reconnect_popup.is_open:
                self._reconnect_popup.refresh()
        except Exception:
            # Don't let a transient render error kill the scheduled refresh;
            # Clock.schedule_interval cancels the callback on exception.
            logging.getLogger(_CLIENT_LOGGER).exception("panel refresh failed")

    def _open_reconnect_popup(self, _button) -> None:
        if self._reconnect_popup is None:
            self._reconnect_popup = ReconnectPopup(self.ctx)
        self._reconnect_popup.refresh()
        self._reconnect_popup.open()


class ReconnectPopup(Popup):
    """Modal popup to retry / re-point the Switch connection.

    Shows the current ``host:port`` and live status, an editable Switch IP
    field (``ip`` or ``ip:port``), and a Reconnect button. Reconnect parses the
    field via ``parse_switch_target``, updates ``ctx.switch_host``/
    ``switch_port``, and schedules ``ctx.reconnect_switch()`` on the running
    loop (button callbacks fire on the asyncio loop under App.async_run, same
    as the /dread_connect command path).
    """

    def __init__(self, ctx: "DreadContext"):
        self._ctx = ctx
        self._status_label: Label | None = None
        self._input: TextInput | None = None
        self.is_open = False
        super().__init__(
            title="Switch connection",
            size_hint=(0.8, 0.5),
            auto_dismiss=True,
        )
        self.bind(
            on_open=lambda *_: setattr(self, "is_open", True),
            on_dismiss=lambda *_: setattr(self, "is_open", False),
        )
        self._build()

    def _build(self) -> None:
        outer = BoxLayout(orientation="vertical", spacing=dp(8), padding=dp(10))

        self._status_label = _wrapping_label(text="", height=dp(60), valign="top")
        outer.add_widget(self._status_label)

        row = BoxLayout(orientation="horizontal", size_hint_y=None,
                        height=dp(36), spacing=dp(8))
        row.add_widget(Label(
            text="Switch IP:", size_hint_x=None, width=dp(90),
            halign="right", valign="middle", text_size=(dp(90), dp(36)),
        ))
        self._input = TextInput(
            multiline=False, write_tab=False,
            size_hint_y=None, height=dp(36),
        )
        # Enter in the field acts like pressing Reconnect.
        self._input.bind(on_text_validate=lambda *_: self._on_reconnect())
        row.add_widget(self._input)
        outer.add_widget(row)

        btn = Button(text="Reconnect", size_hint_y=None, height=dp(40))
        btn.bind(on_release=lambda *_: self._on_reconnect())
        outer.add_widget(btn)

        # Spacer pushes the controls to the top of the popup body.
        outer.add_widget(Label(size_hint_y=1))
        self.content = outer

    def refresh(self) -> None:
        ctx = self._ctx
        snap = ctx.state.snapshot()
        conn = snap.get("switch_conn", "disconnected") or "disconnected"
        if self._status_label is not None:
            self._status_label.text = (
                f"[b]Target[/b]: {ctx.switch_host}:{ctx.switch_port}\n"
                f"[b]Status[/b]: {conn}"
            )
        # Prefill the field with the live target — but never while the user is
        # typing (refresh fires every 1.5 s whenever the popup is open).
        if self._input is not None and not self._input.focus:
            self._input.text = f"{ctx.switch_host}:{ctx.switch_port}"

    def _on_reconnect(self) -> None:
        ctx = self._ctx
        raw = (self._input.text if self._input else "").strip()
        if raw:
            try:
                host, port = parse_switch_target(raw)
            except ValueError as exc:
                logging.getLogger(_CLIENT_LOGGER).warning(
                    "bad switch target %r: %s", raw, exc)
                if self._status_label is not None:
                    self._status_label.text = f"[color=#ff9800]Invalid: {exc}[/color]"
                return
            ctx.switch_host = host
            if port is not None:
                ctx.switch_port = port
        logging.getLogger(_CLIENT_LOGGER).info(
            "reconnecting to Switch at %s:%s", ctx.switch_host, ctx.switch_port)
        asyncio.ensure_future(ctx.reconnect_switch())
        self.refresh()


def _wrapping_label(text: str, height: float, valign: str = "middle") -> Label:
    """Label that wraps text to its own current width.

    Binding text_size to the live width avoids the popup compressed-text bug:
    refresh() builds labels before open(), when the popup width is still the
    default, so a static text_size would bake that narrow wrap into the
    texture. Copied from SMO.
    """
    lbl = Label(
        text=text,
        markup=True,
        halign="left",
        valign=valign,
        size_hint_y=None,
        height=height,
        text_size=(None, None),
    )
    lbl.bind(width=lambda inst, w: setattr(inst, "text_size", (w - dp(8), None)))
    return lbl
