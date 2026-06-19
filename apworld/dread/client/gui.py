"""Kivy UI for DreadClient.

THIS MODULE PULLS KIVY. Never import it from anywhere that runs at apworld
load time — generation hosts may not have a display server. Only
``DreadContext.run_gui()`` reaches it, and run_gui is only called from
client/main.py inside the Launcher subprocess.

Subclasses CommonClient's GameManager. We add:

  * ONE custom tab ("Dread"), split 50/50 horizontally —
      left  : at-a-glance state (AP + Switch wires, slot/seed, scenario,
              item/check counts, goal) refreshed every 1.5 s
      right : a UILog tailing the client logger tree, so context /
              bridge_server diagnostics AND Switch-forwarded device logs
              land in one pane.
  * ONE top-bar widget — a Switches-status pill next to the AP Connect
      button. Clicking it opens a popup that lists every connected Switch
      (active + inactive) with per-row "Promote" buttons.

The Switch is now the TCP dialer (UDP-discovered) so there is no editable
IP field anymore; the popup is purely informational + promote-control.
"""
from __future__ import annotations

import asyncio
import logging
import typing

# IMPORTANT: kvui MUST be imported before any kivy.* module.
from kvui import GameManager, UILog

from kivy.clock import Clock
from kivy.metrics import dp
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.button import Button
from kivy.uix.label import Label
from kivy.uix.popup import Popup
from kivy.uix.scrollview import ScrollView

from .discovery import DEFAULT_DISCOVERY_PORT
from .display import (
    format_patcher_detail,
    format_patcher_pill,
    format_status_panel,
    format_switch_pill,
)

if typing.TYPE_CHECKING:  # pragma: no cover
    from .context import DreadContext


_CLIENT_LOGGER = __name__.rpartition(".")[0]

# Panel + pill refresh cadence. State changes at human speed so 1.5s is enough.
_REFRESH_INTERVAL = 1.5


class _StatusLabel(Label):
    """Markup Label sized to its text, pinned top-left in a ScrollView."""

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
    """Auto-fit pill width to its text; center text vertically."""
    pill.bind(
        texture_size=lambda lbl, sz: setattr(lbl, "width", sz[0] + dp(16)),
        height=lambda lbl, h: setattr(lbl, "text_size", (None, h)),
    )


def _wrapping_label(text: str, height: float, valign: str = "middle") -> Label:
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


class DreadManager(GameManager):
    """Window for the DreadClient.

    One AP-side log tab ("Archipelago", via logging_pairs) plus one custom
    "Dread" tab (state + client/device log tail) and a top-bar Switch-status
    pill that opens the Switches popup.
    """

    logging_pairs = [
        ("Client", "Archipelago"),
    ]
    base_title = "Archipelago Metroid Dread Client"

    def __init__(self, ctx: "DreadContext"):
        super().__init__(ctx)
        self._status_label: _StatusLabel | None = None
        self._switch_pill: Button | None = None
        self._patcher_pill: Button | None = None
        self._client_log: UILog | None = None
        self._switches_popup: "SwitchesPopup | None" = None
        self._patcher_popup: "PatcherPopup | None" = None

    def build(self):
        container = super().build()

        split = BoxLayout(orientation="horizontal", spacing=dp(4))
        left_scroll = ScrollView(do_scroll_x=False, do_scroll_y=True,
                                 size_hint_x=0.5)
        self._status_label = _StatusLabel(text="(starting bridge…)")
        left_scroll.add_widget(self._status_label)
        split.add_widget(left_scroll)

        self._client_log = UILog(logging.getLogger(_CLIENT_LOGGER))
        self._client_log.size_hint_x = 0.5
        split.add_widget(self._client_log)
        self.add_client_tab("Dread", split)

        pill_h = self.server_connect_bar.height
        self._switch_pill = self._make_pill("Bridge: idle", pill_h,
                                            self._open_switches_popup)
        self.connect_layout.add_widget(self._switch_pill)
        # A second top-bar pill for patcher status — visible on every tab so a
        # failed auto-patch is noticed without opening the Dread tab. Click it
        # for the full error.
        self._patcher_pill = self._make_pill("Patcher: idle", pill_h,
                                             self._open_patcher_popup)
        self.connect_layout.add_widget(self._patcher_pill)

        Clock.schedule_interval(self._refresh_panels, _REFRESH_INTERVAL)
        return container

    def _make_pill(self, text: str, height: float, on_release) -> Button:
        """A transparent, auto-fitting top-bar status pill bound to a click
        handler. Shared by the Switch + Patcher pills."""
        pill = Button(
            text=text,
            markup=True,
            size_hint_x=None,
            size_hint_y=None,
            width=dp(150),
            height=height,
            halign="center",
            valign="middle",
            padding=(dp(8), 0),
            pos_hint={"center_y": 0.55},
            text_size=(None, height),
            background_normal="",
            background_down="",
            background_color=(0, 0, 0, 0),
        )
        _bind_pill_layout(pill)
        pill.bind(on_release=on_release)
        return pill

    def _refresh_panels(self, _dt) -> None:
        try:
            snap = self.ctx.state.snapshot()
            if self._status_label is not None:
                self._status_label.text = format_status_panel(snap)
            if self._switch_pill is not None:
                self._switch_pill.text = format_switch_pill(snap)
            if self._patcher_pill is not None:
                self._patcher_pill.text = format_patcher_pill(snap)
            if self._switches_popup is not None and self._switches_popup.is_open:
                self._switches_popup.refresh()
            if self._patcher_popup is not None and self._patcher_popup.is_open:
                self._patcher_popup.refresh()
        except Exception:
            logging.getLogger(_CLIENT_LOGGER).exception("panel refresh failed")

    def _open_switches_popup(self, _button) -> None:
        if self._switches_popup is None:
            self._switches_popup = SwitchesPopup(self.ctx)
        self._switches_popup.refresh()
        self._switches_popup.open()

    def _open_patcher_popup(self, _button) -> None:
        if self._patcher_popup is None:
            self._patcher_popup = PatcherPopup(self.ctx)
        self._patcher_popup.refresh()
        self._patcher_popup.open()


