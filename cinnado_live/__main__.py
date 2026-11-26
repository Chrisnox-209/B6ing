"""GTK viewer for authenticated Cinnado B6 live streams."""

from __future__ import annotations

import asyncio
from concurrent.futures import CancelledError
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from itertools import islice
from pathlib import Path
import os
from queue import Empty, Full, Queue
import sys
from tempfile import TemporaryDirectory
import threading
from time import monotonic

import gi
gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
gi.require_version('Gst', '1.0')
gi.require_version('Graphene', '1.0')
from gi.repository import Adw, Gdk, Gio, GLib, GObject, Gst, Gtk, Pango

from .alerts import AlertEntry, AlertJournal
from .alerts_view import AlertsView
from .camera_controls import CameraConfiguration, CameraControls, FavoriteSettings, FavoriteShortcuts
from .camera_grid import CameraGrid, icon_button
from .desktop import (
    APP_ICON_NAME, APP_ID, APP_LAUNCHER_ICON, APP_NAME, ICON_SEARCH_PATH, autostart_enabled, set_autostart,
)
from .engine import Account, CameraError
from .joystick import CameraKeyboard, Joystick, MotionInput
from . import __version__, keyring
from .live_view import LiveClock, VideoZoom, ZoomButtons
from .media import AudioPlayer, MicrophoneCapture, Recording, TalkRequest, has_keyframe, publish_media_file, save_snapshot, snapshot
from .preferences import Preferences
from .sd_view import SdView
from .settings_dialog import SettingsDialog
from .speak_button import SpeakButton
from .tray import TrayIcon

STATE = Path(os.environ.get('XDG_STATE_HOME') or Path.home() / '.local/state') / 'cinnado-live'
PREFERENCES_PATH = Path(os.environ.get('XDG_CONFIG_HOME') or Path.home() / '.config') / 'cinnado-live/settings.json'
STATUS_REFRESH_SECONDS = 120
MOTION_NOTIFICATION_ID = 'motion-alerts'
ALERT_PRELOAD_ATTEMPTS = 5
SIDEBAR_WIDTH = 264
COLOR_SCHEMES = {
    'system': Adw.ColorScheme.DEFAULT,
    'light': Adw.ColorScheme.FORCE_LIGHT,
    'dark': Adw.ColorScheme.FORCE_DARK,
}


def note(event: str) -> None:
    """Only record fixed test milestones. Never record account data or frames."""
    try:
        STATE.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(STATE / 'status.log', os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, 'a', encoding='utf-8') as output:
            output.write(event + '\n')
    except OSError:
        pass


class Worker:
    def __init__(self):
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self.loop.run_forever, daemon=True)
        self.thread.start()
        self.account = Account()

    def submit(self, coroutine):
        return asyncio.run_coroutine_threadsafe(coroutine, self.loop)

    def close(self):
        async def shutdown():
            pending = [task for task in asyncio.all_tasks() if task is not asyncio.current_task()]
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            await self.account.close()
        try:
            self.submit(shutdown()).result(2)
        finally:
            self.loop.call_soon_threadsafe(self.loop.stop)
            self.thread.join(2)
            if not self.thread.is_alive():
                self.loop.close()


class Preview:
    """Decode Annex B access units to a GTK paintable."""

    def __init__(self, picture, codec, fps, first_frame, on_error, on_resolution=None, *, zoom=None):
        kind, parser, decoder = {
            'h264': ('h264', 'h264parse', 'avdec_h264'),
            'hevc': ('h265', 'h265parse', 'avdec_h265'),
        }[codec]
        fps = max(1, min(60, int(fps)))
        self.fps = fps
        self.pipeline = Gst.parse_launch(
            f'appsrc name=media is-live=true format=time do-timestamp=true max-buffers=20 leaky-type=downstream '
            f'caps="video/x-{kind},stream-format=byte-stream,alignment=au,framerate={fps}/1" '
            f'! {parser} ! {decoder} max-threads=2 ! videoconvert ! gtk4paintablesink name=view sync=false'
        )
        self.source = self.pipeline.get_by_name('media')
        self.picture = picture
        self.codec = codec
        self.on_error = on_error
        self.zoom = zoom
        self.displayed_ms = None
        sink = self.pipeline.get_by_name('view')
        self.raw_paintable = sink.get_property('paintable')
        if zoom is not None:
            zoom.set_source(self.raw_paintable)
        picture.set_paintable(zoom if zoom is not None else self.raw_paintable)
        first = True

        def decoded(_pad, _info):
            nonlocal first
            pts = _info.get_buffer().pts
            if pts != Gst.CLOCK_TIME_NONE:
                self.displayed_ms = pts // Gst.MSECOND
            if first:
                first = False
                GLib.idle_add(first_frame)
                if zoom is None:
                    return Gst.PadProbeReturn.REMOVE
            return Gst.PadProbeReturn.OK

        pad = sink.get_static_pad('sink')
        pad.add_probe(Gst.PadProbeType.BUFFER, decoded)
        if on_resolution is not None:
            def dimensions(_pad, info):
                event = info.get_event()
                if event.type == Gst.EventType.CAPS:
                    caps = event.parse_caps().get_structure(0)
                    GLib.idle_add(on_resolution, caps.get_value('width'), caps.get_value('height'))
                return Gst.PadProbeReturn.OK
            pad.add_probe(Gst.PadProbeType.EVENT_DOWNSTREAM, dimensions)
        self.bus = self.pipeline.get_bus()
        self.bus.add_signal_watch()
        self.handler = self.bus.connect('message::error', lambda *_: self.on_error())
        if self.pipeline.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
            self.close()
            raise CameraError('Le lecteur vidéo ne peut pas démarrer sur ce PC.')

    def push(self, data, stamp=None):
        buffer = Gst.Buffer.new_wrapped(data)
        if stamp is not None:
            buffer.pts = stamp * Gst.MSECOND
        return self.source.emit('push-buffer', buffer)

    def close(self):
        self.bus.disconnect(self.handler)
        self.bus.remove_signal_watch()
        self.pipeline.set_state(Gst.State.NULL)
        if self.zoom is not None:
            self.zoom.set_source(None)
        self.picture.set_paintable(None)


class ReadingBadge(Gtk.Box):
    def __init__(self):
        super().__init__(spacing=5)
        self.add_css_class('reading-badge')
        self.icon = Gtk.Image()
        self.label = Gtk.Label(css_classes=['numeric'])
        self.append(self.icon)
        self.append(self.label)

    def show_reading(self, icon, text, description, reported_at=None, *, warning=False):
        self.icon.set_from_icon_name(icon)
        if reported_at is not None:
            if datetime.now(timezone.utc) - reported_at >= timedelta(days=1):
                text += ' · ancien'
            description += f'\nDernier relevé le {reported_at.astimezone():%d/%m/%Y à %H:%M}'
        self.label.set_text(text)
        if warning:
            self.add_css_class('weak-reading')
        else:
            self.remove_css_class('weak-reading')
        self.set_tooltip_text(description)
        self.update_property([Gtk.AccessibleProperty.LABEL], [description])


