"""The alert journal and its inline video player."""

from concurrent.futures import CancelledError
from pathlib import Path
from tempfile import TemporaryDirectory

import gi
gi.require_version('GdkPixbuf', '2.0')
from gi.repository import Adw, Gdk, GdkPixbuf, GLib, Gtk

from .engine import CameraError


def _preview_texture(image):
    loader = GdkPixbuf.PixbufLoader.new()

    def resize(_loader, width, height):
        scale = min(1, 224 / width, 128 / height)
        loader.set_size(max(1, round(width * scale)), max(1, round(height * scale)))

    loader.connect('size-prepared', resize)
    try:
        loader.write(image)
        loader.close()
    except GLib.Error:
        try:
            loader.close()
        except GLib.Error:
            pass
        return None
    pixels = loader.get_pixbuf()
    return Gdk.Texture.new_for_pixbuf(pixels) if pixels is not None else None


def _icon_button(icon, description):
    button = Gtk.Button(icon_name=icon, valign=Gtk.Align.CENTER, tooltip_text=description)
    button.update_property([Gtk.AccessibleProperty.LABEL], [description])
    return button


class AlertsView(Gtk.Box):
    def __init__(self, remove, clear, play_clip, download_clip, request_previews):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=12,
                         margin_top=18, margin_bottom=18, margin_start=18, margin_end=18)
        self.remove = remove
        self.clear = clear
        self.play_clip = play_clip
        self.download_clip = download_clip
        self.request_previews = request_previews
        self.rows = {}
        self.textures = {}
        self.pending_previews = set()
        self.downloading = set()
        self.entries = ()
        self.active = False
        self.preview_source = None

        header = Gtk.Box(spacing=12)
        header.append(Gtk.Label(label='Alertes de mouvement', xalign=0, hexpand=True, css_classes=['title-2']))
        self.clear_button = Gtk.Button(label='Tout effacer')
        self.clear_button.connect('clicked', self.confirm_clear)
        header.append(self.clear_button)
        self.append(header)

        self.clip = AlertClipPlayer()
        self.append(self.clip)
        self.pages = Gtk.Stack(vexpand=True)
        self.append(self.pages)
        self.empty = Adw.StatusPage(title='Aucune alerte', icon_name='preferences-system-notifications-symbolic')
        self.pages.add_named(self.empty, 'empty')

        self.listbox = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        self.listbox.add_css_class('boxed-list')
        self.scroll = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER)
        self.scroll.set_child(self.listbox)
        self.pages.add_named(self.scroll, 'entries')
        adjustment = self.scroll.get_vadjustment()
        adjustment.connect('changed', self.queue_previews)
        adjustment.connect('value-changed', self.queue_previews)
        self.connect('map', self.queue_previews)

    def set_active(self, active):
        self.active = active
        if active:
            self.queue_previews()
        else:
            if self.preview_source is not None:
                GLib.source_remove(self.preview_source)
                self.preview_source = None
            self.clip.close()

    def show_entries(self, entries, names, alerts_enabled):
        self.clear_button.set_sensitive(bool(entries))
        self.empty.set_description('Les mouvements détectés apparaîtront ici.' if alerts_enabled else
                                   'Active les alertes de mouvement dans les réglages.')
        self.pages.set_visible_child_name('entries' if entries else 'empty')
        displayed = tuple((entry, names.get(entry.camera_id, entry.camera_name), entry.camera_id in names)
                          for entry in entries)
        if displayed == self.entries:
            self.queue_previews()
            return
        self.entries = displayed
        keys = {entry.key for entry in entries}
        self.textures = {key: texture for key, texture in self.textures.items() if key in keys}
        self.listbox.remove_all()
        self.rows.clear()
        for entry, name, available in self.entries:
            occurred = entry.occurred_at.astimezone()
            row = Adw.ActionRow(title=name, use_markup=False, title_lines=1, subtitle_lines=1,
                                subtitle=f'{occurred:%d/%m/%Y à %H:%M:%S}')
            thumbnail = Gtk.Overlay(width_request=112, height_request=64, valign=Gtk.Align.CENTER,
                                    overflow=Gtk.Overflow.HIDDEN, css_classes=['alert-thumbnail'])
            thumbnail.set_child(Gtk.Image(icon_name='camera-video-symbolic', pixel_size=18))
            picture = Gtk.Picture(paintable=self.textures.get(entry.key), can_shrink=True,
                                  content_fit=Gtk.ContentFit.COVER)
            thumbnail.add_overlay(picture)
            row.add_prefix(thumbnail)

            play = _icon_button('media-playback-start-symbolic', f'Lire le clip de {name}')
            play.add_css_class('alert-play')
            play.set_sensitive(available)
            play.connect('clicked', lambda _, item=entry, title=name: self.play_clip(item, title))
            row.add_suffix(play)
            if available:
                row.set_activatable_widget(play)
            download = _icon_button('b6ing-download-symbolic', f'Télécharger le clip de {name}')
            download.set_sensitive(available and entry.key not in self.downloading)
            download.connect('clicked', lambda _, item=entry, title=name: self.download_clip(item, title))
            row.add_suffix(download)
            remove = _icon_button('user-trash-symbolic', f'Effacer l’alerte de {name}')
            remove.add_css_class('flat')
            remove.connect('clicked', lambda _, key=entry.key: self.remove(key))
            row.add_suffix(remove)
            self.rows[entry.key] = (row, picture, download, available)
            self.listbox.append(row)
        self.queue_previews()

    def queue_previews(self, *_):
        if self.active and self.preview_source is None and self.entries:
            self.preview_source = GLib.timeout_add(100, self.load_visible_previews)

    def load_visible_previews(self):
        self.preview_source = None
        if not self.active or not self.get_mapped() or self.pending_previews:
            return GLib.SOURCE_REMOVE
        adjustment = self.scroll.get_vadjustment()
        height = adjustment.get_page_size()
        if height <= 0:
            return GLib.SOURCE_REMOVE
        top, bottom = adjustment.get_value() - 72, adjustment.get_value() + height + 72
        candidates = []
        for entry, _, available in self.entries:
            if not available or entry.key in self.textures or entry.key in self.pending_previews:
                continue
            row = self.rows[entry.key][0]
            valid, bounds = row.compute_bounds(self.listbox)
            if valid and bounds.get_y() + bounds.get_height() >= top and bounds.get_y() <= bottom:
                candidates.append(entry)
        if candidates:
            self.pending_previews.update(entry.key for entry in candidates)
            self.request_previews(candidates)
        return GLib.SOURCE_REMOVE

    def show_previews(self, keys, images):
        self.pending_previews.difference_update(keys)
        for key in keys:
            if key in self.rows:
                image = images.get(key)
                texture = _preview_texture(image) if image else None
                self.textures[key] = texture
                self.rows[key][1].set_paintable(texture)
        self.queue_previews()

    def set_downloading(self, key, active):
        if active:
            self.downloading.add(key)
        else:
            self.downloading.discard(key)
        if key in self.rows:
            _, _, button, available = self.rows[key]
            button.set_sensitive(available and not active)

    def reset(self):
        self.set_active(False)
        self.show_entries((), {}, True)
        self.pending_previews.clear()
        self.downloading.clear()

    def confirm_clear(self, *_):
        prompt = Adw.AlertDialog(heading='Effacer le journal d’alertes ?',
                                 body='Toutes les alertes enregistrées sur ce PC pour ce compte seront supprimées.')
        prompt.add_response('cancel', 'Annuler')
        prompt.add_response('clear', 'Tout effacer')
        prompt.set_response_appearance('clear', Adw.ResponseAppearance.DESTRUCTIVE)
        prompt.set_close_response('cancel')
        prompt.set_default_response('cancel')
        prompt.connect('response::clear', lambda *_: self.clear())
        prompt.present(self)


