"""System tray icon over D-Bus (StatusNotifierItem + com.canonical.dbusmenu).

GTK4 has no tray API, so this speaks the protocol KDE Plasma (and GNOME with
the AppIndicator extension, waybar, etc.) use, with nothing but Gio.
"""

import os

from gi.repository import Gio, GLib

WATCHER = "org.kde.StatusNotifierWatcher"
ITEM_PATH = "/StatusNotifierItem"
MENU_PATH = "/MenuBar"

SNI_XML = """
<node>
  <interface name="org.kde.StatusNotifierItem">
    <property name="Category" type="s" access="read"/>
    <property name="Id" type="s" access="read"/>
    <property name="Title" type="s" access="read"/>
    <property name="Status" type="s" access="read"/>
    <property name="WindowId" type="i" access="read"/>
    <property name="IconName" type="s" access="read"/>
    <property name="IconPixmap" type="a(iiay)" access="read"/>
    <property name="IconThemePath" type="s" access="read"/>
    <property name="OverlayIconName" type="s" access="read"/>
    <property name="AttentionIconName" type="s" access="read"/>
    <property name="ToolTip" type="(sa(iiay)ss)" access="read"/>
    <property name="ItemIsMenu" type="b" access="read"/>
    <property name="Menu" type="o" access="read"/>
    <method name="Activate"><arg name="x" type="i" direction="in"/><arg name="y" type="i" direction="in"/></method>
    <method name="SecondaryActivate"><arg name="x" type="i" direction="in"/><arg name="y" type="i" direction="in"/></method>
    <method name="ContextMenu"><arg name="x" type="i" direction="in"/><arg name="y" type="i" direction="in"/></method>
    <method name="Scroll"><arg name="delta" type="i" direction="in"/><arg name="orientation" type="s" direction="in"/></method>
    <signal name="NewTitle"/>
    <signal name="NewIcon"/>
    <signal name="NewToolTip"/>
    <signal name="NewStatus"><arg name="status" type="s"/></signal>
  </interface>
</node>
"""

MENU_XML = """
<node>
  <interface name="com.canonical.dbusmenu">
    <property name="Version" type="u" access="read"/>
    <property name="TextDirection" type="s" access="read"/>
    <property name="Status" type="s" access="read"/>
    <property name="IconThemePath" type="as" access="read"/>
    <method name="GetLayout">
      <arg name="parentId" type="i" direction="in"/>
      <arg name="recursionDepth" type="i" direction="in"/>
      <arg name="propertyNames" type="as" direction="in"/>
      <arg name="revision" type="u" direction="out"/>
      <arg name="layout" type="(ia{sv}av)" direction="out"/>
    </method>
    <method name="GetGroupProperties">
      <arg name="ids" type="ai" direction="in"/>
      <arg name="propertyNames" type="as" direction="in"/>
      <arg name="properties" type="a(ia{sv})" direction="out"/>
    </method>
    <method name="GetProperty">
      <arg name="id" type="i" direction="in"/>
      <arg name="name" type="s" direction="in"/>
      <arg name="value" type="v" direction="out"/>
    </method>
    <method name="Event">
      <arg name="id" type="i" direction="in"/>
      <arg name="eventId" type="s" direction="in"/>
      <arg name="data" type="v" direction="in"/>
      <arg name="timestamp" type="u" direction="in"/>
    </method>
    <method name="EventGroup">
      <arg name="events" type="a(isvu)" direction="in"/>
      <arg name="idErrors" type="ai" direction="out"/>
    </method>
    <method name="AboutToShow">
      <arg name="id" type="i" direction="in"/>
      <arg name="needUpdate" type="b" direction="out"/>
    </method>
    <method name="AboutToShowGroup">
      <arg name="ids" type="ai" direction="in"/>
      <arg name="updatesNeeded" type="ai" direction="out"/>
      <arg name="idErrors" type="ai" direction="out"/>
    </method>
    <signal name="ItemsPropertiesUpdated">
      <arg name="updatedProps" type="a(ia{sv})"/>
      <arg name="removedProps" type="a(ias)"/>
    </signal>
    <signal name="LayoutUpdated">
      <arg name="revision" type="u"/>
      <arg name="parent" type="i"/>
    </signal>
    <signal name="ItemActivationRequested">
      <arg name="id" type="i"/>
      <arg name="timestamp" type="u"/>
    </signal>
  </interface>
</node>
"""


