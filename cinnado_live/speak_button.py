"""Hold-to-speak control for the grid and the fullscreen viewer."""

from gi.repository import Gdk, Gtk


class SpeakButton(Gtk.Button):
    def __init__(self, start, stop):
        super().__init__(icon_name='audio-input-microphone-symbolic', tooltip_text='Maintiens pour parler',
                         css_classes=['control-icon'])
        self.update_property([Gtk.AccessibleProperty.LABEL], ['Maintiens pour parler'])
        self.start = start
        self.stop = stop
        self.held = False

        press = Gtk.GestureDrag(button=Gdk.BUTTON_PRIMARY)
        press.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        press.connect('drag-begin', self.begin_drag)
        press.connect('drag-end', self.release)
        press.connect('cancel', self.release)
        self.add_controller(press)
        keyboard = Gtk.EventControllerKey()
        keyboard.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        keyboard.connect('key-pressed', self.key_pressed)
        keyboard.connect('key-released', self.key_released)
        self.add_controller(keyboard)
        focus = Gtk.EventControllerFocus()
        focus.connect('leave', self.release)
        self.add_controller(focus)
        self.connect('unmap', self.release)

    def press(self):
        if self.is_sensitive() and not self.held:
            self.held = True
            self.start(self)

    def begin_drag(self, gesture, *_):
        if self.is_sensitive():
            self.grab_focus()
            gesture.set_state(Gtk.EventSequenceState.CLAIMED)
            self.press()

    def key_pressed(self, _controller, keyval, *_):
        if keyval in (Gdk.KEY_space, Gdk.KEY_Return):
            self.press()
            return True
        return False

    def key_released(self, _controller, keyval, *_):
        if keyval in (Gdk.KEY_space, Gdk.KEY_Return):
            self.release()

    def release(self, *_):
        if self.held:
            self.held = False
            self.stop(self)