class SwitchesPopup(Popup):
    """Modal popup listing connected Switches with per-row Promote buttons.

    Replaces the old ReconnectPopup. Reads its data from
    ``ctx._bridge.list_switches()`` (a snapshot). Auto-refreshes every
    1.5 s while open via DreadManager._refresh_panels.
    """

    def __init__(self, ctx: "DreadContext"):
        self._ctx = ctx
        self._header: Label | None = None
        self._rows_box: BoxLayout | None = None
        self.is_open = False
        super().__init__(
            title="Switches",
            size_hint=(0.9, 0.7),
            auto_dismiss=True,
        )
        self.bind(
            on_open=lambda *_: setattr(self, "is_open", True),
            on_dismiss=lambda *_: setattr(self, "is_open", False),
        )
        self._build()

    def _build(self) -> None:
        outer = BoxLayout(orientation="vertical", spacing=dp(8), padding=dp(10))
        self._header = _wrapping_label(text="", height=dp(60), valign="top")
        outer.add_widget(self._header)

        scroll = ScrollView(do_scroll_x=False, do_scroll_y=True)
        self._rows_box = BoxLayout(orientation="vertical",
                                   size_hint_y=None,
                                   spacing=dp(4))
        self._rows_box.bind(
            minimum_height=lambda inst, mh: setattr(inst, "height", mh)
        )
        scroll.add_widget(self._rows_box)
        outer.add_widget(scroll)

        self.content = outer

    def refresh(self) -> None:
        ctx = self._ctx
        bridge = ctx._bridge
        if self._header is not None:
            if bridge is None:
                self._header.text = "[color=#ff5555]Bridge not started[/color]"
            else:
                self._header.text = (
                    f"[b]Bridge[/b]: TCP {bridge.host}:{bridge.port} | "
                    f"UDP discovery {DEFAULT_DISCOVERY_PORT}\n"
                    f"[b]Mode[/b]: Switch initiates (no IP entry needed)"
                )

        if self._rows_box is None:
            return
        self._rows_box.clear_widgets()
        if bridge is None:
            self._rows_box.add_widget(_wrapping_label(
                text="Bridge is not running.", height=dp(40)))
            return
        switches = bridge.list_switches()
        if not switches:
            self._rows_box.add_widget(_wrapping_label(
                text="No Switches connected yet. Boot Metroid Dread with the "
                     "patched sysmodule and wait — it will discover this PC "
                     "via UDP and dial in.",
                height=dp(80)))
            return

        for s in switches:
            row = BoxLayout(orientation="horizontal",
                            size_hint_y=None, height=dp(44),
                            spacing=dp(6))
            tag = "[ACTIVE]" if s["active"] else "[inactive]"
            color = "33cc33" if s["active"] else "888888"
            row.add_widget(Label(
                text=f"[color=#{color}][b]{tag}[/b][/color]",
                markup=True,
                size_hint_x=None, width=dp(90),
                halign="center", valign="middle",
                text_size=(dp(90), dp(44)),
            ))
            row.add_widget(_wrapping_label(
                text=(f"[b]{s['device_id']}[/b] peer={s['peer_ip']}\n"
                      f"mod_ver={s['mod_ver']!r} dread={s['dread_ver']!r}"),
                height=dp(44),
            ))
            if not s["active"]:
                btn = Button(text="Promote",
                             size_hint_x=None, width=dp(90),
                             height=dp(44))
                did = s["device_id"]
                btn.bind(on_release=lambda *_args, d=did: self._on_promote(d))
                row.add_widget(btn)
            self._rows_box.add_widget(row)

    def _on_promote(self, device_id: str) -> None:
        bridge = self._ctx._bridge
        if bridge is None:
            return

        async def _go():
            ok = await bridge.manual_promote(device_id)
            logging.getLogger(_CLIENT_LOGGER).info(
                "promote %s: %s", device_id, "OK" if ok else "unknown device_id")
            self.refresh()
        asyncio.ensure_future(_go())


class PatcherPopup(Popup):
    """Modal popup showing the latest patch run's full detail.

    Opened from the top-bar Patcher pill so a failed auto-patch (e.g. an
    unrecognized romfs version) can be read in full without hunting through
    the Dread-tab log. Auto-refreshes while open via
    DreadManager._refresh_panels."""

    def __init__(self, ctx: "DreadContext"):
        self._ctx = ctx
        self._body: Label | None = None
        self.is_open = False
        super().__init__(
            title="Patcher status",
            size_hint=(0.9, 0.7),
            auto_dismiss=True,
        )
        self.bind(
            on_open=lambda *_: setattr(self, "is_open", True),
            on_dismiss=lambda *_: setattr(self, "is_open", False),
        )
        self._build()

    def _build(self) -> None:
        outer = BoxLayout(orientation="vertical", spacing=dp(8), padding=dp(10))
        scroll = ScrollView(do_scroll_x=False, do_scroll_y=True)
        self._body = _StatusLabel(text="")
        scroll.add_widget(self._body)
        outer.add_widget(scroll)
        self.content = outer

    def refresh(self) -> None:
        if self._body is not None:
            self._body.text = format_patcher_detail(self.ctx_snapshot())

    def ctx_snapshot(self) -> dict:
        return self._ctx.state.snapshot()