class CameraIndicators(Gtk.Box):
    def __init__(self, margin):
        super().__init__(spacing=6, halign=Gtk.Align.START, valign=Gtk.Align.START,
                         margin_start=margin, margin_top=margin)
        self.battery = ReadingBadge()
        self.wifi = ReadingBadge()
        self.wifi.add_css_class('wifi-reading')
        self.append(self.battery)
        self.append(self.wifi)
        self.update(None, None)

    def update(self, battery, wifi):
        if battery is None:
            self.battery.show_reading('battery-missing-symbolic', '—', 'Batterie · aucun relevé reçu')
        else:
            level = ((battery.percent + 5) // 10) * 10
            value = f'{battery.percent} %'
            self.battery.show_reading(f'battery-level-{level}-symbolic', value, f'Batterie · {value}',
                                      battery.reported_at, warning=battery.percent <= 20)

        if wifi is None:
            self.wifi.show_reading('network-wireless-signal-none-symbolic', '—', 'Wi-Fi · aucun relevé reçu')
            return

        if wifi.percent is None:
            value = f'{wifi.dbm} dBm'
            bars = sum(wifi.dbm >= threshold for threshold in (-95, -75, -65, -55))
        else:
            value = f'{wifi.percent} %'
            bars = sum(wifi.percent >= threshold for threshold in (1, 40, 60, 80))
        description = f'Wi-Fi · {value}'
        if wifi.percent is not None and wifi.dbm is not None:
            description += f'\nPuissance mesurée · {wifi.dbm} dBm'
            if wifi.dbm_at is not None and (wifi.percent_at is None or
                                          abs((wifi.dbm_at - wifi.percent_at).total_seconds()) >= 60):
                description += f' le {wifi.dbm_at.astimezone():%d/%m/%Y à %H:%M}'
        level = ('none', 'weak', 'ok', 'good', 'excellent')[bars]
        self.wifi.show_reading(f'network-wireless-signal-{level}-symbolic', value, description,
                               wifi.reported_at, warning=bars <= 1)


def latest_reading(previous, incoming):
    if incoming is None:
        return previous
    if previous is None or previous.reported_at is None:
        return incoming
    if incoming.reported_at is None or incoming.reported_at < previous.reported_at:
        return previous
    return incoming


class CameraTile(Gtk.Box):
    live_state = GObject.Property(type=str, default='idle')

    def __init__(self, camera, worker, index):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, hexpand=True, vexpand=True,
                         overflow=Gtk.Overflow.HIDDEN, css_classes=['camera-card'])
        self.camera, self.worker = camera, worker
        self.index = index
        self.preview = None
        self.preview_epoch = None
        self.preview_token = None
        self.task = None
        self.frames = None
        self.motion = None
        self.clock = self.reconnect = None
        self.catchup_until = None
        self.delay_checked = 0
        self.fullscreen_favorites = None
        self.fullscreen_favorite_settings = None
        self.zoom = VideoZoom()
        self.motorized = False
        self.recording = None
        self.fullscreen_window = None
        self.fullscreen_joystick = None
        self.fullscreen_toasts = None
        self.fullscreen_controls = None
        self.fullscreen_record_button = None
        self.fullscreen_indicators = None
        self.battery_reading = None
        self.wifi_reading = None
        self.camera_status_task = None
        self.camera_options = None
        self.controls_task = None
        self.quality_code = None
        self.audio_enabled = False
        self.audio_frames = None
        self.audio_player = None
        self.talk_request = None
        self.talk_owner = None
        self.microphone = None
        self.timer = None
        self.generation = None
        selection = Gtk.GestureClick(button=Gdk.BUTTON_PRIMARY)
        selection.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        selection.connect('released', self.select_from_tile)
        self.add_controller(selection)

        header = Gtk.Box(spacing=6, css_classes=['camera-heading'])
        self.name_label = Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.END)
        self.name_label.add_css_class('camera-name')
        self.select_button = Gtk.Button(child=self.name_label, hexpand=True, has_frame=False,
                                        css_classes=['camera-select-button'])
        self.select_button.connect('clicked', self.select_from_heading)
        header.append(self.select_button)
        self.indicators = CameraIndicators(margin=0)
        self.indicators.set_valign(Gtk.Align.CENTER)
        header.append(self.indicators)
        self.append(header)
        self.layout_controls = Gtk.Box(spacing=5, visible=False, css_classes=['camera-layout-controls'])
        self.append(self.layout_controls)

        frame = self.video_area = Gtk.Overlay(hexpand=True, vexpand=True)
        frame.add_css_class('camera-stage')
        self.picture = Gtk.Picture(can_shrink=True, content_fit=Gtk.ContentFit.CONTAIN)
        self.zoom.attach(self.picture)
        frame.set_child(self.picture)
        self.placeholder = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, halign=Gtk.Align.CENTER,
                                   valign=Gtk.Align.CENTER, margin_start=10, margin_end=10,
                                   can_target=False, css_classes=['camera-placeholder'])
        self.placeholder.append(Gtk.Image(icon_name=APP_ICON_NAME, pixel_size=37))
        self.status = Gtk.Label(label='Prête · diffusion à la demande', wrap=True, max_width_chars=38,
                                justify=Gtk.Justification.CENTER)
        self.placeholder.append(self.status)
        frame.add_overlay(self.placeholder)
        self.joystick = self.overlay_joystick(size=96, margin=12)
        self.joystick.add_css_class('compact')
        self.joystick.set_margin_bottom(52)
        self.joystick.set_visible(False)
        self.joystick.set_sensitive(False)
        frame.add_overlay(self.joystick)
        self.append(frame)

        media = Gtk.Box(spacing=4, halign=Gtk.Align.START, valign=Gtk.Align.END, margin_start=9,
                        margin_bottom=9, css_classes=['camera-media'])
        frame.add_overlay(media)
        self.button = icon_button('media-playback-start-symbolic', 'Voir le direct')
        self.button.connect('clicked', self.toggle)
        media.append(self.button)
        self.camera_actions = Gtk.Box(spacing=4, sensitive=False)
        media.append(self.camera_actions)
        self.audio_button = self.sound_button()
        self.audio_button.connect('toggled', self.change_audio)
        self.camera_actions.append(self.audio_button)
        self.talk_button = SpeakButton(self.start_talking, self.stop_talking)
        self.camera_actions.append(self.talk_button)
        self.add_capture_buttons(self.camera_actions)
        fullscreen = icon_button('view-fullscreen-symbolic', 'Plein écran')
        fullscreen.connect('clicked', self.fullscreen_player)
        self.camera_actions.append(fullscreen)

        self.quality_choice = self.quality_selector()
        self.quality_choice.connect('notify::selected', self.change_quality)
        self.quality_choice.set_sensitive(False)
        self.resolution = Gtk.Label(xalign=0, css_classes=['dim-label', 'numeric'])
        self.delay_label = Gtk.Label(label='Retard —', css_classes=['dim-label', 'numeric'])
        self.delay_label.set_tooltip_text('Retard supplémentaire estimé depuis l’ouverture du flux')
        self.live_tools, _, self.favorites = self.make_live_tools(self.delay_label)
        self.live_tools.set_sensitive(False)
        self.controls = CameraControls(self.change_camera_option, labeled=True)
        self.favorite_settings = self.make_favorite_settings()
        quality = Gtk.Box(spacing=8)
        quality.append(Gtk.Label(label='Qualité du direct', xalign=0, hexpand=True, wrap=True))
        quality.append(self.quality_choice)
        direct = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        for widget in (quality, self.resolution, self.live_tools):
            direct.append(widget)
        positions = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self.favorites.set_halign(Gtk.Align.START)
        positions.append(self.favorites)
        positions.append(self.favorite_settings)
        self.positions_status = Gtk.Label(label='Positions indisponibles.', xalign=0, wrap=True,
                                          css_classes=['dim-label'])
        positions.append(self.positions_status)
        self.configuration = CameraConfiguration(self.toggle_camera_power, self.change_motion_sensitivity,
                                                 direct, self.controls, positions)
        self.set_display_name(camera.name)
        self.refresh_controls()

    def set_display_name(self, name):
        self.name_label.set_text(name)
        label = f'Afficher les réglages de {name}'
        self.select_button.set_tooltip_text(label)
        self.select_button.update_property([Gtk.AccessibleProperty.LABEL], [label])
        self.configuration.set_camera_name(name)
        if self.fullscreen_window is not None:
            self.fullscreen_window.set_title(name)

    def make_live_tools(self, delay, *, fullscreen=False):
        zoom = ZoomButtons(self.zoom)
        favorites = FavoriteShortcuts(lambda slot: self.change_favorite('turn', slot))
        jump = icon_button('media-seek-forward-symbolic', 'Revenir au direct')
        if fullscreen:
            jump.set_child(Adw.ButtonContent(icon_name='media-seek-forward-symbolic', label='Revenir au direct'))
        jump.connect('clicked', self.return_to_live)
        recovery = Gtk.Box(spacing=6)
        recovery.append(jump)
        recovery.append(delay)
        if fullscreen:
            tools = Gtk.Box(spacing=6)
            for group in (zoom, favorites, recovery):
                tools.append(group)
        else:
            tools = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
            zoom.set_halign(Gtk.Align.START)
            tools.append(zoom)
            tools.append(recovery)
        return tools, zoom, favorites

    @staticmethod
    def camera_settings_disclosure(content, label):
        panel = Gtk.Revealer(child=content, transition_type=Gtk.RevealerTransitionType.SLIDE_DOWN,
                             transition_duration=160)
        button = Gtk.ToggleButton(icon_name='emblem-system-symbolic', tooltip_text=label,
                                  css_classes=['control-icon'])
        button.update_property([Gtk.AccessibleProperty.LABEL], [label])
        button.update_state([Gtk.AccessibleState.EXPANDED], [0])
        button.bind_property('active', panel, 'reveal-child', GObject.BindingFlags.SYNC_CREATE)
        button.connect('toggled', lambda gear: gear.update_state([Gtk.AccessibleState.EXPANDED], [int(gear.get_active())]))
        return button, panel

    def make_favorite_settings(self):
        return FavoriteSettings(lambda: self.change_favorite('save'), self.confirm_remove_favorite)

    def confirm_remove_favorite(self, slot):
        dialog = Adw.AlertDialog(heading=f'Supprimer la position {slot} ?',
                                 body='La position sera aussi supprimée de Wansview Cloud.')
        dialog.add_response('cancel', 'Annuler')
        dialog.add_response('remove', 'Supprimer')
        dialog.set_response_appearance('remove', Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_close_response('cancel')
        dialog.connect('response::remove', lambda *_: self.change_favorite('remove', slot))
        dialog.present(self.fullscreen_window or self)

    def change_favorite(self, action, slot=None):
        if self.controls_task is not None:
            return
        if action == 'save' and not self.live_tools.is_sensitive():
            self.notify('Ouvre le direct pour mémoriser l’orientation actuelle.')
            return
        if action in ('save', 'turn'):
            self.release_joysticks()
            if isinstance(root := self.get_root(), Window):
                root.keyboard.release()
        message = {
            'save': 'Position mémorisée.',
            'turn': f'Rappel de la position {slot} demandé.',
            'remove': f'Position {slot} supprimée.',
        }[action]
        self.run_camera_control(self.worker.account.change_favorite(self.camera, action, slot), message=message)

    def return_to_live(self, *_):
        if self.preview is None or self.catchup_until is not None:
            return
        self.catchup_until = monotonic() + 5
        self.status.set_label('Rattrapage du direct…')
        self.close_audio()

    def refresh_delay(self, now):
        if now < self.delay_checked:
            return
        self.delay_checked = now + .25
        value = self.clock.delay(self.preview_epoch, self.preview.displayed_ms) if self.preview is not None else None
        if value is None:
            text = 'Retard —'
        elif value < .1:
            text = 'Retard < 0,1 s'
        else:
            text = f'Retard ≈ {value:.1f} s'.replace('.', ',')
        self.delay_label.set_label(text)

    def add_capture_buttons(self, actions, *, fullscreen=False):
        label = 'Enregistrer une image PNG'
        photo = Gtk.Button(icon_name='camera-photo-symbolic', tooltip_text=label, css_classes=['control-icon'])
        photo.update_property([Gtk.AccessibleProperty.LABEL], [label])
        photo.connect('clicked', self.take_photo)
        actions.append(photo)

        record = Gtk.Button(css_classes=['control-icon', 'record-control'])
        record.connect('clicked', self.toggle_recording)
        actions.append(record)
        if fullscreen:
            self.fullscreen_record_button = record
        else:
            self.record_button = record
        self.update_record_buttons()

    def update_record_buttons(self):
        active = self.recording is not None
        icon = 'media-playback-stop-symbolic' if active else 'media-record-symbolic'
        label = 'Arrêter l’enregistrement' if active else 'Enregistrer la vidéo sur le PC'
        for button in (self.record_button, self.fullscreen_record_button):
            if button is None:
                continue
            button.set_icon_name(icon)
            button.set_tooltip_text(label)
            button.update_property([Gtk.AccessibleProperty.LABEL], [label])
            if active:
                button.add_css_class('destructive-action')
            else:
                button.remove_css_class('destructive-action')

    def update_camera_indicators(self, options):
        if options is not None:
            self.battery_reading = latest_reading(self.battery_reading, options.battery)
            self.wifi_reading = latest_reading(self.wifi_reading, options.wifi)
        self.indicators.update(self.battery_reading, self.wifi_reading)
        if self.fullscreen_indicators is not None:
            self.fullscreen_indicators.update(self.battery_reading, self.wifi_reading)

    def refresh_camera_status(self):
        if self.camera_status_task is not None or self.controls_task is not None:
            return
        self.camera_status_task = self.worker.submit(self.worker.account.read_camera_options(self.camera))
        self.camera_status_task.add_done_callback(lambda future: GLib.idle_add(self.finish_camera_status, future))

    def finish_camera_status(self, future):
        if future is not self.camera_status_task:
            return False
        self.camera_status_task = None
        try:
            options = future.result()
        except (CameraError, CancelledError):
            options = None
        if options is not None and self.controls_task is None:
            self.apply_camera_options(options)
            self.refresh_controls()
        self.update_camera_indicators(options)
        return False

    def overlay_joystick(self, *, size, margin):
        joystick = Joystick(self.set_direction, size=size)
        joystick.set_halign(Gtk.Align.END)
        joystick.set_valign(Gtk.Align.END)
        joystick.set_margin_end(margin)
        joystick.set_margin_bottom(margin)
        joystick.add_css_class('video-control')
        joystick.set_tooltip_text('Maintiens une direction pour orienter la caméra')
        return joystick

    def select_from_tile(self, _gesture, _count, x, y):
        clicked = self.pick(x, y, Gtk.PickFlags.DEFAULT)
        while clicked is not None and clicked is not self:
            if isinstance(clicked, Gtk.Button) or clicked is self.layout_controls:
                return
            clicked = clicked.get_parent()
        self.select_from_heading()

    def select_from_heading(self, *_):
        if isinstance(root := self.get_root(), Window):
            root.select_camera(self)
        if self.joystick.is_sensitive():
            self.joystick.grab_focus()

    def set_selected(self, selected):
        if selected:
            self.add_css_class('selected-camera')
        else:
            self.release_joysticks()
            self.remove_css_class('selected-camera')
        self.sync_joystick()

    def sync_joystick(self):
        self.joystick.set_visible(self.has_css_class('selected-camera') and self.joystick.get_sensitive())

    @staticmethod
    def sound_button():
        sound = Gtk.ToggleButton(icon_name='audio-volume-muted-symbolic', tooltip_text='Écouter le son de la caméra',
                                 css_classes=['control-icon'])
        sound.update_property([Gtk.AccessibleProperty.LABEL], ['Son du direct'])
        return sound

    def change_audio(self, button):
        self.audio_enabled = button.get_active()
        button.set_icon_name('audio-volume-high-symbolic' if self.audio_enabled else 'audio-volume-muted-symbolic')
        button.set_tooltip_text('Couper le son du direct' if self.audio_enabled else 'Écouter le son de la caméra')
        if not self.audio_enabled:
            self.close_audio()

    def close_audio(self):
        if self.audio_player is not None:
            self.audio_player.close()
            self.audio_player = None
        if self.audio_frames is not None:
            while True:
                try:
                    self.audio_frames.get_nowait()
                except Empty:
                    break

    def audio_failure(self):
        self.audio_button.set_active(False)
        self.notify('Le son de la caméra ne peut pas être lu sur ce PC.')

    def start_talking(self, button):
        if not self.camera_actions.is_sensitive() or self.talk_request is not None:
            return
        if isinstance(root := self.get_root(), Window):
            for tile in root.tiles:
                if tile is not self:
                    tile.stop_talking()
        request = TalkRequest(lambda format: GLib.idle_add(self.talk_ready, request, format))
        self.talk_owner = button
        self.talk_request = request
        self.close_audio()
        button.add_css_class('suggested-action')
        button.set_tooltip_text('Connexion au haut-parleur…')

    def talk_ready(self, request, format):
        if self.talk_request is not request:
            return False
        if format is None:
            self.stop_talking()
            self.notify('La caméra n’a pas ouvert le canal de parole.')
            return False
        try:
            self.microphone = MicrophoneCapture(format, request.offer, lambda: self.microphone_failed(request))
        except (GLib.Error, RuntimeError):
            self.microphone_failed(request)
            return False
        self.talk_owner.remove_css_class('suggested-action')
        self.talk_owner.add_css_class('destructive-action')
        self.talk_owner.set_tooltip_text('Micro actif · relâche pour couper')
        note(f'talk_started camera={self.index}')
        return False

    def microphone_failed(self, request):
        if self.talk_request is request:
            self.stop_talking()
            self.notify('Ubuntu n’a pas pu ouvrir le microphone.')

    def stop_talking(self, button=None):
        owner = self.talk_owner
        if owner is None or (button is not None and owner is not button):
            return
        self.talk_request = self.talk_owner = None
        if self.microphone is not None:
            self.microphone.close()
            self.microphone = None
        owner.held = False
        owner.remove_css_class('suggested-action')
        owner.remove_css_class('destructive-action')
        owner.set_tooltip_text('Maintiens pour parler')
        note(f'talk_stopped camera={self.index}')

    def drain_audio(self):
        if not self.audio_enabled or self.audio_frames is None or self.talk_request is not None:
            return
        for _ in range(6):
            try:
                data, epoch = self.audio_frames.get_nowait()
            except Empty:
                break
            if self.preview_epoch != epoch:
                continue
            if self.audio_player is None:
                def heard():
                    if self.audio_enabled and self.preview_epoch == epoch:
                        note(f'live_audio_decoded camera={self.index}')
                    return False

                try:
                    self.audio_player = AudioPlayer(heard, self.audio_failure)
                except (GLib.Error, RuntimeError):
                    self.audio_failure()
                    return
            if not self.audio_player.push(data):
                self.audio_failure()
                return

    def notify(self, message):
        if self.fullscreen_toasts is not None:
            self.fullscreen_toasts.add_toast(Adw.Toast.new(message))
        elif root := self.get_root():
            root.toast(message)

    def refresh_controls(self):
        busy = self.controls_task is not None
        power = self.camera_options.hibernating if self.camera_options is not None else None
        sleeping = power is True
        self.configuration.refresh(self.camera_options, busy)
        self.button.set_sensitive(not sleeping and (not busy or self.task is not None))
        for controls in (self.controls, self.fullscreen_controls):
            if controls is not None:
                controls.refresh(self.camera_options, busy or sleeping)
        slots = self.camera_options.favorite_slots if self.camera_options is not None else None
        self.positions_status.set_visible(slots is None)
        for shortcuts in (self.favorites, self.fullscreen_favorites):
            if shortcuts is not None:
                shortcuts.refresh(slots, busy)
        for settings in (self.favorite_settings, self.fullscreen_favorite_settings):
            if settings is not None:
                settings.refresh(slots, live=self.live_tools.is_sensitive(), busy=busy)
        if self.task is not None:
            state = 'playing' if self.camera_actions.get_sensitive() else 'connecting'
        elif sleeping:
            state = 'sleeping'
        else:
            state = 'busy' if busy else 'idle'
        if self.live_state != state:
            self.live_state = state

    def load_camera_options(self):
        self.run_camera_control(self.worker.account.read_camera_options(self.camera))

    def apply_camera_options(self, options):
        previous = self.camera_options.hibernating if self.camera_options is not None else None
        if options.hibernating is None and previous is not None:
            options = replace(options, hibernating=previous)
        self.camera_options = options
        if options.hibernating:
            if self.task is not None:
                self.reset('En veille')
            else:
                self.status.set_label('En veille')
        elif previous:
            self.status.set_label('Prête · diffusion à la demande')

    def toggle_camera_power(self, *_):
        if self.controls_task is not None or self.camera_options is None:
            return
        sleeping = self.camera_options.hibernating
        if sleeping is not None:
            self.run_camera_control(
                self.worker.account.set_hibernation(self.camera, not sleeping),
                message='Veille désactivée.' if sleeping else 'Veille activée.',
            )

    def change_motion_sensitivity(self, value):
        if self.controls_task is None:
            self.run_camera_control(self.worker.account.set_motion_sensitivity(self.camera, value),
                                    message='Sensibilité enregistrée sur la caméra.')

    def change_camera_option(self, option, value):
        if self.controls_task is not None:
            return

        async def request(options):
            account = self.worker.account
            if options is None:
                options = await account.read_camera_options(self.camera)
            capability = 'night_vision' if option == 'vision' else option
            if not getattr(options, capability, False):
                raise CameraError('Cette caméra ne propose pas cette commande.')
            await account.set_camera_option(self.camera, option, value)
            return replace(options, vision_mode=value) if option == 'vision' else options

        message = 'Mode de vision enregistré.' if option == 'vision' else 'Commande acceptée par AJCloud.'
        self.run_camera_control(request(self.camera_options), message=message)

    def run_camera_control(self, request, *, message=None):
        if self.camera_status_task is not None:
            self.camera_status_task.cancel()
            self.camera_status_task = None
        self.controls_task = self.worker.submit(request)
        self.refresh_controls()
        self.controls_task.add_done_callback(
            lambda future: GLib.idle_add(self.finish_camera_controls, future, message))

    def finish_camera_controls(self, future, message):
        if future is not self.controls_task:
            return False
        self.controls_task = None
        had_options = self.camera_options is not None
        try:
            options = future.result()
        except CancelledError:
            pass
        except CameraError as exc:
            if message is not None:
                self.notify(str(exc))
        except Exception:
            if message is not None:
                self.notify('La caméra n’a pas confirmé la commande.')
        else:
            self.apply_camera_options(options)
            if not had_options or message is None:
                self.update_camera_indicators(self.camera_options)
            if message is not None:
                self.notify(message)
        self.refresh_controls()
        return False

    @staticmethod
    def quality_selector():
        choice = Gtk.DropDown.new_from_strings(['Haute', 'Éco'])
        choice.add_css_class('quality-control')
        choice.set_tooltip_text('Qualité du direct')
        choice.update_property([Gtk.AccessibleProperty.LABEL], ['Qualité du direct'])
        return choice

    def change_quality(self, choice, _spec):
        selected = choice.get_selected()
        if selected in (0, 1):
            self.stop_recording()
            self.quality_code = (5, 1)[selected]

    def show_resolution(self, generation, epoch, width, height):
        if self.generation is generation and self.preview_epoch == epoch and width and height:
            text = f'{width} × {height}'
            if self.resolution.get_text() != text:
                self.resolution.set_text(text)
                note(f'live_resolution camera={self.index} {width}x{height}')
        return False

    def toggle(self, *_):
        if self.task is not None:
            self.reset('Direct arrêté.')
            return
        if not self.button.get_sensitive():
            return
        generation = self.generation = object()
        queue = self.frames = Queue(maxsize=48)
        audio_frames = self.audio_frames = Queue(maxsize=16)
        motion = self.motion = MotionInput()
        clock = self.clock = LiveClock()
        reconnect = self.reconnect = threading.Event()
        self.set_playing(True)
        self.status.set_label('Ouverture du direct…')
        note(f'stream_requested camera={self.index}')

        async def receive(data, codec, fps, epoch, source_ms=None):
            stamp = clock.observe(epoch, source_ms)
            try:
                queue.put_nowait((data, codec, fps, epoch, stamp))
            except Full:
                pass

        async def receive_audio(data, epoch):
            if self.talk_request is not None:
                return
            try:
                audio_frames.put_nowait((data, epoch))
            except Full:
                pass

        def stage(message):
            GLib.idle_add(self.set_status, generation, message)

        def show_controls(available):
            def apply():
                if self.generation is generation:
                    self.motorized = available
                return False
            GLib.idle_add(apply)

        async def request():
            await self.worker.account.play(self.camera, asyncio.Event(), receive, stage,
                                           direction=motion.read, ptz_available=show_controls,
                                           quality=lambda: self.quality_code,
                                           listening=lambda: self.audio_enabled, audio=receive_audio,
                                           requested_talk=lambda: self.talk_request,
                                           reconnect=reconnect)

        self.task = self.worker.submit(request())
        self.task.add_done_callback(lambda future: GLib.idle_add(self.finish, generation, future))
        self.timer = GLib.timeout_add(25, self.drain, generation)
        self.refresh_controls()

    def set_status(self, generation, message):
        if self.generation is generation:
            self.status.set_label(message)
            self.placeholder.set_visible(True)
            self.release_joysticks()
            self.stop_talking()
            self.close_audio()
            self.joystick.set_sensitive(False)
            self.sync_joystick()
            self.camera_actions.set_sensitive(False)
            self.live_tools.set_sensitive(False)
            self.quality_choice.set_sensitive(False)
            self.refresh_controls()
            self.delay_label.set_label('Retard —')
            self.resolution.set_text('')
            if self.recording is not None:
                self.stop_recording()
        return False

    def set_direction(self, direction):
        if self.motion is not None:
            self.motion.refresh(self.preview_epoch, direction if self.joystick.get_sensitive() else None)

    def release_joysticks(self):
        self.joystick.release()
        if self.fullscreen_joystick is not None:
            self.fullscreen_joystick.release()

    def set_playing(self, playing):
        icon, label = ('media-playback-stop-symbolic', 'Arrêter le direct') if playing else (
            'media-playback-start-symbolic', 'Voir le direct',
        )
        self.button.set_icon_name(icon)
        self.button.set_tooltip_text(label)
        self.button.update_property([Gtk.AccessibleProperty.LABEL], [label])

    def fullscreen_player(self, *_):
        if self.fullscreen_window is not None:
            self.fullscreen_window.present()
            return
        root = self.get_root()
        window = Gtk.Window(application=root.get_application(), transient_for=root, title=self.name_label.get_text())
        self.fullscreen_window = window
        self.joystick.release()
        self.stop_talking()
        layout = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        box = Gtk.Overlay(hexpand=True, vexpand=True)
        box.add_css_class('video-area')
        layout.append(box)
        picture = Gtk.Picture(can_shrink=True, content_fit=Gtk.ContentFit.CONTAIN)
        self.zoom.attach(picture)
        bindings = [self.picture.bind_property('paintable', picture, 'paintable', GObject.BindingFlags.SYNC_CREATE)]
        box.set_child(picture)
        self.fullscreen_indicators = CameraIndicators(margin=16)
        self.fullscreen_indicators.update(self.battery_reading, self.wifi_reading)
        box.add_overlay(self.fullscreen_indicators)

        joystick = self.fullscreen_joystick = self.overlay_joystick(size=144, margin=24)
        keyboard = CameraKeyboard(window, lambda: joystick)
        for property_name in ('visible', 'sensitive'):
            bindings.append(self.joystick.bind_property('sensitive', joystick, property_name,
                                                        GObject.BindingFlags.SYNC_CREATE))
        box.add_overlay(joystick)

        toolbar = Gtk.Box(spacing=12)
        toolbar.add_css_class('fullscreen-toolbar')
        layout.append(toolbar)
        options = Gtk.Box(spacing=5, css_classes=['media-strip'])
        sound = self.sound_button()
        options.append(sound)
        speak = SpeakButton(self.start_talking, self.stop_talking)
        options.append(speak)
        self.add_capture_buttons(options, fullscreen=True)
        window.connect('notify::is-active',
                       lambda fullscreen, _prop: speak.release() if not fullscreen.is_active() else None)
        dimensions = Gtk.Label()
        dimensions.add_css_class('numeric')
        dimensions.add_css_class('dim-label')
        quality = self.quality_selector()
        options.append(quality)
        options.append(dimensions)
        toolbar.append(options)
        toolbar.append(Gtk.Separator(orientation=Gtk.Orientation.VERTICAL))
        delay = Gtk.Label(css_classes=['dim-label', 'numeric'])
        live_tools, zoom_buttons, self.fullscreen_favorites = self.make_live_tools(delay, fullscreen=True)
        toolbar.append(live_tools)
        self.fullscreen_favorite_settings = self.make_favorite_settings()
        self.fullscreen_favorite_settings.add_css_class('fullscreen-position-settings')
        favorite_gear, favorites_panel = self.camera_settings_disclosure(
            self.fullscreen_favorite_settings, 'Gérer les positions')
        toolbar.append(favorite_gear)
        layout.append(favorites_panel)
        bindings.append(self.fullscreen_favorite_settings.bind_property(
            'visible', favorite_gear, 'visible', GObject.BindingFlags.SYNC_CREATE))
        toolbar.append(Gtk.Separator(orientation=Gtk.Orientation.VERTICAL))
        self.fullscreen_controls = CameraControls(self.change_camera_option)
        toolbar.append(self.fullscreen_controls)
        self.refresh_controls()
        bindings.extend((
            self.audio_button.bind_property('active', sound, 'active',
                                            GObject.BindingFlags.SYNC_CREATE | GObject.BindingFlags.BIDIRECTIONAL),
            self.audio_button.bind_property('icon-name', sound, 'icon-name', GObject.BindingFlags.SYNC_CREATE),
            self.audio_button.bind_property('tooltip-text', sound, 'tooltip-text', GObject.BindingFlags.SYNC_CREATE),
            self.resolution.bind_property('label', dimensions, 'label', GObject.BindingFlags.SYNC_CREATE),
            self.quality_choice.bind_property('selected', quality, 'selected',
                                              GObject.BindingFlags.SYNC_CREATE | GObject.BindingFlags.BIDIRECTIONAL),
            self.camera_actions.bind_property('sensitive', options, 'sensitive', GObject.BindingFlags.SYNC_CREATE),
            self.live_tools.bind_property('sensitive', live_tools, 'sensitive', GObject.BindingFlags.SYNC_CREATE),
            self.delay_label.bind_property('label', delay, 'label', GObject.BindingFlags.SYNC_CREATE),
        ))
        toolbar.append(Gtk.Box(hexpand=True))
        close = Gtk.Button(icon_name='view-restore-symbolic', tooltip_text='Quitter le plein écran',
                           css_classes=['control-icon'])
        close.update_property([Gtk.AccessibleProperty.LABEL], ['Quitter le plein écran'])
        close.connect('clicked', lambda *_: window.close())
        toolbar.append(close)
        key = Gtk.EventControllerKey()

        def escape(_controller, keyval, *_):
            if keyval == Gdk.KEY_Escape:
                window.close()
                return True
            return False

        def closed(*_):
            keyboard.release()
            joystick.release()
            self.stop_talking(speak)
            zoom_buttons.close()
            for binding in bindings:
                binding.unbind()
            self.fullscreen_joystick = None
            self.fullscreen_toasts = None
            self.fullscreen_controls = None
            self.fullscreen_favorites = None
            self.fullscreen_favorite_settings = None
            self.fullscreen_record_button = None
            self.fullscreen_indicators = None
            self.fullscreen_window = None
            return False

        key.connect('key-pressed', escape)
        window.add_controller(key)
        self.fullscreen_toasts = Adw.ToastOverlay(child=layout)
        window.set_child(self.fullscreen_toasts)
        window.connect('close-request', closed)
        window.fullscreen()
        window.present()
        if joystick.get_visible() and joystick.get_sensitive():
            joystick.grab_focus()

    def take_photo(self, *_):
        root = self.get_root()
        if not isinstance(root, Window):
            return
        captured = datetime.now()
        try:
            texture = snapshot(self.picture, paintable=self.preview.raw_paintable) if self.preview is not None else None
        except (GLib.Error, TypeError):
            texture = None
        if texture is None:
            self.notify('Aucune image disponible pour la capture.')
            return

        try:
            path = root.preferences.photo_path(root.default_photo_folder, self.name_label.get_text(),
                                               self.index, captured)
            saved = save_snapshot(texture, path)
        except OSError:
            self.notify('Dossier des photos inaccessible. Vérifie le dossier dans les réglages.')
            return
        self.notify('Photo enregistrée.' if saved else 'Impossible d’enregistrer la photo.')

    def toggle_recording(self, *_):
        if self.recording is not None:
            self.stop_recording()
            return
        root = self.get_root()
        if not isinstance(root, Window) or self.preview is None:
            return
        try:
            path = root.preferences.recording_path(root.default_recording_folder, self.name_label.get_text(),
                                                   self.index, datetime.now())
            self.recording = Recording(path, self.preview.codec, self.preview.fps)
        except OSError:
            self.notify('Dossier d’enregistrement inaccessible. Vérifie le dossier dans les réglages.')
            return
        except (GLib.Error, RuntimeError):
            self.notify('Impossible de démarrer l’enregistrement.')
            return
        self.update_record_buttons()
        self.notify('Enregistrement du direct sur le PC.')

    def stop_recording(self):
        recording, self.recording = self.recording, None
        if recording is None:
            return
        try:
            saved = recording.finish()
        except OSError:
            saved = False
        self.update_record_buttons()
        self.notify('Vidéo enregistrée.' if saved else 'Aucune vidéo enregistrée.')

    def drain(self, generation):
        if self.generation is not generation or not self.frames:
            return False
        now = monotonic()
        if self.catchup_until is not None and now >= self.catchup_until:
            self.catchup_until = None
            if self.recording is None:
                self.reconnect.set()
            else:
                self.status.set_label('En direct')
                self.notify('Le rattrapage demanderait une reconnexion du flux en cours d’enregistrement.')
        for _ in range(12):
            try:
                data, codec, fps, epoch, stamp = self.frames.get_nowait()
            except Empty:
                break
            if self.preview and (self.preview_epoch != epoch or self.preview.codec != codec):
                self.stop_recording()
                self.preview.close()
                self.preview = None
            if self.recording is not None and not self.recording.push(data):
                self.stop_recording()
            if self.catchup_until is not None:
                delay = self.clock.delay(epoch, stamp)
                if delay is None or delay > .8 or not has_keyframe(data, codec):
                    continue
                self.catchup_until = None
                if self.preview is not None:
                    self.preview.close()
                    self.preview = None
            if self.preview is None:
                try:
                    self.preview_epoch = epoch
                    token = self.preview_token = object()
                    self.preview = Preview(self.picture, codec, fps,
                                           lambda epoch=epoch, token=token:
                                           self.decoded(generation, epoch) if token is self.preview_token else False,
                                           lambda epoch=epoch, token=token:
                                           self.decoder_error(generation, epoch) if token is self.preview_token else False,
                                           lambda width, height, epoch=epoch, token=token:
                                           self.show_resolution(generation, epoch, width, height)
                                           if token is self.preview_token else False, zoom=self.zoom)
                except (CameraError, GLib.Error, KeyError):
                    self.timer = None
                    self.reset('Le flux a été reçu, mais le lecteur vidéo ne peut pas le décoder.')
                    note(f'decoder_error camera={self.index}')
                    return False
            self.preview.push(data, stamp)
        self.drain_audio()
        self.refresh_delay(now)
        return True

    def decoded(self, generation, epoch):
        if self.generation is generation and self.preview_epoch == epoch:
            self.status.set_label('En direct')
            self.placeholder.set_visible(False)
            self.joystick.set_sensitive(self.motorized)
            self.sync_joystick()
            if self.motorized and isinstance(root := self.get_root(), Window) and root.selected_camera is None:
                root.select_camera(self)
            self.camera_actions.set_sensitive(True)
            self.live_tools.set_sensitive(True)
            self.quality_choice.set_sensitive(True)
            self.refresh_controls()
            note(f'live_frame_decoded camera={self.index}')
        return False

    def decoder_error(self, generation, epoch):
        if self.generation is generation and self.preview_epoch == epoch:
            self.reset('Le flux a été reçu, mais le lecteur vidéo ne peut pas le décoder.')
            note(f'decoder_error camera={self.index}')

    def finish(self, generation, future):
        if self.generation is not generation:
            return False
        try:
            future.result()
        except CancelledError:
            self.reset('Direct arrêté.')
        except CameraError as exc:
            self.reset(str(exc))
            note(f'stream_connection_failed camera={self.index} · {exc}')
        except Exception:
            self.reset('Erreur inattendue lors de la lecture.')
            note(f'unexpected_stream_error camera={self.index}')
        else:
            self.reset('Session terminée. Relance le direct pour continuer.')
            note(f'stream_finished camera={self.index}')
        return False

    def reset(self, message):
        self.generation = None
        self.release_joysticks()
        self.stop_talking()
        self.audio_button.set_active(False)
        self.close_audio()
        self.audio_frames = None
        self.stop_recording()
        if self.fullscreen_window is not None:
            self.fullscreen_window.close()
        if self.task:
            self.task.cancel()
            self.task = None
        if self.timer is not None:
            GLib.source_remove(self.timer)
            self.timer = None
        if self.preview:
            self.preview.close()
            self.preview = None
        self.preview_epoch = None
        self.preview_token = None
        self.frames = None
        self.motion = None
        self.clock = self.reconnect = None
        self.catchup_until = None
        self.zoom.set_zoom(1)
        self.motorized = False
        self.joystick.set_visible(False)
        self.joystick.set_sensitive(False)
        self.camera_actions.set_sensitive(False)
        self.live_tools.set_sensitive(False)
        self.quality_choice.set_sensitive(False)
        self.status.set_label(message)
        self.refresh_controls()
        self.delay_label.set_label('Retard —')
        self.resolution.set_text('')
        self.placeholder.set_visible(True)
        self.set_playing(False)


class Window(Adw.ApplicationWindow):
    def __init__(self, application, worker):
        super().__init__(application=application, title=APP_NAME, default_width=1080, default_height=740)
        self.add_css_class('b6-window')
        self.initial_monitor_handler = None
        self.initial_monitor_known = False
        self.size_for_monitor(self.get_display().get_monitors().get_item(0))
        self.connect('realize', self.watch_initial_monitor)
        self.worker = worker
        self.tiles = []
        self.camera_status_timer = None
        self.motion_task = None
        self.motion_generation = None
        self.alert_journal = None
        self.alert_preview_tasks = set()
        self.alert_downloads = {}
        self.alert_preload_pending = False
        self.selected_camera = None
        self.keyboard = CameraKeyboard(self, self.keyboard_joystick)
        self.closed = False
        self.login_task = None
        self.saved_account = False
        self.preferences = Preferences.load(PREFERENCES_PATH)
        self.apply_theme()
        videos = GLib.get_user_special_dir(GLib.UserDirectory.DIRECTORY_VIDEOS)
        self.default_recording_folder = (Path(videos) if videos else Path.home() / 'Videos') / 'Cinnado Live'
        pictures = GLib.get_user_special_dir(GLib.UserDirectory.DIRECTORY_PICTURES)
        self.default_photo_folder = (Path(pictures) if pictures else Path.home() / 'Pictures') / 'Cinnado Live'
        self.connect('close-request', self.on_close)
        self.connect('notify::is-active', self.release_window_microphone)

        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar(css_classes=['b6-header'])
        brand = Gtk.Box(spacing=9, valign=Gtk.Align.CENTER, width_request=SIDEBAR_WIDTH - 12,
                        css_classes=['b6-brand'])
        logo = Gtk.Image.new_from_icon_name(APP_ICON_NAME)
        logo.add_css_class('brand-icon')
        logo.set_pixel_size(30)
        brand.append(logo)
        brand.append(Gtk.Label(label=APP_NAME, css_classes=['b6-wordmark']))
        header.pack_start(brand)
        self.page_title = Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.END, visible=False,
                                    css_classes=['b6-page-title'])
        header.pack_start(self.page_title)
        header.set_title_widget(Gtk.Box())
        toolbar.add_top_bar(header)
        self.logout = Gtk.Button(label='Déconnexion', visible=False)
        self.logout.connect('clicked', self.sign_out)
        header.pack_end(self.logout)
        self.settings = Gtk.Button(icon_name='emblem-system-symbolic', tooltip_text='Réglages',
                                   css_classes=['flat', 'b6-header-settings'])
        self.settings.update_property([Gtk.AccessibleProperty.LABEL], ['Réglages'])
        self.settings.connect('clicked', self.open_settings)
        header.pack_end(self.settings)
        self.stack = Gtk.Stack(css_classes=['b6-main'])
        toolbar.set_content(self.stack)
        self.toasts = Adw.ToastOverlay(child=toolbar)
        self.set_content(self.toasts)

        login = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14,
                        halign=Gtk.Align.CENTER, valign=Gtk.Align.CENTER)
        login.set_size_request(410, -1)
        login.add_css_class('view-card')
        title = Gtk.Label(label='Voir mes caméras', xalign=0)
        title.add_css_class('title-1')
        login.append(title)
        info = Gtk.Label(label='Connecte-toi avec le compte Wansview Cloud de tes caméras.', xalign=0, wrap=True)
        login.append(info)
        form = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        form.add_css_class('boxed-list')
        self.email = Adw.EntryRow(title='Adresse e-mail')
        self.email.set_input_purpose(Gtk.InputPurpose.EMAIL)
        self.password = Adw.PasswordEntryRow(title='Mot de passe')
        self.email.connect('entry-activated', lambda *_: self.password.grab_focus())
        self.password.connect('entry-activated', self.sign_in)
        form.append(self.email)
        form.append(self.password)
        login.append(form)
        self.remember = Gtk.CheckButton(label='Mémoriser le compte sur ce PC')
        login.append(self.remember)
        self.connect_button = Gtk.Button(label='Se connecter')
        self.connect_button.add_css_class('suggested-action')
        self.connect_button.connect('clicked', self.sign_in)
        login.append(self.connect_button)
        self.forget = Gtk.Button(label='Oublier le compte mémorisé', visible=False)
        self.forget.connect('clicked', self.forget_saved_account)
        login.append(self.forget)
        self.info = Gtk.Label(label='La mémorisation utilise le trousseau Ubuntu.', xalign=0, wrap=True)
        self.info.add_css_class('dim-label')
        login.append(self.info)
        self.stack.add_named(login, 'login')

        account_view = Gtk.Box()
        sidebar = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=5, width_request=SIDEBAR_WIDTH, hexpand=False,
                          css_classes=['b6-sidebar'])
        account_view.append(sidebar)
        self.cameras_button = Gtk.ToggleButton(css_classes=['flat', 'b6-nav-button'])
        self.cameras_button.set_child(Adw.ButtonContent(icon_name='camera-video-symbolic', label='Caméras',
                                                        halign=Gtk.Align.START))
        self.cameras_button.connect('clicked', self.show_cameras)
        sidebar.append(self.cameras_button)
        self.alerts_content = Adw.ButtonContent(icon_name='preferences-system-notifications-symbolic', label='Alertes',
                                                halign=Gtk.Align.START)
        self.alerts_button = Gtk.ToggleButton(child=self.alerts_content, css_classes=['flat', 'b6-nav-button'])
        self.alerts_button.set_group(self.cameras_button)
        self.alerts_button.connect('clicked', self.open_alerts)
        sidebar.append(self.alerts_button)
        self.sd_button = Gtk.ToggleButton(css_classes=['flat', 'b6-nav-button'])
        self.sd_button.set_child(Adw.ButtonContent(icon_name='media-flash-symbolic', label='MicroSD',
                                                  halign=Gtk.Align.START))
        self.sd_button.set_group(self.cameras_button)
        self.sd_button.connect('clicked', self.open_sd)
        sidebar.append(self.sd_button)
        sidebar.append(Gtk.Separator(css_classes=['b6-sidebar-divider']))
        sidebar.append(Gtk.Box(vexpand=True))
        self.panels = Gtk.Stack(hhomogeneous=False, vhomogeneous=False)
        self.panel_scroll = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER,
                                               hexpand=False, propagate_natural_height=True, max_content_height=340,
                                               child=self.panels, visible=False)
        sidebar.append(self.panel_scroll)

        self.account_pages = Gtk.Stack(hexpand=True, vexpand=True, hhomogeneous=False, vhomogeneous=False)
        account_view.append(self.account_pages)
        self.camera_grid = CameraGrid(self.preferences, self.save_camera_layouts, self.select_camera)
        self.account_pages.add_named(self.camera_grid, 'cameras')
        self.alerts_view = AlertsView(self.remove_alert, self.clear_alerts, self.open_alert_clip,
                                      self.download_alert_clip, self.request_alert_previews)
        self.account_pages.add_named(self.alerts_view, 'alerts')
        self.sd_view = SdView(self.worker, Preview, self.tile_for_camera)
        self.account_pages.add_named(self.sd_view, 'sd')
        self.cameras_button.set_active(True)
        self.stack.add_named(account_view, 'account')
        self.stack.set_visible_child_name('login')
        note('app_opened')
        lookup = self.worker.submit(asyncio.to_thread(keyring.load_account))
        lookup.add_done_callback(lambda future: GLib.idle_add(self.restore_account, future))

    def toast(self, message):
        self.toasts.add_toast(Adw.Toast.new(message))

    def apply_theme(self):
        Adw.StyleManager.get_default().set_color_scheme(COLOR_SCHEMES[self.preferences.theme])

    def update_view_preferences(self, **changes):
        updated = replace(self.preferences, **changes)
        try:
            updated.save(PREFERENCES_PATH)
        except (OSError, UnicodeError):
            self.toast('Impossible de mémoriser les réglages de la vue.')
            return False
        self.preferences = updated
        return True

    def save_camera_layouts(self, layout, views):
        return self.update_view_preferences(camera_grid=layout, camera_views=views)

    def open_settings(self, *_):
        cameras = [tile.camera for tile in self.tiles]
        dialog = SettingsDialog(self.preferences, self.default_recording_folder, self.default_photo_folder,
                                cameras, self.save_preferences)
        dialog.present(self)

    def save_preferences(self, preferences, *, autostart=None):
        preferences = replace(preferences, camera_grid=self.preferences.camera_grid,
                              camera_views=self.preferences.camera_views,
                              camera_positions=self.preferences.camera_positions)
        previous = autostart_enabled()
        startup_changed = autostart is not None and autostart != previous
        if startup_changed:
            set_autostart(autostart)
        try:
            preferences.save(PREFERENCES_PATH)
        except (OSError, UnicodeError):
            if startup_changed:
                set_autostart(previous)
            raise
        self.preferences = preferences
        self.apply_theme()
        for tile in self.tiles:
            tile.set_display_name(preferences.camera_names.get(tile.camera.identifier, tile.camera.name))
        self.refresh_sd_cameras()
        self.update_motion_notifications()
        self.refresh_alerts()

    def open_alerts(self, *_):
        if self.alert_journal is None:
            return
        self.sd_view.set_active(False)
        if self.account_pages.get_visible_child_name() != 'alerts' or not self.alerts_view.clip.get_visible():
            self.alert_preload_pending = True
        self.keyboard.release()
        for tile in self.tiles:
            tile.release_joysticks()
            tile.stop_talking()
        self.account_pages.set_visible_child_name('alerts')
        self.page_title.set_label('Alertes de mouvement')
        self.panel_scroll.set_visible(False)
        self.alerts_button.set_active(True)
        self.alerts_view.set_active(True)
        self.dismiss_alert_notification()

    def show_cameras(self, *_):
        self.alert_preload_pending = False
        self.alerts_view.set_active(False)
        self.sd_view.set_active(False)
        self.account_pages.set_visible_child_name('cameras')
        self.page_title.set_label('Direct')
        self.panel_scroll.set_visible(self.selected_camera is not None)
        self.cameras_button.set_active(True)

    def refresh_sd_cameras(self):
        self.sd_view.set_cameras((tile.camera, tile.name_label.get_text()) for tile in self.tiles)

    def tile_for_camera(self, camera):
        return next((tile for tile in self.tiles if tile.camera is camera), None)

    def open_sd(self, *_):
        if not self.tiles:
            return
        self.alert_preload_pending = False
        self.alerts_view.set_active(False)
        self.keyboard.release()
        for tile in self.tiles:
            tile.release_joysticks()
            tile.stop_talking()
        self.account_pages.set_visible_child_name('sd')
        self.page_title.set_label('Carte microSD')
        self.panel_scroll.set_visible(False)
        self.sd_button.set_active(True)
        self.sd_view.set_active(True)

    def open_alert_clip(self, entry, name, *, autoplay=True, on_unavailable=None):
        self.alert_preload_pending = False
        if self.account_pages.get_visible_child_name() != 'alerts':
            return False
        camera = next((tile.camera for tile in self.tiles if tile.camera.identifier == entry.camera_id), None)
        if camera is None:
            return False
        try:
            self.alerts_view.clip.play(self.worker, camera, entry, name, autoplay=autoplay,
                                       on_unavailable=on_unavailable)
        except OSError:
            self.toast('Impossible de préparer le fichier temporaire du clip.')
            return False
        return True

    def preload_alert_clip(self, candidates):
        for entry, name in candidates:
            if self.open_alert_clip(entry, name, autoplay=False,
                                    on_unavailable=lambda: self.preload_alert_clip(candidates)):
                return True
        return False

    def download_alert_clip(self, entry, name):
        if entry.key in self.alert_downloads:
            return
        tile = next((item for item in self.tiles if item.camera.identifier == entry.camera_id), None)
        if tile is None:
            return
        preferences = self.preferences
        default_folder = self.default_recording_folder
        camera, index = tile.camera, tile.index

        async def save_clip():
            folder = preferences.recording_folder or default_folder
            if preferences.recording_folder is None:
                folder.mkdir(parents=True, exist_ok=True)
            with TemporaryDirectory(dir=folder, prefix='.b6ing-alert-') as directory:
                path = Path(directory) / 'clip'
                extension = await self.worker.account.download_alert_clip(camera, entry.identifier,
                                                                          entry.occurred_at, path)
                destination = preferences.alert_clip_path(default_folder, name, index,
                                                         entry.occurred_at.astimezone(), extension)
                return publish_media_file(path, destination)

        task = self.worker.submit(save_clip())
        self.alert_downloads[entry.key] = task
        self.alerts_view.set_downloading(entry.key, True)
        task.add_done_callback(lambda future: GLib.idle_add(self.alert_download_finished, entry.key, future))

    def alert_download_finished(self, key, future):
        if self.alert_downloads.get(key) is not future:
            return GLib.SOURCE_REMOVE
        del self.alert_downloads[key]
        self.alerts_view.set_downloading(key, False)
        try:
            destination = future.result()
        except CancelledError:
            pass
        except CameraError as exc:
            self.toast(str(exc))
        except OSError:
            self.toast('Impossible d’enregistrer le clip dans le dossier des vidéos.')
        except Exception:
            self.toast('Le téléchargement du clip a échoué.')
        else:
            self.toast(f'Clip enregistré · {destination.name}')
        return GLib.SOURCE_REMOVE

    def request_alert_previews(self, entries):
        cameras = {tile.camera.identifier: tile.camera for tile in self.tiles}
        grouped = {}
        for entry in entries:
            if entry.camera_id in cameras:
                grouped.setdefault(entry.camera_id, {})[entry.identifier] = entry.occurred_at

        async def load():
            account = self.worker.account
            results = await asyncio.gather(*(account.read_alert_previews(cameras[camera_id], items)
                                             for camera_id, items in grouped.items()), return_exceptions=True)
            return {(camera_id, alert_id): image
                    for camera_id, result in zip(grouped, results) if isinstance(result, dict)
                    for alert_id, image in result.items()}

        keys = tuple(entry.key for entry in entries)
        task = self.worker.submit(load())
        self.alert_preview_tasks.add(task)
        task.add_done_callback(lambda future: GLib.idle_add(self.alert_previews_finished, keys, future))

    def alert_previews_finished(self, keys, future):
        if future in self.alert_preview_tasks:
            self.alert_preview_tasks.remove(future)
            try:
                images = future.result()
            except Exception:
                images = {}
            self.alerts_view.show_previews(keys, images)
        return GLib.SOURCE_REMOVE

    def refresh_alerts(self):
        journal = self.alert_journal
        count = journal.unread_count
        label = f'Alertes ({count})' if count else 'Alertes'
        self.alerts_content.set_label(label)
        self.alerts_button.update_property([Gtk.AccessibleProperty.LABEL], [label])
        names = {tile.camera.identifier: tile.name_label.get_text() for tile in self.tiles}
        self.alerts_view.show_entries(journal.entries, names, self.preferences.motion_notifications)
        if self.alert_preload_pending:
            candidates = ((entry, names[entry.camera_id]) for entry in journal.entries if entry.camera_id in names)
            self.preload_alert_clip(islice(candidates, ALERT_PRELOAD_ATTEMPTS))

    def edit_alert_journal(self, edit):
        try:
            edit()
        except (OSError, UnicodeError):
            self.toast('Impossible de sauvegarder le journal d’alertes.')
        self.refresh_alerts()

    def dismiss_alert_notification(self):
        self.get_application().withdraw_notification(MOTION_NOTIFICATION_ID)
        if self.alert_journal is not None:
            self.edit_alert_journal(self.alert_journal.mark_read)

    def remove_alert(self, key):
        if self.alert_journal is not None:
            if self.alerts_view.clip.key == key:
                self.alerts_view.clip.close()
            self.edit_alert_journal(lambda: self.alert_journal.remove(key))
            if self.alert_journal.unread_count == 0:
                self.get_application().withdraw_notification(MOTION_NOTIFICATION_ID)

    def clear_alerts(self):
        if self.alert_journal is not None:
            self.alert_preload_pending = False
            self.alerts_view.clip.close()
            camera_ids = tuple(tile.camera.identifier for tile in self.tiles)
            self.edit_alert_journal(lambda: self.alert_journal.clear(camera_ids))
            if not self.alert_journal.entries:
                self.get_application().withdraw_notification(MOTION_NOTIFICATION_ID)

    def close_alert_journal(self):
        self.alert_preload_pending = False
        for task in (*self.alert_preview_tasks, *self.alert_downloads.values()):
            task.cancel()
        self.alert_preview_tasks.clear()
        self.alert_downloads.clear()
        self.alerts_view.reset()
        self.alert_journal = None
        self.get_application().withdraw_notification(MOTION_NOTIFICATION_ID)

    def update_motion_notifications(self):
        if not self.preferences.motion_notifications:
            self.stop_motion_notifications()
            self.get_application().withdraw_notification(MOTION_NOTIFICATION_ID)
            return
        if self.motion_task is not None or not self.tiles:
            return
        generation = self.motion_generation = object()
        cameras = tuple(tile.camera for tile in self.tiles)
        imported_cameras = self.alert_journal.imported_cameras

        def notify(camera, alert):
            GLib.idle_add(self.show_motion_notification, generation, camera, alert)

        def import_history(camera, alerts):
            GLib.idle_add(self.import_motion_history, generation, camera, alerts)

        async def watch():
            await asyncio.gather(
                *(self.worker.account.watch_motion(
                    camera, notify, import_history=None if camera.identifier in imported_cameras else import_history,
                ) for camera in cameras),
                return_exceptions=True,
            )

        self.motion_task = self.worker.submit(watch())

    def stop_motion_notifications(self):
        self.motion_generation = None
        if self.motion_task is not None:
            self.motion_task.cancel()
            self.motion_task = None

    def import_motion_history(self, generation, camera, alerts):
        if self.motion_generation is generation and self.alert_journal is not None:
            name = self.preferences.camera_names.get(camera.identifier, camera.name)
            entries = (AlertEntry(camera.identifier, alert.identifier, name, alert.occurred_at) for alert in alerts)
            self.edit_alert_journal(lambda: self.alert_journal.import_history(camera.identifier, entries))
        return GLib.SOURCE_REMOVE

    def show_motion_notification(self, generation, camera, alert):
        journal = self.alert_journal
        if journal is None or self.motion_generation is not generation:
            return GLib.SOURCE_REMOVE
        tile = next((tile for tile in self.tiles if tile.camera is camera), None)
        if tile is None:
            return GLib.SOURCE_REMOVE

        name = tile.name_label.get_text()
        unread = self.account_pages.get_visible_child_name() != 'alerts' or not self.is_active()
        entry = AlertEntry(camera.identifier, alert.identifier, name, alert.occurred_at, unread=unread)
        try:
            if not journal.add(entry):
                return GLib.SOURCE_REMOVE
        except (OSError, UnicodeError):
            note('motion_journal_write_failed')
            unread = True
        self.refresh_alerts()
        if unread:
            occurred = entry.occurred_at.astimezone()
            notification = Gio.Notification.new('Mouvement détecté')
            notification.set_body(f'{name} · {occurred:%H:%M}')
            notification.set_icon(Gio.FileIcon.new(Gio.File.new_for_path(str(APP_LAUNCHER_ICON))))
            notification.set_default_action('app.show-alerts')
            notification.add_button('Marquer comme lu', 'app.dismiss-alerts')
            self.get_application().send_notification(MOTION_NOTIFICATION_ID, notification)
        return GLib.SOURCE_REMOVE

    def select_camera(self, tile):
        if tile is self.selected_camera:
            return
        self.keyboard.release()
        if self.selected_camera is not None:
            self.selected_camera.set_selected(False)
        self.selected_camera = tile
        if tile is not None:
            tile.set_selected(True)
            self.panels.set_visible_child(tile.configuration)
        self.camera_grid.focus_camera(tile)
        self.panel_scroll.set_visible(tile is not None and self.account_pages.get_visible_child_name() == 'cameras')

    def keyboard_joystick(self):
        if (self.stack.get_visible_child_name() != 'account'
                or self.account_pages.get_visible_child_name() != 'cameras'):
            return None
        return self.selected_camera.joystick if self.selected_camera is not None else None

    def release_window_microphone(self, *_):
        if not self.is_active():
            for tile in self.tiles:
                tile.stop_talking(tile.talk_button)

    def restore_account(self, future):
        if self.closed or self.login_task is not None:
            return False
        try:
            account = future.result()
        except (GLib.Error, ValueError, ImportError):
            self.info.set_label('Le trousseau est indisponible. Tu peux te connecter manuellement.')
            return False
        if account is not None:
            self.saved_account = True
            self.forget.set_visible(True)
            if not self.email.get_text() and not self.password.get_text():
                email, password = account
                self.email.set_text(email)
                self.remember.set_active(True)
                self.start_login(email, password, automatic=True)
        return False

    def forget_saved_account(self, *_):
        self.connect_button.set_sensitive(False)
        self.forget.set_sensitive(False)
        task = self.worker.submit(asyncio.to_thread(keyring.forget_account))
        task.add_done_callback(lambda future: GLib.idle_add(self.forgot_account, future))

    def forgot_account(self, future):
        if self.closed:
            return False
        self.connect_button.set_sensitive(True)
        self.forget.set_sensitive(True)
        try:
            future.result()
        except (GLib.Error, ImportError):
            self.info.set_label('Le trousseau n’a pas pu effacer le compte.')
        else:
            self.saved_account = False
            self.remember.set_active(False)
            self.forget.set_visible(False)
            self.email.set_text('')
            self.password.set_text('')
            self.info.set_label('Le compte a été oublié sur ce PC.')
        return False

    def sign_in(self, *_):
        if self.login_task is not None or not self.connect_button.get_sensitive():
            return
        email, password = self.email.get_text().strip(), self.password.get_text()
        if not email or not password:
            self.info.set_label('Saisis ton adresse et ton mot de passe Wansview Cloud.')
            return
        self.start_login(email, password)

    def start_login(self, email, password, *, automatic=False):
        remember = self.remember.get_active()
        previously_saved = self.saved_account
        self.password.set_text('')
        self.connect_button.set_sensitive(False)
        self.forget.set_sensitive(False)
        self.info.set_label('Connexion à AJCloud…')

        async def authenticate():
            cameras = await self.worker.account.login(email, password)
            saved = previously_saved
            warning = None
            if not automatic and (remember or previously_saved):
                try:
                    if remember:
                        await asyncio.to_thread(keyring.save_account, email, password)
                    else:
                        await asyncio.to_thread(keyring.forget_account)
                    saved = remember
                except (GLib.Error, ImportError, RuntimeError):
                    warning = 'Le trousseau n’a pas pu mettre à jour le compte mémorisé.'
            return cameras, saved, warning, AlertJournal.for_account(STATE / 'alerts', email)

        self.login_task = self.worker.submit(authenticate())
        self.login_task.add_done_callback(lambda future: GLib.idle_add(self.signed_in, future))

    def signed_in(self, future):
        self.login_task = None
        if self.closed:
            return False
        self.connect_button.set_sensitive(True)
        self.forget.set_sensitive(True)
        try:
            cameras, self.saved_account, warning, journal = future.result()
        except CameraError as exc:
            self.info.set_label(str(exc))
            note('login_failed')
            return False
        except Exception:
            self.info.set_label('La connexion a échoué. Réessaie dans un instant.')
            note('unexpected_login_error')
            return False
        self.forget.set_visible(self.saved_account)
        if warning:
            self.toast(warning)
        self.clear_tiles()
        for index, camera in enumerate(cameras):
            tile = CameraTile(camera, self.worker, index + 1)
            tile.set_display_name(self.preferences.camera_names.get(camera.identifier, camera.name))
            tile.connect('notify::live-state', self.sd_view.live_changed)
            self.tiles.append(tile)
            self.panels.add_child(tile.configuration)
            self.camera_grid.add(tile)
            tile.load_camera_options()
        self.camera_grid.arrange()
        if order := self.camera_grid.ordered_keys():
            self.select_camera(self.camera_grid.tiles[order[0]])
        self.logout.set_visible(True)
        self.page_title.set_visible(True)
        self.alert_journal = journal
        self.refresh_sd_cameras()
        self.show_cameras()
        self.refresh_alerts()
        self.stack.set_visible_child_name('account')
        self.update_motion_notifications()
        self.camera_status_timer = GLib.timeout_add_seconds(STATUS_REFRESH_SECONDS, self.refresh_camera_statuses)
        note(f'login_ok_b6_count={len(cameras)}')
        return False

    def refresh_camera_statuses(self):
        for tile in self.tiles:
            tile.refresh_camera_status()
        return GLib.SOURCE_CONTINUE

    def clear_tiles(self):
        self.sd_view.set_active(False)
        self.sd_view.set_cameras(())
        self.stop_motion_notifications()
        if self.camera_status_timer is not None:
            GLib.source_remove(self.camera_status_timer)
            self.camera_status_timer = None
        self.select_camera(None)
        for tile in self.tiles:
            if tile.controls_task is not None:
                tile.controls_task.cancel()
                tile.controls_task = None
            if tile.camera_status_task is not None:
                tile.camera_status_task.cancel()
                tile.camera_status_task = None
            tile.reset('Direct arrêté.')
            self.panels.remove(tile.configuration)
        self.camera_grid.clear()
        self.tiles.clear()

    def sign_out(self, *_):
        self.clear_tiles()
        self.close_alert_journal()
        self.worker.submit(self.worker.account.close())
        self.logout.set_visible(False)
        self.page_title.set_visible(False)
        self.stack.set_visible_child_name('login')
        self.info.set_label('Session fermée.')

    def size_for_monitor(self, monitor):
        if monitor is not None:
            screen = monitor.get_geometry()
            self.set_default_size(round(screen.width * .75), round(screen.height * .75))

    def watch_initial_monitor(self, *_):
        if not self.initial_monitor_known:
            surface = self.get_surface()
            self.initial_monitor_handler = surface.connect('enter-monitor', self.initial_monitor_entered)

    def initial_monitor_entered(self, surface, monitor):
        surface.disconnect(self.initial_monitor_handler)
        self.initial_monitor_handler = None
        self.initial_monitor_known = True
        self.size_for_monitor(monitor)

    def on_close(self, *_):
        application = self.get_application()
        if not application.quitting:
            application.hide_to_tray()
            return True
        self.closed = True
        self.clear_tiles()
        self.close_alert_journal()
        return False


