"""Display-only zoom and timing for the live viewer."""

from time import monotonic_ns

from gi.repository import Gdk, GObject, Graphene, Gtk

from .camera_grid import icon_button


class LiveClock:
    """Estimate extra lag using the fastest observed arrival in each session."""

    def __init__(self):
        self.sample = None

    def observe(self, epoch, source_ms):
        arrival = monotonic_ns() // 1_000_000
        stamp = source_ms if isinstance(source_ms, int) and 0 <= source_ms < 1 << 43 else arrival
        offset = arrival - stamp
        if self.sample is not None:
            previous_epoch, previous_stamp, best = self.sample
            if previous_epoch == epoch and stamp >= previous_stamp - 1000:
                offset = min(offset, best)
        self.sample = epoch, stamp, offset
        return stamp

    def delay(self, epoch, stamp):
        if self.sample is None or stamp is None or self.sample[0] != epoch:
            return None
        return max(0, (monotonic_ns() // 1_000_000 - self.sample[2] - stamp) / 1000)


class VideoZoom(GObject.Object, Gdk.Paintable):
    __gsignals__ = {'zoom-changed': (GObject.SignalFlags.RUN_FIRST, None, ())}

    def __init__(self):
        super().__init__()
        self.source = None
        self.handlers = ()
        self.factor = 1.0
        self.x = self.y = .5

    def set_source(self, source):
        if self.source is not None:
            for handler in self.handlers:
                self.source.disconnect(handler)
        self.source = source
        self.handlers = () if source is None else (
            source.connect('invalidate-contents', lambda *_: self.invalidate_contents()),
            source.connect('invalidate-size', lambda *_: self.invalidate_size()),
        )
        self.invalidate_size()
        self.invalidate_contents()

    def do_get_intrinsic_width(self):
        return self.source.get_intrinsic_width() if self.source is not None else 0

    def do_get_intrinsic_height(self):
        return self.source.get_intrinsic_height() if self.source is not None else 0

    def do_get_intrinsic_aspect_ratio(self):
        return self.source.get_intrinsic_aspect_ratio() if self.source is not None else 0

    def do_snapshot(self, snapshot, width, height):
        if self.source is None:
            return
        snapshot.save()
        snapshot.push_clip(Graphene.Rect().init(0, 0, width, height))
        snapshot.translate(Graphene.Point().init(width * (.5 - self.x * self.factor),
                                                  height * (.5 - self.y * self.factor)))
        self.source.snapshot(snapshot, width * self.factor, height * self.factor)
        snapshot.pop()
        snapshot.restore()

    def set_center(self, x, y):
        edge = .5 / self.factor
        self.x = min(1 - edge, max(edge, x))
        self.y = min(1 - edge, max(edge, y))
        self.invalidate_contents()

    def set_zoom(self, value):
        self.factor = max(1., min(4., value))
        self.set_center(self.x, self.y)
        self.emit('zoom-changed')

    def attach(self, picture):
        drag = Gtk.GestureDrag(button=Gdk.BUTTON_PRIMARY)
        # Offsets are reported from the start of the gesture.
        center = (.5, .5)

        def start(gesture, *_):
            nonlocal center
            if self.factor == 1 or self.source is None:
                gesture.set_state(Gtk.EventSequenceState.DENIED)
            else:
                center = self.x, self.y
                gesture.set_state(Gtk.EventSequenceState.CLAIMED)

        def pan(_gesture, dx, dy):
            ratio = self.do_get_intrinsic_aspect_ratio()
            if ratio <= 0 or picture.get_height() <= 0 or picture.get_width() <= 0:
                return
            width = min(picture.get_width(), picture.get_height() * ratio)
            height = width / ratio
            self.set_center(center[0] - dx / (width * self.factor), center[1] - dy / (height * self.factor))

        drag.connect('drag-begin', start)
        drag.connect('drag-update', pan)
        picture.add_controller(drag)


class ZoomButtons(Gtk.Box):
    def __init__(self, zoom):
        super().__init__(spacing=2, css_classes=['control-cluster'])
        self.zoom = zoom
        reduce = icon_button('zoom-out-symbolic', 'Réduire le zoom')
        increase = icon_button('zoom-in-symbolic', 'Agrandir, puis glisser dans l’image pour déplacer le cadrage')
        reset = Gtk.Button(tooltip_text='Rétablir le zoom à 1×', css_classes=['control-icon'])
        reset.update_property([Gtk.AccessibleProperty.LABEL], ['Rétablir le zoom à 1×'])
        for button in (reduce, reset, increase):
            self.append(button)
        reduce.connect('clicked', lambda *_: zoom.set_zoom(zoom.factor - .5))
        increase.connect('clicked', lambda *_: zoom.set_zoom(zoom.factor + .5))
        reset.connect('clicked', lambda *_: zoom.set_zoom(1))

        def refresh(*_):
            reset.set_label(f'{zoom.factor:g}×'.replace('.', ','))
            reduce.set_sensitive(zoom.factor > 1)
            reset.set_sensitive(zoom.factor > 1)
            increase.set_sensitive(zoom.factor < 4)

        self.handler = zoom.connect('zoom-changed', refresh)
        refresh()

    def close(self):
        self.zoom.disconnect(self.handler)
