"""`artwall tray`: an icon in the system tray, with the current work and quick actions.

It speaks the StatusNotifierItem and dbusmenu D-Bus protocols that KDE Plasma,
DankMaterialShell, Waybar, Cinnamon, XFCE, and GNOME's AppIndicator extension
host. Only Gio is needed, so the tray stays small; the window opens on demand.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import logging
import os
import signal
import sys
from threading import Thread
import time
from typing import Any

import gi

from artwall.gallery import Gallery, Record
from artwall.launcher import APP_ID, DATA

gi.require_version("Gio", "2.0")
from gi.repository import Gio, GLib  # noqa: E402

LOG = logging.getLogger(__name__)
WATCHER = "org.kde.StatusNotifierWatcher"
ITEM_PATH = "/StatusNotifierItem"
MENU_PATH = "/MenuBar"
ICON_THEME = DATA / "icons"
POLL_SECONDS = 30


@dataclass(frozen=True)
class Item:
    """One menu entry: an action, a disabled heading, or a separator."""

    label: str = ""
    action: str | None = None
    checked: bool | None = None  # None: not a checkmark item.
    separator: bool = False


SEPARATOR = Item(separator=True)


def menu(record: Record | None) -> list[Item]:
    items = [Item(record.title), Item(record.byline)] if record else [Item("Nothing on your wall yet")]
    items = [item for item in items if item.label]
    items += [SEPARATOR, Item("Open Artwall", "open"), Item("Explain This Wallpaper", "explain"), SEPARATOR,
              Item("Next Wallpaper", "next")]
    if record:
        items += [Item("Like", "like", record.reaction.includes("like")),
                  Item("Not for Me", "dislike", record.reaction.includes("dislike")),
                  Item("Favorite", "favorite", record.reaction.includes("favorite"))]
    return items + [SEPARATOR, Item("Quit Tray", "quit")]


def tooltip(record: Record | None, next_change: float | None, now: float) -> str:
    lines = [record.title, record.byline] if record else ["Nothing on your wall yet"]
    if next_change is not None and next_change > now:
        hours, rest = divmod(int(next_change - now), 3600)
        lines.append(f"Next wallpaper in {hours} h {rest // 60:02d} min" if hours else
                     f"Next wallpaper in {max(rest // 60, 1)} min")
    return "\n".join(line for line in lines if line)


_INTERFACES = Gio.DBusNodeInfo.new_for_xml("""<node>
<interface name="org.kde.StatusNotifierItem">
  <property name="Category" type="s" access="read"/>
  <property name="Id" type="s" access="read"/>
  <property name="Title" type="s" access="read"/>
  <property name="Status" type="s" access="read"/>
  <property name="WindowId" type="i" access="read"/>
  <property name="IconName" type="s" access="read"/>
  <property name="IconThemePath" type="s" access="read"/>
  <property name="IconPixmap" type="a(iiay)" access="read"/>
  <property name="ToolTip" type="(sa(iiay)ss)" access="read"/>
  <property name="ItemIsMenu" type="b" access="read"/>
  <property name="Menu" type="o" access="read"/>
  <method name="ContextMenu"><arg name="x" type="i" direction="in"/><arg name="y" type="i" direction="in"/></method>
  <method name="Activate"><arg name="x" type="i" direction="in"/><arg name="y" type="i" direction="in"/></method>
  <method name="SecondaryActivate"><arg name="x" type="i" direction="in"/><arg name="y" type="i" direction="in"/></method>
  <method name="Scroll"><arg name="delta" type="i" direction="in"/><arg name="orientation" type="s" direction="in"/></method>
  <signal name="NewTitle"/>
  <signal name="NewIcon"/>
  <signal name="NewToolTip"/>
  <signal name="NewStatus"><arg name="status" type="s"/></signal>