class Application(Adw.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.HANDLES_COMMAND_LINE)
        self.add_main_option('minimized', 0, GLib.OptionFlags.NONE, GLib.OptionArg.NONE,
                             'Démarrer dans la zone de notification', None)
        self.worker = None
        self.main_window = None
        self.tray = None
        self.startup_fallback = None
        self.quitting = False
        show_cameras = Gio.SimpleAction.new('show-cameras', None)
        show_cameras.connect('activate', lambda *_: self.activate())
        self.add_action(show_cameras)
        show_alerts = Gio.SimpleAction.new('show-alerts', None)
        show_alerts.connect('activate', self.show_alerts)
        self.add_action(show_alerts)
        dismiss_alerts = Gio.SimpleAction.new('dismiss-alerts', None)
        dismiss_alerts.connect('activate', self.dismiss_alerts)
        self.add_action(dismiss_alerts)

    def do_startup(self):
        Adw.Application.do_startup(self)
        css = Gtk.CssProvider()
        directory = Path(__file__).parent
        style = (directory / 'style.css').read_text(encoding='utf-8')
        palettes = {dark: (directory / filename).read_text(encoding='utf-8')
                    for dark, filename in ((True, 'palette-dark.css'), (False, 'palette-light.css'))}
        display = Gdk.Display.get_default()
        manager = Adw.StyleManager.get_for_display(display)

        def load_palette(theme, *_):
            css.load_from_string(palettes[theme.get_dark()] + '\n' + style)

        self.palette_handler = manager.connect('notify::dark', load_palette)
        load_palette(manager)
        Gtk.IconTheme.get_for_display(display).add_search_path(str(ICON_SEARCH_PATH))
        Gtk.Window.set_default_icon_name(APP_ICON_NAME)
        Gtk.StyleContext.add_provider_for_display(display, css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        connection = self.get_dbus_connection()
        if connection is not None:
            self.tray = TrayIcon(connection, self.show_from_tray, self.hide_to_tray, self.quit_from_tray,
                                 manager)

    def ensure_window(self):
        if self.main_window is None:
            self.worker = Worker()
            self.main_window = Window(self, self.worker)
        return self.main_window

    def do_command_line(self, command_line):
        if command_line.get_options_dict().contains('minimized'):
            if self.main_window is None:
                self.ensure_window()
                self.startup_fallback = GLib.timeout_add_seconds(7, self.finish_minimized_startup)
        else:
            self.activate()
        return 0

    def finish_minimized_startup(self):
        self.startup_fallback = None
        if self.tray is None or not self.tray.available:
            self.main_window.minimize()
            self.main_window.present()
        return GLib.SOURCE_REMOVE

    def cancel_startup_fallback(self):
        if self.startup_fallback is not None:
            GLib.source_remove(self.startup_fallback)
            self.startup_fallback = None

    def do_activate(self):
        self.cancel_startup_fallback()
        window = self.ensure_window()
        (self.get_active_window() or window).present()

    def show_from_tray(self):
        self.activate()
        return GLib.SOURCE_REMOVE

    def show_alerts(self, *_):
        self.activate()
        self.main_window.present()
        self.main_window.open_alerts()

    def dismiss_alerts(self, *_):
        if self.main_window is not None:
            self.main_window.dismiss_alert_notification()
        else:
            self.withdraw_notification(MOTION_NOTIFICATION_ID)

    def hide_to_tray(self):
        window = self.main_window
        if window is not None:
            self.cancel_startup_fallback()
            window.keyboard.release()
            window.alerts_view.clip.close()
            for tile in window.tiles:
                tile.release_joysticks()
                tile.stop_talking()
                if tile.fullscreen_window is not None:
                    tile.fullscreen_window.close()
            if self.tray is not None and self.tray.available:
                window.set_visible(False)
            else:
                window.minimize()
        return GLib.SOURCE_REMOVE

    def quit_from_tray(self):
        self.quitting = True
        if self.main_window is not None:
            self.main_window.close()
        else:
            self.quit()
        return GLib.SOURCE_REMOVE

    def do_shutdown(self):
        self.cancel_startup_fallback()
        Adw.StyleManager.get_for_display(Gdk.Display.get_default()).disconnect(self.palette_handler)
        if self.tray is not None:
            self.tray.close()
        if self.worker:
            self.worker.close()
        Adw.Application.do_shutdown(self)


def main():
    if sys.argv[1:] == ['--version']:
        print(f'{APP_NAME} {__version__}')
        return 0
    GLib.set_application_name(APP_NAME)
    Gst.init(None)
    return Application().run(sys.argv)


if __name__ == '__main__':
    sys.exit(main())