class AlertClipPlayer(Gtk.Box):
    """Play a cloud alert clip inside the alert page."""

    def __init__(self):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=8, visible=False)
        self.temporary = None
        self.task = None
        self.stream = None
        self.key = None
        self.add_css_class('alert-player')

        header = Gtk.Box(spacing=12)
        self.title = Adw.WindowTitle(hexpand=True, halign=Gtk.Align.START)
        header.append(self.title)
        close = _icon_button('window-close-symbolic', 'Fermer le lecteur')
        close.add_css_class('flat')
        close.connect('clicked', lambda *_: self.close())
        header.append(close)
        self.append(header)

        self.pages = Gtk.Stack(vhomogeneous=False)
        self.append(self.pages)
        loading = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12, height_request=64,
                          halign=Gtk.Align.CENTER, valign=Gtk.Align.CENTER)
        loading.append(Gtk.Spinner(spinning=True))
        loading.append(Gtk.Label(label='Chargement du clip…'))
        self.pages.add_named(loading, 'loading')
        self.error = Gtk.Label(wrap=True, height_request=64, justify=Gtk.Justification.CENTER)
        self.pages.add_named(self.error, 'error')
        self.player = Gtk.Video(autoplay=True)
        frame = Gtk.AspectFrame(ratio=16 / 9, obey_child=False, height_request=235)
        frame.set_child(self.player)
        self.pages.add_named(frame, 'video')

    def play(self, worker, camera, entry, name):
        self.close()
        self.temporary = TemporaryDirectory(prefix='b6ing-alert-')
        path = Path(self.temporary.name) / 'clip'
        self.key = entry.key
        occurred = entry.occurred_at.astimezone()
        self.title.set_title(name)
        self.title.set_subtitle(f'Clip d’alerte · {occurred:%d/%m/%Y à %H:%M}')
        self.pages.set_visible_child_name('loading')
        self.set_visible(True)
        download = worker.account.download_alert_clip(camera, entry.identifier, entry.occurred_at, path)
        self.task = worker.submit(download)
        self.task.add_done_callback(lambda task: GLib.idle_add(self.download_finished, task, path))

    def download_finished(self, task, path):
        if task is not self.task:
            return GLib.SOURCE_REMOVE
        self.task = None
        try:
            task.result()
        except CancelledError:
            return GLib.SOURCE_REMOVE
        except CameraError as exc:
            self.show_error(str(exc))
        except OSError:
            self.show_error('Impossible d’ouvrir le fichier temporaire du clip.')
        except Exception:
            self.show_error('Le chargement du clip a échoué. Réessaie dans un instant.')
        else:
            self.stream = Gtk.MediaFile.new_for_filename(str(path))
            self.stream.connect('notify::error', self.media_error)
            self.player.set_media_stream(self.stream)
            self.pages.set_visible_child_name('video')
            self.media_error(self.stream)
        return GLib.SOURCE_REMOVE

    def media_error(self, stream, *_):
        if stream is self.stream and stream.get_error() is not None:
            self.show_error('Ubuntu n’a pas pu décoder la vidéo de cette alerte.')

    def show_error(self, message):
        self.error.set_label(message)
        self.pages.set_visible_child_name('error')

    def close(self):
        if self.task is not None:
            task, self.task = self.task, None
            task.cancel()
        if self.stream is not None:
            stream, self.stream = self.stream, None
            stream.pause()
            self.player.set_media_stream(None)
            stream.clear()
        if self.temporary is not None:
            temporary, self.temporary = self.temporary, None
            temporary.cleanup()
        self.key = None
        self.set_visible(False)