class MenuItem:
    def __init__(self, label="", callback=None, enabled=True, separator=False):
        self.label = label
        self.callback = callback
        self.enabled = enabled
        self.separator = separator

    def props(self):
        if self.separator:
            return {"type": GLib.Variant("s", "separator")}
        return {"label": GLib.Variant("s", self.label), "enabled": GLib.Variant("b", self.enabled)}


class TrayIcon:
    """A tray icon with a tooltip and a flat menu.

    `on_activate` runs on left click; `available` tells whether a tray host is
    running (if not, nothing is shown and the app should not hide into it).
    """

    def __init__(self, app_id, title, on_activate, on_available_changed=None):
        self.app_id = app_id
        self.title = title
        self.on_activate = on_activate
        self.on_available_changed = on_available_changed
        self.icon_name = "input-mouse"
        self.icon_pixmaps = []  # [(w, h, argb32)]; used instead of icon_name when set
        self._icon_key = None
        self.tooltip = (title, "")
        self.items = []  # menu item ids are list index + 1; 0 is the root
        self.revision = 1
        self.available = False

        self.bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        self.bus_name = f"org.kde.StatusNotifierItem-{os.getpid()}-1"
        sni = Gio.DBusNodeInfo.new_for_xml(SNI_XML).interfaces[0]
        menu = Gio.DBusNodeInfo.new_for_xml(MENU_XML).interfaces[0]
        self._ids = [
            self.bus.register_object(ITEM_PATH, sni, self._sni_call, self._sni_get, None),
            self.bus.register_object(MENU_PATH, menu, self._menu_call, self._menu_get, None),
        ]
        self._own = Gio.bus_own_name_on_connection(self.bus, self.bus_name, Gio.BusNameOwnerFlags.NONE, None, None)
        # Register now and again whenever the tray host (re)starts, e.g. plasmashell restarts.
        self._watch = Gio.bus_watch_name_on_connection(
            self.bus, WATCHER, Gio.BusNameWatcherFlags.NONE, self._watcher_appeared, self._watcher_vanished)

    # -- public -----------------------------------------------------------

    def set_icon(self, icon_name):
        self._set_icon(("name", icon_name), icon_name, [])

    def set_icon_pixmaps(self, key, pixmaps):
        """Show rendered images; `key` identifies them so unchanged icons aren't resent."""
        # Hosts prefer IconName over IconPixmap, so clear the name.
        self._set_icon(("pixmaps", key), "", pixmaps)

    def _set_icon(self, key, icon_name, pixmaps):
        if key != self._icon_key:
            self._icon_key = key
            self.icon_name, self.icon_pixmaps = icon_name, pixmaps
            self._emit(ITEM_PATH, "org.kde.StatusNotifierItem", "NewIcon")

    def set_tooltip(self, title, body=""):
        if (title, body) != self.tooltip:
            self.tooltip = (title, body)
            self._emit(ITEM_PATH, "org.kde.StatusNotifierItem", "NewToolTip")

    def set_menu(self, items):
        old = [(i.label, i.enabled, i.separator) for i in self.items]
        self.items = items
        if old != [(i.label, i.enabled, i.separator) for i in items]:
            self.revision += 1
            self._emit(MENU_PATH, "com.canonical.dbusmenu", "LayoutUpdated",
                       GLib.Variant("(ui)", (self.revision, 0)))

    def close(self):
        Gio.bus_unwatch_name(self._watch)
        Gio.bus_unown_name(self._own)
        for rid in self._ids:
            self.bus.unregister_object(rid)

    # -- registration ------------------------------------------------------

    def _watcher_appeared(self, conn, name, owner):
        conn.call(WATCHER, "/StatusNotifierWatcher", WATCHER, "RegisterStatusNotifierItem",
                  GLib.Variant("(s)", (self.bus_name,)), None, Gio.DBusCallFlags.NONE, -1, None,
                  self._registered)

    def _registered(self, conn, result):
        try:
            conn.call_finish(result)
            self._set_available(True)
        except GLib.Error:
            self._set_available(False)

    def _watcher_vanished(self, conn, name):
        self._set_available(False)

    def _set_available(self, available):
        if available != self.available:
            self.available = available
            if self.on_available_changed:
                self.on_available_changed(available)

    def _emit(self, path, iface, signal, params=None):
        try:
            self.bus.emit_signal(None, path, iface, signal, params)
        except GLib.Error:
            pass

    # -- org.kde.StatusNotifierItem ---------------------------------------

    def _sni_get(self, conn, sender, path, iface, prop):
        values = {
            "Category": GLib.Variant("s", "Hardware"),
            "Id": GLib.Variant("s", self.app_id),
            "Title": GLib.Variant("s", self.title),
            "Status": GLib.Variant("s", "Active"),
            "WindowId": GLib.Variant("i", 0),
            "IconName": GLib.Variant("s", self.icon_name),
            "IconPixmap": GLib.Variant("a(iiay)", self.icon_pixmaps),
            "IconThemePath": GLib.Variant("s", ""),
            "OverlayIconName": GLib.Variant("s", ""),
            "AttentionIconName": GLib.Variant("s", ""),
            "ToolTip": GLib.Variant("(sa(iiay)ss)", ("", [], self.tooltip[0], self.tooltip[1])),
            "ItemIsMenu": GLib.Variant("b", False),
            "Menu": GLib.Variant("o", MENU_PATH),
        }
        return values.get(prop)

    def _sni_call(self, conn, sender, path, iface, method, params, invocation):
        if method in ("Activate", "SecondaryActivate"):
            GLib.idle_add(self.on_activate)
        invocation.return_value(None)

    # -- com.canonical.dbusmenu --------------------------------------------

    def _menu_get(self, conn, sender, path, iface, prop):
        return {
            "Version": GLib.Variant("u", 3),
            "TextDirection": GLib.Variant("s", "ltr"),
            "Status": GLib.Variant("s", "normal"),
            "IconThemePath": GLib.Variant("as", []),
        }.get(prop)

    def _item_props(self, item_id):
        if item_id == 0:
            return {"children-display": GLib.Variant("s", "submenu")}
        if 1 <= item_id <= len(self.items):
            return self.items[item_id - 1].props()
        return {}

    def _layout(self, item_id, with_children):
        children = []
        if item_id == 0 and with_children:
            children = [GLib.Variant("(ia{sv}av)", (i + 1, self._item_props(i + 1), []))
                        for i in range(len(self.items))]
        return (item_id, self._item_props(item_id), children)

    def _menu_call(self, conn, sender, path, iface, method, params, invocation):
        args = params.unpack()
        if method == "GetLayout":
            parent, depth, _names = args
            invocation.return_value(GLib.Variant("(u(ia{sv}av))", (self.revision, self._layout(parent, depth != 0))))
        elif method == "GetGroupProperties":
            ids, _names = args
            invocation.return_value(GLib.Variant("(a(ia{sv}))", ([(i, self._item_props(i)) for i in ids],)))
        elif method == "GetProperty":
            item_id, name = args
            value = self._item_props(item_id).get(name, GLib.Variant("s", ""))
            invocation.return_value(GLib.Variant("(v)", (value,)))
        elif method == "Event":
            item_id, event, _data, _ts = args
            self._event(item_id, event)
            invocation.return_value(None)
        elif method == "EventGroup":
            for item_id, event, _data, _ts in args[0]:
                self._event(item_id, event)
            invocation.return_value(GLib.Variant("(ai)", ([],)))
        elif method == "AboutToShow":
            invocation.return_value(GLib.Variant("(b)", (False,)))
        elif method == "AboutToShowGroup":
            invocation.return_value(GLib.Variant("(aiai)", ([], [])))
        else:
            invocation.return_value(None)

    def _event(self, item_id, event):
        if event == "clicked" and 1 <= item_id <= len(self.items):
            callback = self.items[item_id - 1].callback
            if callback:
                GLib.idle_add(callback)
