"""StatusNotifierItem and its menu for the desktop notification area."""

import gi
gi.require_version('GdkPixbuf', '2.0')
from gi.repository import GdkPixbuf, Gio, GLib

from .desktop import APP_ICON, APP_NAME

ITEM_PATH = '/StatusNotifierItem'
MENU_PATH = '/MenuBar'
WATCHER = 'org.kde.StatusNotifierWatcher'

ITEM = Gio.DBusNodeInfo.new_for_xml('''
<node><interface name="org.kde.StatusNotifierItem">
  <property name="Category" type="s" access="read"/>
  <property name="Id" type="s" access="read"/>
  <property name="Title" type="s" access="read"/>
  <property name="Status" type="s" access="read"/>
  <property name="WindowId" type="i" access="read"/>
  <property name="ItemIsMenu" type="b" access="read"/>
  <property name="Menu" type="o" access="read"/>
  <property name="IconName" type="s" access="read"/>
  <property name="IconPixmap" type="a(iiay)" access="read"/>
  <property name="IconThemePath" type="s" access="read"/>
  <property name="OverlayIconName" type="s" access="read"/>
  <property name="OverlayIconPixmap" type="a(iiay)" access="read"/>
  <property name="AttentionIconName" type="s" access="read"/>
  <property name="AttentionIconPixmap" type="a(iiay)" access="read"/>
  <property name="AttentionMovieName" type="s" access="read"/>
  <method name="Activate"><arg type="i" direction="in"/><arg type="i" direction="in"/></method>
  <method name="SecondaryActivate"><arg type="i" direction="in"/><arg type="i" direction="in"/></method>
  <method name="ContextMenu"><arg type="i" direction="in"/><arg type="i" direction="in"/></method>
  <method name="Scroll"><arg type="i" direction="in"/><arg type="s" direction="in"/></method>
  <signal name="NewIcon"/>
</interface></node>
''').interfaces[0]

MENU = Gio.DBusNodeInfo.new_for_xml('''
<node><interface name="com.canonical.dbusmenu">
  <property name="Version" type="u" access="read"/>
  <property name="TextDirection" type="s" access="read"/>
  <property name="Status" type="s" access="read"/>
  <property name="IconThemePath" type="as" access="read"/>
  <method name="GetLayout">
    <arg type="i" direction="in"/><arg type="i" direction="in"/><arg type="as" direction="in"/>
    <arg type="u" direction="out"/><arg type="(ia{sv}av)" direction="out"/>
  </method>
  <method name="GetGroupProperties">
    <arg type="ai" direction="in"/><arg type="as" direction="in"/><arg type="a(ia{sv})" direction="out"/>
  </method>
  <method name="GetProperty">
    <arg type="i" direction="in"/><arg type="s" direction="in"/><arg type="v" direction="out"/>
  </method>
  <method name="Event">
    <arg type="i" direction="in"/><arg type="s" direction="in"/><arg type="v" direction="in"/>
    <arg type="u" direction="in"/>
  </method>
  <method name="EventGroup"><arg type="a(isvu)" direction="in"/><arg type="ai" direction="out"/></method>
  <method name="AboutToShow"><arg type="i" direction="in"/><arg type="b" direction="out"/></method>
</interface></node>
''').interfaces[0]


def _logo_pixmaps(dark):
    source = APP_ICON.read_bytes()
    if dark:
        source = source.replace(b'fill="#000"', b'fill="#fff"')
    pixmaps = []
    for size in (24, 48, 96):
        stream = Gio.MemoryInputStream.new_from_bytes(GLib.Bytes.new(source))
        pixbuf = GdkPixbuf.Pixbuf.new_from_stream_at_scale(stream, size, size, True, None)
        width, height, stride = pixbuf.get_width(), pixbuf.get_height(), pixbuf.get_rowstride()
        pixels = pixbuf.get_pixels()
        rgba = b''.join(pixels[row * stride:row * stride + width * 4] for row in range(height))
        argb = bytearray(len(rgba))
        argb[0::4], argb[1::4], argb[2::4], argb[3::4] = rgba[3::4], rgba[0::4], rgba[1::4], rgba[2::4]
        pixmaps.append((width, height, bytes(argb)))
    return GLib.Variant('a(iiay)', pixmaps)


