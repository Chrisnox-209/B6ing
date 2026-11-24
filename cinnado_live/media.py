"""Snapshots and local video files made from the authenticated live view."""

from itertools import count
from pathlib import Path
from queue import Empty, Full, Queue
import os
import re
import tempfile

from gi.repository import GLib, Gst, Gtk


START_CODE = re.compile(rb'\x00\x00\x00?\x01')


def has_keyframe(data, codec):
    for start in START_CODE.finditer(data):
        if start.end() < len(data):
            header = data[start.end()]
            if codec == 'h264' and header & 31 == 5 or codec == 'hevc' and 16 <= (header >> 1) & 63 <= 23:
                return True
    return False


def snapshot(picture, *, paintable=None):
    if paintable is None:
        paintable = picture.get_paintable()
    if paintable is None:
        return None
    width = paintable.get_intrinsic_width()
    height = paintable.get_intrinsic_height()
    if width <= 0 or height <= 0:
        return None
    drawing = Gtk.Snapshot.new()
    paintable.snapshot(drawing, width, height)
    node = drawing.to_node()
    return picture.get_native().get_renderer().render_texture(node, None) if node is not None else None


def clip_thumbnail(frames, encoding):
    if not frames or encoding is None:
        return None
    codec, fps = encoding
    kind = {'h264': 'h264', 'hevc': 'h265'}[codec]
    pipeline = Gst.parse_launch(
        f'appsrc name=input format=time caps="video/x-{kind},stream-format=byte-stream,alignment=au,'
        f'framerate={max(1, min(60, fps))}/1" ! {kind}parse ! avdec_{kind} max-threads=1 '
        '! videoconvert ! videoscale ! video/x-raw,width=224,height=126,pixel-aspect-ratio=1/1 '
        '! jpegenc quality=82 ! appsink name=output sync=false max-buffers=1 drop=true'
    )
    try:
        if pipeline.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
            return None
        source = pipeline.get_by_name('input')
        for raw in frames:
            source.emit('push-buffer', Gst.Buffer.new_wrapped(raw))
        source.emit('end-of-stream')
        sample = pipeline.get_by_name('output').emit('try-pull-sample', 2 * Gst.SECOND)
        if sample is not None:
            buffer = sample.get_buffer()
            return buffer.extract_dup(0, buffer.get_size())
    finally:
        pipeline.set_state(Gst.State.NULL)
    return None


def publish_media_file(temporary: Path, base: Path) -> Path:
    for index in count(1):
        destination = base if index == 1 else base.with_stem(f'{base.stem}_{index}')
        try:
            reservation = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            continue
        os.close(reservation)
        try:
            temporary.replace(destination)
        except OSError:
            destination.unlink(missing_ok=True)
            raise
        return destination


def save_snapshot(texture, destination: Path) -> bool:
    descriptor, path = tempfile.mkstemp(prefix='.cinnado-', suffix='.png', dir=destination.parent)
    os.close(descriptor)
    temporary = Path(path)
    try:
        if not texture.save_to_png(str(temporary)):
            return False
        publish_media_file(temporary, destination)
        return True
    finally:
        temporary.unlink(missing_ok=True)


class AudioPlayer:
    """Play the AAC-LC mono frames delivered by the B6 live audio channel."""

    def __init__(self, first_audio, on_error, *, sink='autoaudiosink'):
        self.pipeline = Gst.parse_launch(
            'appsrc name=input is-live=true do-timestamp=true format=time max-buffers=12 leaky-type=downstream '
            'caps="audio/mpeg,mpegversion=4,stream-format=raw,framed=true,rate=8000,channels=1,'
            'codec_data=(buffer)1588" ! avdec_aac name=decoder ! audioconvert ! audioresample '
            f'! {sink} sync=false'
        )
        self.source = self.pipeline.get_by_name('input')

        def decoded(_pad, _info):
            GLib.idle_add(first_audio)
            return Gst.PadProbeReturn.REMOVE

        self.pipeline.get_by_name('decoder').get_static_pad('src').add_probe(Gst.PadProbeType.BUFFER, decoded)
        self.bus = self.pipeline.get_bus()
        self.bus.add_signal_watch()
        self.handler = self.bus.connect('message::error', lambda *_: on_error())
        if self.pipeline.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
            self.close()
            raise RuntimeError('Audio pipeline failed')

    def push(self, data):
        buffer = Gst.Buffer.new_wrapped(data)
        buffer.duration = Gst.SECOND * 1024 // 8000
        return self.source.emit('push-buffer', buffer) == Gst.FlowReturn.OK

    def close(self):
        self.bus.disconnect(self.handler)
        self.bus.remove_signal_watch()
        self.pipeline.set_state(Gst.State.NULL)