</interface>
<interface name="com.canonical.dbusmenu">
  <property name="Version" type="u" access="read"/>
  <property name="TextDirection" type="s" access="read"/>
  <property name="Status" type="s" access="read"/>
  <property name="IconThemePath" type="as" access="read"/>
  <method name="GetLayout">
    <arg name="parentId" type="i" direction="in"/><arg name="recursionDepth" type="i" direction="in"/>
    <arg name="propertyNames" type="as" direction="in"/>
    <arg name="revision" type="u" direction="out"/><arg name="layout" type="(ia{sv}av)" direction="out"/>
  </method>
  <method name="GetGroupProperties">
    <arg name="ids" type="ai" direction="in"/><arg name="propertyNames" type="as" direction="in"/>
    <arg name="properties" type="a(ia{sv})" direction="out"/>
  </method>
  <method name="GetProperty">
    <arg name="id" type="i" direction="in"/><arg name="name" type="s" direction="in"/>
    <arg name="value" type="v" direction="out"/>
  </method>
  <method name="Event">
    <arg name="id" type="i" direction="in"/><arg name="eventId" type="s" direction="in"/>
    <arg name="data" type="v" direction="in"/><arg name="timestamp" type="u" direction="in"/>
  </method>
  <method name="EventGroup">
    <arg name="events" type="a(isvu)" direction="in"/><arg name="idErrors" type="ai" direction="out"/>
  </method>
  <method name="AboutToShow">
    <arg name="id" type="i" direction="in"/><arg name="needUpdate" type="b" direction="out"/>
  </method>
  <method name="AboutToShowGroup">
    <arg name="ids" type="ai" direction="in"/>
    <arg name="updatesNeeded" type="ai" direction="out"/><arg name="idErrors" type="ai" direction="out"/>
  </method>
  <signal name="ItemsPropertiesUpdated">
    <arg name="updatedProps" type="a(ia{sv})"/><arg name="removedProps" type="a(ias)"/>
  </signal>
  <signal name="LayoutUpdated"><arg name="revision" type="u"/><arg name="parent" type="i"/></signal>
  <signal name="ItemActivationRequested"><arg name="id" type="i"/><arg name="timestamp" type="u"/></signal>