class TrayIcon:
    def __init__(self, connection, open_window, hide_window, quit_app, style):
        self.connection = connection
        self.open_window = open_window
        self.style = style
        self.available = False
        self.host = None
        self.actions = {
            1: (f'Ouvrir {APP_NAME}', open_window),
            2: ('Réduire la fenêtre', hide_window),
            3: (f'Quitter {APP_NAME}', quit_app),
        }
        self.item_properties = {
            'Category': GLib.Variant('s', 'ApplicationStatus'), 'Id': GLib.Variant('s', 'b6ing'),
            'Title': GLib.Variant('s', APP_NAME), 'Status': GLib.Variant('s', 'Active'),
            'WindowId': GLib.Variant('i', 0), 'ItemIsMenu': GLib.Variant('b', False),
            'Menu': GLib.Variant('o', MENU_PATH), 'IconName': GLib.Variant('s', ''),
            'IconThemePath': GLib.Variant('s', ''), 'IconPixmap': _logo_pixmaps(style.get_dark()),
            'OverlayIconName': GLib.Variant('s', ''), 'OverlayIconPixmap': GLib.Variant('a(iiay)', []),
            'AttentionIconName': GLib.Variant('s', ''), 'AttentionIconPixmap': GLib.Variant('a(iiay)', []),
            'AttentionMovieName': GLib.Variant('s', ''),
        }
        self.menu_properties = {
            'Version': GLib.Variant('u', 3), 'TextDirection': GLib.Variant('s', 'ltr'),
            'Status': GLib.Variant('s', 'normal'), 'IconThemePath': GLib.Variant('as', []),
        }
        self.registrations = (
            connection.register_object(ITEM_PATH, ITEM, self.item_called, self.get_property, None),
            connection.register_object(MENU_PATH, MENU, self.menu_called, self.get_property, None),
        )
        self.watch = Gio.bus_watch_name_on_connection(connection, WATCHER, Gio.BusNameWatcherFlags.NONE,
                                                    self.host_appeared, self.host_vanished)
        self.style_handler = style.connect('notify::dark', self.update_icon)

    def update_icon(self, style, *_):
        self.item_properties['IconPixmap'] = _logo_pixmaps(style.get_dark())
        self.connection.emit_signal(None, ITEM_PATH, ITEM.name, 'NewIcon', None)

    def get_property(self, _connection, _sender, path, _interface, name):
        properties = self.item_properties if path == ITEM_PATH else self.menu_properties
        return properties.get(name)

    def host_appeared(self, connection, _name, owner):
        self.host = owner
        connection.call(owner, '/StatusNotifierWatcher', WATCHER, 'RegisterStatusNotifierItem',
                        GLib.Variant('(s)', (ITEM_PATH,)), None, Gio.DBusCallFlags.NONE, 5000, None,
                        self.registered, owner)

    def registered(self, connection, result, owner):
        try:
            connection.call_finish(result)
        except GLib.Error:
            return
        if self.host == owner:
            self.available = True

    def host_vanished(self, *_):
        self.host = None
        self.available = False

    def close(self):
        self.style.disconnect(self.style_handler)
        self.host_vanished()
        Gio.bus_unwatch_name(self.watch)
        for registration in self.registrations:
            self.connection.unregister_object(registration)

    def item_called(self, _connection, _sender, _path, _interface, method, _params, invocation):
        invocation.return_value(None)
        if method in ('Activate', 'SecondaryActivate'):
            GLib.idle_add(self.open_window)

    def entry_properties(self, identifier, names):
        if identifier == 0:
            values = {'children-display': GLib.Variant('s', 'submenu')}
        else:
            values = {
                'label': GLib.Variant('s', self.actions[identifier][0]),
                'enabled': GLib.Variant('b', True), 'visible': GLib.Variant('b', True),
            }
        return {key: value for key, value in values.items() if not names or key in names}

    def dispatch(self, identifier, event):
        if event == 'clicked' and identifier in self.actions:
            GLib.idle_add(self.actions[identifier][1])

    def menu_called(self, _connection, _sender, _path, _interface, method, params, invocation):
        values = params.unpack()
        ids = (0, *self.actions)
        if method == 'GetLayout':
            identifier, depth, names = values
            if identifier not in ids:
                invocation.return_dbus_error('com.canonical.dbusmenu.InvalidMenuItemId', 'Menu item introuvable')
                return
            children = [GLib.Variant('(ia{sv}av)', (item, self.entry_properties(item, names), []))
                        for item in self.actions] if identifier == 0 and depth else []
            result = GLib.Variant('(u(ia{sv}av))', (1, (identifier, self.entry_properties(identifier, names), children)))
        elif method == 'GetGroupProperties':
            requested, names = values
            result = GLib.Variant('(a(ia{sv}))', ([(item, self.entry_properties(item, names))
                                                  for item in requested or ids if item in ids],))
        elif method == 'GetProperty':
            identifier, name = values
            value = self.entry_properties(identifier, ()).get(name) if identifier in ids else None
            if value is None:
                invocation.return_dbus_error('com.canonical.dbusmenu.InvalidMenuItemId', 'Propriété introuvable')
                return
            result = GLib.Variant('(v)', (value,))
        elif method == 'AboutToShow':
            result = GLib.Variant('(b)', (False,))
        elif method == 'EventGroup':
            for identifier, event, _, _timestamp in values[0]:
                self.dispatch(identifier, event)
            result = GLib.Variant('(ai)', ([],))
        else:
            self.dispatch(values[0], values[1])
            result = None
        invocation.return_value(result)