class TalkRequest:
    def __init__(self, ready):
        self.ready = ready
        self.frames = Queue(maxsize=3)

    def offer(self, data):
        try:
            self.frames.put_nowait(data)
        except Full:
            pass

    def next_frame(self):
        try:
            return self.frames.get_nowait()
        except Empty:
            return None


class MicrophoneCapture:
    """Encode the default desktop microphone only while the speak button is held."""

    def __init__(self, format, on_frame, on_error, *, source='autoaudiosrc'):
        caps = f'audio/x-raw,rate={format.rate},channels=1'
        if format.codec == 0x102:
            encoding = f'{caps},format=S16LE ! audiobuffersplit output-buffer-duration=2/25 ! alawenc'
        else:
            encoding = f'{caps} ! avenc_aac bitrate=24000 ! audio/mpeg,stream-format=raw'
        self.pipeline = Gst.parse_launch(
            f'{source} ! audioconvert ! audioresample ! {encoding} ! '
            'appsink name=voice emit-signals=true sync=false max-buffers=3 drop=true'
        )
        self.sink = self.pipeline.get_by_name('voice')

        def sample_ready(sink):
            sample = sink.emit('pull-sample')
            if sample is not None:
                buffer = sample.get_buffer()
                if 0 < buffer.get_size() <= 1280:
                    on_frame(buffer.extract_dup(0, buffer.get_size()))
            return Gst.FlowReturn.OK

        self.sample_handler = self.sink.connect('new-sample', sample_ready)
        self.bus = self.pipeline.get_bus()
        self.bus.add_signal_watch()
        self.error_handler = self.bus.connect('message::error', lambda *_: on_error())
        if self.pipeline.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
            self.close()
            raise RuntimeError('Microphone could not start')

    def close(self):
        self.bus.disconnect(self.error_handler)
        self.bus.remove_signal_watch()
        self.sink.disconnect(self.sample_handler)
        self.pipeline.set_state(Gst.State.NULL)


class Recording:
    def __init__(self, destination, codec, fps):
        self.destination = Path(destination)
        self.codec = codec
        self.started = False
        self.error = False
        self.frames = 0
        self.fps = max(1, min(60, int(fps)))
        self.pipeline = None
        descriptor, path = tempfile.mkstemp(prefix='.cinnado-', suffix='.mkv', dir=self.destination.parent)
        os.close(descriptor)
        self.temporary = Path(path)
        try:
            kind = {'h264': 'h264', 'hevc': 'h265'}[codec]
            self.pipeline = Gst.parse_launch(
                'appsrc name=input is-live=true format=time '
                f'caps="video/x-{kind},stream-format=byte-stream,alignment=au,framerate={self.fps}/1" '
                f'! {kind}parse config-interval=-1 ! matroskamux ! filesink name=output sync=false'
            )
            self.source = self.pipeline.get_by_name('input')
            self.pipeline.get_by_name('output').set_property('location', str(self.temporary))
            if self.pipeline.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
                raise RuntimeError('Recording pipeline failed')
        except (GLib.Error, RuntimeError, KeyError):
            if self.pipeline is not None:
                self.pipeline.set_state(Gst.State.NULL)
            self.temporary.unlink(missing_ok=True)
            raise

    def push(self, data):
        if not self.started:
            self.started = has_keyframe(data, self.codec)
            if not self.started:
                return True
        if self.source.get_property('current-level-buffers') >= max(48, 3 * self.fps):
            return False
        buffer = Gst.Buffer.new_wrapped(data)
        start = self.frames * Gst.SECOND // self.fps
        self.frames += 1
        buffer.pts = start
        buffer.duration = self.frames * Gst.SECOND // self.fps - start
        result = self.source.emit('push-buffer', buffer)
        self.error = result != Gst.FlowReturn.OK or bool(self.pipeline.get_bus().pop_filtered(Gst.MessageType.ERROR))
        return not self.error

    def finish(self):
        complete = False
        try:
            if self.started and not self.error and self.source.emit('end-of-stream') == Gst.FlowReturn.OK:
                result = self.pipeline.get_bus().timed_pop_filtered(Gst.SECOND, Gst.MessageType.EOS | Gst.MessageType.ERROR)
                complete = result is not None and result.type == Gst.MessageType.EOS
        finally:
            self.pipeline.set_state(Gst.State.NULL)
        try:
            if complete and self.temporary.stat().st_size:
                self.destination = publish_media_file(self.temporary, self.destination)
                return True
            return False
        finally:
            self.temporary.unlink(missing_ok=True)