</interface>
</node>""")


def _properties(item: Item) -> dict[str, Any]:
    if item.separator:
        return {"type": GLib.Variant("s", "separator")}
    # Underscores mark mnemonics in dbusmenu labels, so a title's own are doubled.
    props = {"label": GLib.Variant("s", item.label.replace("_", "__")),
             "enabled": GLib.Variant("b", item.action is not None)}
    if item.checked is not None:
        props["toggle-type"] = GLib.Variant("s", "checkmark")
        props["toggle-state"] = GLib.Variant("i", int(item.checked))
    return props


def _pixmaps() -> list[tuple[int, int, bytes]]:
    """The icon as ARGB pixmaps, for trays that cannot look up icon names."""
    try:
        gi.require_version("GdkPixbuf", "2.0")
        from gi.repository import GdkPixbuf
    except (ImportError, ValueError):
        return []
    source = ICON_THEME / "hicolor" / "scalable" / "apps" / f"{APP_ID}.svg"
    pixmaps = []
    for size in (22, 32, 48, 64):
        try:
            pixbuf = GdkPixbuf.Pixbuf.new_from_file_at_size(str(source), size, size)
        except GLib.Error:
            break
        if not pixbuf.get_has_alpha():
            pixbuf = pixbuf.add_alpha(False, 0, 0, 0)
        width, height, stride = pixbuf.get_width(), pixbuf.get_height(), pixbuf.get_rowstride()
        data = pixbuf.get_pixels()
        rgba = b"".join(data[row * stride:row * stride + width * 4] for row in range(height))
        argb = bytearray(len(rgba))
        argb[0::4], argb[1::4], argb[2::4], argb[3::4] = rgba[3::4], rgba[0::4], rgba[1::4], rgba[2::4]
        pixmaps.append((width, height, bytes(argb)))
    return pixmaps


class Tray:
    def __init__(self, gallery: Gallery, bus: Any, loop: Any) -> None:
        self.gallery = gallery
        self.bus = bus
        self.loop = loop
        self.name = f"org.kde.StatusNotifierItem-{os.getpid()}-1"
        self.record: Record | None = None
        self.items: list[Item] = menu(None)
        self.revision = 1
        self.tip = ""
        self.pixmaps = _pixmaps()

    def start(self) -> None:
        item, dbusmenu = _INTERFACES.interfaces
        self.bus.register_object(ITEM_PATH, item, self._item_call, self._item_property, None)
        self.bus.register_object(MENU_PATH, dbusmenu, self._menu_call, self._menu_property, None)
        self.bus.call_sync("org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus", "RequestName",
                           GLib.Variant("(su)", (self.name, 4)), None, Gio.DBusCallFlags.NONE, -1, None)
        self.refresh()
        Gio.bus_watch_name_on_connection(self.bus, WATCHER, Gio.BusNameWatcherFlags.NONE,
                                         self._watcher_appeared, self._watcher_vanished)
        GLib.timeout_add_seconds(POLL_SECONDS, lambda: self.refresh() or True)

    # Registration with the tray host

    def _watcher_appeared(self, *_: Any) -> None:
        self.bus.call(WATCHER, "/StatusNotifierWatcher", WATCHER, "RegisterStatusNotifierItem",
                      GLib.Variant("(s)", (self.name,)), None, Gio.DBusCallFlags.NONE, -1, None, self._registered)

    def _registered(self, bus: Any, result: Any) -> None:
        try:
            bus.call_finish(result)
        except GLib.Error as exc:
            LOG.warning("The system tray refused the Artwall icon: %s", exc.message)

    def _watcher_vanished(self, *_: Any) -> None:
        LOG.warning("No system tray is running; the icon appears once one starts. "
                    "(GNOME needs the AppIndicator and KStatusNotifierItem Support extension.)")

    # State

    def refresh(self) -> bool:
        """Read the current wallpaper again and tell the tray host what changed."""
        try:
            self.record = self.gallery.current()
            next_change = self.gallery.next_change()
        except Exception as exc:  # A locked or missing database must not stop the tray.
            LOG.warning("Could not read the gallery: %s", exc)
            return False
        items = menu(self.record)
        changed = items != self.items
        if changed:
            self.items = items
            self.revision += 1
            self._emit(MENU_PATH, "com.canonical.dbusmenu", "LayoutUpdated", GLib.Variant("(ui)", (self.revision, 0)))
        tip = tooltip(self.record, next_change, time.time())
        if tip != self.tip:
            self.tip = tip
            self._emit(ITEM_PATH, "org.kde.StatusNotifierItem", "NewToolTip", None)
        return changed

    def _emit(self, path: str, interface: str, name: str, parameters: Any) -> None:
        try:
            self.bus.emit_signal(None, path, interface, name, parameters)
        except GLib.Error as exc:
            LOG.debug("signal %s failed: %s", name, exc.message)

    # Actions

    def act(self, action: str) -> None:
        if action == "quit":
            self.loop.quit()
        elif action in ("open", "explain"):
            # The app is single-instance, so this reaches an open window when there is one.
            Gio.Subprocess.new([sys.executable, "-P", "-m", "artwall.cli", "gui" if action == "open" else "explain"],
                               Gio.SubprocessFlags.NONE)
        elif action == "next":
            self._background(self.gallery.show_next)
        elif action in ("like", "dislike", "favorite") and self.record is not None:
            record = self.record.id
            self._background(lambda: self.gallery.vote(record, action, expected_current=record))  # type: ignore[arg-type]

    def _background(self, work: Callable[[], str]) -> None:
        """Votes and rotation can call desktop commands, so they run off the D-Bus loop."""
        def run() -> None:
            try:
                work()
                failure = None
            except ValueError as exc:
                failure = str(exc)
            except Exception as exc:
                LOG.warning("tray action failed: %s", exc)
                failure = "Something went wrong. See the tray service log."
            GLib.idle_add(self._done, failure)

        Thread(target=run, daemon=True).start()

    def _done(self, failure: str | None) -> bool:
        self.refresh()
        # An open Artwall window shows the change too.
        self.bus.call(APP_ID, "/" + APP_ID.replace(".", "/"), "org.gtk.Actions", "Activate",
                      GLib.Variant("(sava{sv})", ("refresh", [], {})), None, Gio.DBusCallFlags.NO_AUTO_START, 2000,
                      None, _ignore)
        if failure:
            self.bus.call("org.freedesktop.Notifications", "/org/freedesktop/Notifications",
                          "org.freedesktop.Notifications", "Notify",
                          GLib.Variant("(susssasa{sv}i)", ("Artwall", 0, APP_ID, "Artwall", failure, [], {}, 5000)),
                          None, Gio.DBusCallFlags.NONE, 2000, None, _ignore)
        return False

    # org.kde.StatusNotifierItem

    def _item_property(self, _bus: Any, _sender: str, _path: str, _interface: str, name: str) -> Any:
        values = {
            "Category": GLib.Variant("s", "ApplicationStatus"),
            "Id": GLib.Variant("s", "artwall"),
            "Title": GLib.Variant("s", "Artwall"),
            "Status": GLib.Variant("s", "Active"),
            "WindowId": GLib.Variant("i", 0),
            "IconName": GLib.Variant("s", APP_ID),
            "IconThemePath": GLib.Variant("s", str(ICON_THEME)),
            "IconPixmap": GLib.Variant("a(iiay)", self.pixmaps),
            "ToolTip": GLib.Variant("(sa(iiay)ss)", (APP_ID, [], "Artwall", self.tip)),
            "ItemIsMenu": GLib.Variant("b", False),
            "Menu": GLib.Variant("o", MENU_PATH),
        }
        return values.get(name)

    def _item_call(self, _bus: Any, _sender: str, _path: str, _interface: str, method: str, _parameters: Any,
                   invocation: Any) -> None:
        if method == "Activate":
            self.act("open")
        elif method == "SecondaryActivate":
            self.act("explain")
        invocation.return_value(None)  # ContextMenu and Scroll: the host shows the menu itself.

    # com.canonical.dbusmenu

    def _menu_property(self, _bus: Any, _sender: str, _path: str, _interface: str, name: str) -> Any:
        values = {"Version": GLib.Variant("u", 3), "TextDirection": GLib.Variant("s", "ltr"),
                  "Status": GLib.Variant("s", "normal"), "IconThemePath": GLib.Variant("as", [str(ICON_THEME)])}
        return values.get(name)

    def _item(self, number: int) -> Item | None:
        return self.items[number - 1] if 1 <= number <= len(self.items) else None

    def _layout(self, parent: int, depth: int) -> Any:
        if parent != 0:
            item = self._item(parent)
            return (parent, _properties(item) if item else {}, [])
        children = [GLib.Variant("(ia{sv}av)", (number, _properties(item), []))
                    for number, item in enumerate(self.items, start=1)] if depth != 0 else []
        return (0, {"children-display": GLib.Variant("s", "submenu")}, children)

    def _menu_call(self, _bus: Any, _sender: str, _path: str, _interface: str, method: str, parameters: Any,
                   invocation: Any) -> None:
        args = parameters.unpack()
        if method == "GetLayout":
            invocation.return_value(GLib.Variant("(u(ia{sv}av))", (self.revision, self._layout(args[0], args[1]))))
        elif method == "GetGroupProperties":
            ids = args[0] or range(1, len(self.items) + 1)
            found = [(number, _properties(item)) for number in ids if (item := self._item(number))]
            invocation.return_value(GLib.Variant("(a(ia{sv}))", (found,)))
        elif method == "GetProperty":
            item = self._item(args[0])
            value = _properties(item).get(args[1]) if item else None
            if value is None:
                invocation.return_dbus_error("com.canonical.dbusmenu.Error", "No such menu item property")
            else:
                invocation.return_value(GLib.Variant("(v)", (value,)))
        elif method == "Event":
            self._event(args[0], args[1])
            invocation.return_value(None)
        elif method == "EventGroup":
            errors = [number for number, event, _data, _time in args[0] if not self._event(number, event)]
            invocation.return_value(GLib.Variant("(ai)", (errors,)))
        elif method == "AboutToShow":
            invocation.return_value(GLib.Variant("(b)", (self.refresh(),)))
        elif method == "AboutToShowGroup":
            invocation.return_value(GLib.Variant("(aiai)", ([0] if self.refresh() else [], [])))
        else:
            invocation.return_dbus_error("org.freedesktop.DBus.Error.UnknownMethod", method)

    def _event(self, number: int, event: str) -> bool:
        item = self._item(number)
        if item is None:
            return False
        if event == "clicked" and item.action:
            self.act(item.action)
        return True


def _ignore(bus: Any, result: Any) -> None:
    try:
        bus.call_finish(result)
    except GLib.Error:
        pass  # The app is not open, or no notification server is running.


def run_tray(gallery: Gallery | None = None) -> None:
    loop = GLib.MainLoop()
    tray = Tray(gallery or Gallery(), Gio.bus_get_sync(Gio.BusType.SESSION, None), loop)
    tray.start()
    for number in (signal.SIGINT, signal.SIGTERM):
        GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, number, lambda: loop.quit() or False)
    loop.run()
