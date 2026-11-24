"""Mouse, touch, and keyboard input for a camera's motor."""

from time import monotonic

from gi.repository import Gdk, GLib, Gtk


class MotionInput:
    def __init__(self):
        self.held = (None, None, 0)

    def refresh(self, epoch, direction):
        self.held = (epoch, direction, monotonic() + 0.4)

    def read(self, epoch):
        current_epoch, direction, expires = self.held
        return direction if current_epoch == epoch and monotonic() < expires else None


class Joystick(Gtk.Grid):
    KEYS = {Gdk.KEY_Up: 'up', Gdk.KEY_Down: 'down', Gdk.KEY_Left: 'left', Gdk.KEY_Right: 'right',
            Gdk.KEY_KP_Up: 'up', Gdk.KEY_KP_Down: 'down', Gdk.KEY_KP_Left: 'left', Gdk.KEY_KP_Right: 'right'}

    def __init__(self, update, *, size=144):
        super().__init__(halign=Gtk.Align.CENTER, focusable=True, column_homogeneous=True, row_homogeneous=True)
        self.update = update
        self.direction = None
        self.timer = None
        self.start = None
        self.keys = {}
        self.arrows = {}
        self.set_size_request(size, size)
        self.deadzone = size / 10
        self.add_css_class('joystick')
        self.update_property([Gtk.AccessibleProperty.LABEL], ['Joystick de la caméra'])
        for direction, glyph, x, y in (('up', '↑', 1, 0), ('down', '↓', 1, 2),
                                        ('left', '←', 0, 1), ('right', '→', 2, 1)):
            arrow = Gtk.Label(label=glyph, can_target=False)
            arrow.add_css_class('joystick-arrow')
            self.arrows[direction] = arrow
            self.attach(arrow, x, y, 1, 1)
        center = Gtk.Label(label='●', can_target=False)
        center.add_css_class('joystick-center')
        self.attach(center, 1, 1, 1, 1)

        drag = Gtk.GestureDrag(button=Gdk.BUTTON_PRIMARY)
        drag.connect('drag-begin', self.drag_begin)
        drag.connect('drag-update', self.drag_update)
        drag.connect('drag-end', self.release)
        drag.connect('cancel', self.release)
        self.add_controller(drag)
        focus = Gtk.EventControllerFocus()
        focus.connect('leave', self.release)
        self.add_controller(focus)
        self.connect('unmap', self.release)

    def drag_begin(self, gesture, x, y):
        self.grab_focus()
        gesture.set_state(Gtk.EventSequenceState.CLAIMED)
        self.release_keyboard()
        self.start = (x, y)
        self.point(x, y)

    def drag_update(self, _gesture, dx, dy):
        if self.start is not None:
            self.point(self.start[0] + dx, self.start[1] + dy)

    def point(self, x, y):
        dx, dy = x - self.get_width() / 2, y - self.get_height() / 2
        if dx * dx + dy * dy < self.deadzone ** 2:
            self.set_direction(None)
        elif abs(dx) > abs(dy):
            self.set_direction('right' if dx > 0 else 'left')
        else:
            self.set_direction('down' if dy > 0 else 'up')

    def key_pressed(self, keyval):
        if self.start is not None:
            return False
        self.keys.setdefault(keyval, self.KEYS[keyval])
        self.set_direction(self.keys[next(reversed(self.keys))])
        return True

    def key_released(self, keyval):
        if self.keys.pop(keyval, None) is not None:
            self.set_direction(self.keys[next(reversed(self.keys))] if self.keys else None)

    def release_keyboard(self):
        if self.keys:
            self.keys.clear()
            self.set_direction(None)

    def release(self, *_):
        self.start = None
        self.keys.clear()
        self.set_direction(None)

    def set_direction(self, direction):
        if direction == self.direction:
            return
        if self.direction is not None:
            self.arrows[self.direction].remove_css_class('joystick-active')
        self.direction = direction
        self.update(direction)
        if direction is None:
            if self.timer is not None:
                GLib.source_remove(self.timer)
                self.timer = None
            return
        self.arrows[direction].add_css_class('joystick-active')
        if self.timer is None:
            self.timer = GLib.timeout_add(100, self.heartbeat)

    def heartbeat(self):
        self.update(self.direction)
        return GLib.SOURCE_CONTINUE


class CameraKeyboard:
    """Send arrow keys to the selected live camera while its window is active."""

    MODIFIERS = Gdk.ModifierType.CONTROL_MASK | Gdk.ModifierType.ALT_MASK | Gdk.ModifierType.SUPER_MASK

    def __init__(self, window, target):
        self.window = window
        self.target = target
        self.held = None
        controller = Gtk.EventControllerKey()
        controller.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        controller.connect('key-pressed', self.pressed)
        controller.connect('key-released', self.released)
        window.add_controller(controller)
        window.connect('notify::is-active', self.activity_changed)
        window.connect('notify::focus-widget', self.focus_changed)

    def editing(self):
        focus = self.window.get_focus()
        while focus is not None and focus is not self.window:
            if isinstance(focus, (Gtk.Editable, Gtk.TextView, Gtk.DropDown, Gtk.Popover, Gtk.Range)):
                return True
            focus = focus.get_parent()
        return False

    def pressed(self, _controller, keyval, _keycode, modifiers):
        if keyval not in Joystick.KEYS or modifiers & self.MODIFIERS or self.editing():
            return False
        target = self.target()
        if target is None or not target.is_sensitive() or not target.get_visible():
            return False
        if target is not self.held:
            self.release()
        if target.key_pressed(keyval):
            self.held = target
            return True
        return False

    def released(self, _controller, keyval, *_):
        if self.held is not None:
            self.held.key_released(keyval)
            if not self.held.keys:
                self.held = None

    def activity_changed(self, *_):
        if not self.window.is_active():
            self.release()

    def focus_changed(self, *_):
        if self.editing():
            self.release()

    def release(self):
        if self.held is not None:
            self.held.release_keyboard()
            self.held = None
