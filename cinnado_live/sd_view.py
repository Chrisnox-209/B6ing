"""Browse the recordings of a microSD card and play them in place."""

import asyncio
from concurrent.futures import CancelledError
from datetime import date, datetime, time, timedelta
from queue import Empty, Queue

from gi.repository import Adw, Gdk, GLib, Graphene, Gst, Gtk

from .alerts_view import _icon_button, _preview_texture
from .desktop import APP_ICON_NAME
from .engine import CameraError
from .media import clip_thumbnail


class ClipTimeline(Gtk.Widget):
    def __init__(self, activate):
        super().__init__(height_request=49, hexpand=True, focusable=True, css_classes=['clip-timeline'])
        self.activate = activate
        self.segments = ()
        self.ticks = ()
        self.selected = None
        self.update_property([Gtk.AccessibleProperty.LABEL], ['Frise des enregistrements de la journée'])
        click = Gtk.GestureClick(button=Gdk.BUTTON_PRIMARY)
        click.connect('pressed', self.point)
        self.add_controller(click)
        key = Gtk.EventControllerKey()
        key.connect('key-pressed', self.key_pressed)
        self.add_controller(key)

    def set_clips(self, clips, day=None, tz=None):
        self.segments = ()
        self.ticks = ()
        self.selected = None
        if clips:
            start = datetime.combine(day, time.min, tz).timestamp()
            span = datetime.combine(day + timedelta(days=1), time.min, tz).timestamp() - start
            self.segments = tuple((clip, (clip.group_id - start) / span, max(1, clip.duration) / span)
                                  for clip in sorted(clips, key=lambda item: item.group_id)
                                  if start <= clip.group_id < start + span)
            self.ticks = tuple((f'{hour:02d}:00',
                                (datetime.combine(day, time(hour), tz).timestamp() - start) / span)
                               for hour in (0, 6, 12, 18)) + (('24:00', 1),)
        self.set_cursor_from_name('pointer' if self.segments else None)
        self.queue_draw()

    def select(self, clip):
        self.selected = clip
        self.queue_draw()

    def point(self, _gesture, _count, x, _y):
        if self.segments and self.get_width():
            self.grab_focus()
            point = x / self.get_width()
            clip, _, _ = min(self.segments, key=lambda item: (
                max(item[1] - point, 0, point - item[1] - item[2]),
                abs(item[1] + item[2] / 2 - point),
            ))
            self.activate(clip)

    def key_pressed(self, _controller, key, *_):
        if key not in (Gdk.KEY_Left, Gdk.KEY_Right) or not self.segments:
            return False
        clips = [segment[0] for segment in self.segments]
        index = clips.index(self.selected) if self.selected in clips else 0
        if self.selected in clips:
            index = max(0, min(len(clips) - 1, index + (1 if key == Gdk.KEY_Right else -1)))
        self.activate(clips[index])
        return True

    def do_snapshot(self, snapshot):
        width = self.get_width()
        track, segment, selected, label_color = (Gdk.RGBA() for _ in range(4))
        track.parse('#334555')
        segment.parse('#48c4e5')
        selected.parse('#fbb75e')
        label_color.parse('#7c8897')
        snapshot.append_color(track, Graphene.Rect().init(0, 12, width, 12))
        for label, fraction in self.ticks:
            x = min(width - 1, width * fraction)
            snapshot.append_color(track, Graphene.Rect().init(x, 7, 1, 22))
            text = self.create_pango_layout(label)
            text_width, _ = text.get_pixel_size()
            snapshot.save()
            snapshot.translate(Graphene.Point().init(max(0, min(width - text_width, x - text_width / 2)), 31))
            snapshot.append_layout(text, label_color)
            snapshot.restore()
        for clip, start, duration in self.segments:
            x = width * start
            size = min(width - x, max(4, width * duration))
            snapshot.append_color(segment, Graphene.Rect().init(x, 10, size, 16))
        if self.selected is not None:
            for clip, start, duration in self.segments:
                if clip == self.selected:
                    x = width * start
                    snapshot.append_color(selected, Graphene.Rect().init(x, 6, min(width - x, max(6, width * duration)), 24))
                    break


class SdView(Gtk.Box):
    def __init__(self, worker, preview_factory):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=14, margin_top=18,
                         margin_bottom=18, margin_start=18, margin_end=18)
        self.worker = worker
        self.preview_factory = preview_factory
        self.cameras = ()
        self.days = ()
        self.items = ()
        self.rows = {}
        self.textures = {}
        self.attempted = set()
        self.active = False
        self.calendar_task = self.list_task = self.play_task = self.thumbnail_task = None
        self.day_after_calendar = None
        self.preview = self.frame_timer = self.thumbnail_timer = None
        self.tz = None
        self.selected = None

        header = Gtk.Box(spacing=8)
        header.append(Gtk.Label(label='Carte microSD', xalign=0, hexpand=True, css_classes=['title-2']))
        self.selector = Gtk.DropDown()
        self.selector.update_property([Gtk.AccessibleProperty.LABEL], ['Caméra pour la lecture microSD'])
        self.camera_handler = self.selector.connect('notify::selected', self.camera_changed)
        header.append(self.selector)
        refresh = _icon_button('view-refresh-symbolic', 'Actualiser le calendrier')
        refresh.connect('clicked', self.load_calendar)
        header.append(refresh)
        self.append(header)

        browser = Gtk.Box(spacing=15, vexpand=True)
        self.append(browser)
        date_picker = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, valign=Gtk.Align.START)
        self.calendar = Gtk.Calendar()
        self.calendar.add_css_class('sd-calendar')
        self.calendar.connect('notify::date', self.mark_calendar)
        date_picker.append(self.calendar)
        list_button = Gtk.Button(label='Voir les clips du jour')
        list_button.connect('clicked', self.load_clips)
        date_picker.append(list_button)
        self.calendar_status = Gtk.Label(xalign=0, wrap=True, max_width_chars=28, css_classes=['dim-label'])
        date_picker.append(self.calendar_status)
        browser.append(date_picker)

        results = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=9, hexpand=True)
        browser.append(results)
        self.list_status = Gtk.Label(xalign=0, wrap=True, css_classes=['heading'])
        results.append(self.list_status)
        self.timeline_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, visible=False,
                                    css_classes=['sd-timeline-card'])
        self.timeline = ClipTimeline(self.play)
        self.timeline_box.append(self.timeline)
        results.append(self.timeline_box)

        self.player = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, visible=False,
                              css_classes=['alert-player'])
        playback_header = Gtk.Box(spacing=6)
        self.play_status = Gtk.Label(xalign=0, hexpand=True, wrap=True)
        playback_header.append(self.play_status)
        self.previous = _icon_button('media-skip-backward-symbolic', 'Clip précédent')
        self.next = _icon_button('media-skip-forward-symbolic', 'Clip suivant')
        close = _icon_button('window-close-symbolic', 'Fermer le lecteur')
        self.previous.connect('clicked', lambda *_: self.step(-1))
        self.next.connect('clicked', lambda *_: self.step(1))
        close.connect('clicked', self.close_player)
        for button in (self.previous, self.next, close):
            playback_header.append(button)
        self.player.append(playback_header)
        self.picture = Gtk.Picture(content_fit=Gtk.ContentFit.CONTAIN, can_shrink=True,
                                   height_request=230, css_classes=['video-area'])
        self.player.append(self.picture)
        results.append(self.player)

        self.clips = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE, css_classes=['boxed-list'])
        self.scroll = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER, vexpand=True,
                                         min_content_height=100, child=self.clips)
        results.append(self.scroll)
        adjustment = self.scroll.get_vadjustment()
        adjustment.connect('changed', self.schedule_previews)
        adjustment.connect('value-changed', self.schedule_previews)
        self.connect('map', self.schedule_previews)

    def selected_camera(self):
        index = self.selector.get_selected()
        return self.cameras[index][0] if index < len(self.cameras) else None

    def set_cameras(self, cameras):
        cameras = tuple(cameras)
        if cameras == self.cameras:
            return
        selected = self.selected_camera()
        self.cameras = cameras
        with self.selector.handler_block(self.camera_handler):
            self.selector.set_model(Gtk.StringList.new([name for _, name in cameras]))
            self.selector.set_selected(next((i for i, (camera, _) in enumerate(cameras) if camera is selected), 0)
                                       if cameras else Gtk.INVALID_LIST_POSITION)
        if self.selected_camera() is not selected:
            self.days = ()
            self.mark_calendar()
            if self.active and cameras:
                self.load_calendar()
            else:
                self.clear_clips()
        if not cameras:
            self.textures.clear()

    def set_active(self, active):
        if self.active == active:
            return
        self.active = active
        if active:
            if self.days or self.items:
                self.schedule_previews()
            else:
                self.load_calendar()
        else:
            self.cancel_queries()
            self.close_player()

    def cancel_queries(self):
        for task in (self.calendar_task, self.list_task):
            if task is not None:
                task.cancel()
        self.calendar_task = self.list_task = None
        self.day_after_calendar = None
        self.cancel_previews()

    def camera_changed(self, *_):
        if self.active:
            self.days = ()
            self.mark_calendar()
            self.load_calendar()

    def picked_day(self):
        value = self.calendar.get_date()
        return date(value.get_year(), value.get_month(), value.get_day_of_month())

    def mark_calendar(self, *_):
        shown = self.picked_day()
        self.calendar.clear_marks()
        for day in self.days:
            if (day.year, day.month) == (shown.year, shown.month):
                self.calendar.mark_day(day.day)

    def clear_clips(self):
        self.close_player()
        self.cancel_previews()
        self.attempted.clear()
        self.rows.clear()
        self.items = ()
        self.clips.remove_all()
        self.timeline.set_clips(())
        self.timeline_box.set_visible(False)

    def load_calendar(self, *_):
        camera = self.selected_camera()
        if camera is None:
            return
        self.cancel_queries()
        self.clear_clips()
        self.calendar_status.set_label('Chargement du calendrier…')
        self.list_status.set_label('Choisis un jour pour afficher ses clips.')
        task = self.calendar_task = self.worker.submit(self.worker.account.read_sd_calendar(camera))
        task.add_done_callback(lambda future: GLib.idle_add(self.calendar_finished, future))

    def calendar_finished(self, future):
        if future is not self.calendar_task:
            return GLib.SOURCE_REMOVE
        self.calendar_task = None
        requested_day, self.day_after_calendar = self.day_after_calendar, None
        try:
            days = future.result()
        except CameraError as exc:
            self.calendar_status.set_label(str(exc))
        except CancelledError:
            pass
        except Exception:
            self.calendar_status.set_label('Impossible de charger le calendrier microSD.')
        else:
            if days:
                self.days = days
            if self.days:
                self.calendar_status.set_label('Pastille bleue · enregistrements disponibles')
                if requested_day is None and self.picked_day() not in self.days:
                    recent = self.days[-1]
                    self.calendar.select_day(GLib.DateTime.new_local(recent.year, recent.month, recent.day, 12, 0, 0))
                self.mark_calendar()
            else:
                self.calendar_status.set_label('La caméra n’a renvoyé aucune date. Tu peux choisir un jour précis.')
        if requested_day is not None:
            self.request_clips(requested_day)
        return GLib.SOURCE_REMOVE

    def load_clips(self, *_):
        day = self.picked_day()
        if self.calendar_task is not None:
            self.day_after_calendar = day
            self.list_status.set_label(f'Clips du {day:%d/%m/%Y} après le chargement du calendrier…')
            return
        self.request_clips(day)

    def request_clips(self, day):
        camera = self.selected_camera()
        if camera is None:
            return
        self.cancel_queries()
        self.clear_clips()
        self.list_status.set_label(f'Chargement des clips du {day:%d/%m/%Y}…')
        task = self.list_task = self.worker.submit(self.worker.account.read_sd_clips(camera, day))
        task.add_done_callback(lambda future: GLib.idle_add(self.clips_finished, future, day, camera.identifier))

    def clips_finished(self, future, day, camera_id):
        if future is not self.list_task:
            return GLib.SOURCE_REMOVE
        self.list_task = None
        try:
            self.items, self.tz = future.result()
        except CameraError as exc:
            self.list_status.set_label(str(exc))
        except CancelledError:
            pass
        except Exception:
            self.list_status.set_label('Impossible de charger la liste des clips microSD.')
        else:
            self.show_clips(day, camera_id)
        return GLib.SOURCE_REMOVE

    def show_clips(self, day, camera_id):
        total = len(self.items)
        if total and day not in self.days:
            self.days = tuple(sorted((*self.days, day)))
            self.mark_calendar()
        elif not total and day in self.days:
            self.days = tuple(recorded for recorded in self.days if recorded != day)
            self.mark_calendar()
        if self.days:
            self.calendar_status.set_label('Pastille bleue · enregistrements disponibles')
        self.list_status.set_label(f'{total} clip{"s" if total > 1 else ""} · {day:%d/%m/%Y}' if total else
                                   f'Aucun clip pour le {day:%d/%m/%Y}.')
        self.timeline.set_clips(self.items, day, self.tz)
        self.timeline_box.set_visible(bool(self.timeline.segments))
        for clip in self.items:
            hour = clip.started.astimezone(self.tz).strftime('%H:%M:%S')
            row = Adw.ActionRow(title=hour, subtitle=f'{clip.duration} s' if clip.duration else '', use_markup=False)
            thumbnail = Gtk.Overlay(width_request=112, height_request=64, valign=Gtk.Align.CENTER,
                                    overflow=Gtk.Overflow.HIDDEN, css_classes=['alert-thumbnail'])
            thumbnail.set_child(Gtk.Image(icon_name=APP_ICON_NAME, pixel_size=22))
            picture = Gtk.Picture(paintable=self.textures.get((camera_id, clip)), can_shrink=True,
                                  content_fit=Gtk.ContentFit.COVER)
            thumbnail.add_overlay(picture)
            row.add_prefix(thumbnail)
            play = _icon_button('media-playback-start-symbolic', f'Lire le clip de {hour}')
            play.add_css_class('alert-play')
            play.connect('clicked', lambda _, item=clip: self.play(item))
            row.add_suffix(play)
            row.set_activatable_widget(play)
            self.rows[clip] = row, picture
            self.clips.append(row)
        self.schedule_previews()

    def schedule_previews(self, *_):
        if self.active and self.items and self.thumbnail_timer is None and self.thumbnail_task is None and not self.play_task:
            self.thumbnail_timer = GLib.timeout_add(160, self.load_visible_previews)

    def cancel_previews(self):
        if self.thumbnail_timer is not None:
            GLib.source_remove(self.thumbnail_timer)
            self.thumbnail_timer = None
        if self.thumbnail_task is not None:
            self.thumbnail_task.cancel()
            self.thumbnail_task = None

    def load_visible_previews(self):
        self.thumbnail_timer = None
        camera = self.selected_camera()
        adjustment = self.scroll.get_vadjustment()
        if camera is None or not self.active or not self.get_mapped() or adjustment.get_page_size() <= 0:
            return GLib.SOURCE_REMOVE
        top = adjustment.get_value()
        bottom = top + adjustment.get_page_size()
        chosen = []
        for clip, (row, picture) in self.rows.items():
            if clip not in self.attempted and picture.get_paintable() is None:
                valid, bounds = row.compute_bounds(self.clips)
                if valid and bounds.get_y() + bounds.get_height() >= top and bounds.get_y() <= bottom:
                    chosen.append(clip)
                    if len(chosen) == 3:
                        break
        if not chosen:
            return GLib.SOURCE_REMOVE

        async def decode(clip, frames, encoding):
            try:
                raw = await asyncio.to_thread(clip_thumbnail, frames, encoding)
            except (GLib.Error, RuntimeError, TypeError):
                raw = None
            GLib.idle_add(self.show_preview, task, camera.identifier, clip, raw)

        task = self.thumbnail_task = self.worker.submit(self.worker.account.read_sd_thumbnails(camera, chosen, decode))
        task.add_done_callback(lambda future: GLib.idle_add(self.previews_finished, future, tuple(chosen)))
        return GLib.SOURCE_REMOVE

    def show_preview(self, task, camera_id, clip, raw):
        if task is self.thumbnail_task and clip in self.rows:
            self.attempted.add(clip)
            texture = _preview_texture(raw) if raw else None
            if texture is not None:
                while len(self.textures) >= 120:
                    del self.textures[next(iter(self.textures))]
                self.textures[(camera_id, clip)] = texture
                self.rows[clip][1].set_paintable(texture)
        return GLib.SOURCE_REMOVE

    def previews_finished(self, future, clips):
        if future is self.thumbnail_task:
            self.thumbnail_task = None
            self.attempted.update(clips)
            self.schedule_previews()
        return GLib.SOURCE_REMOVE

    def step(self, direction):
        if self.selected is None:
            return
        newest_first = self.items.index(self.selected)
        next_index = newest_first - direction
        if 0 <= next_index < len(self.items):
            self.play(self.items[next_index])

    def play(self, clip):
        camera = self.selected_camera()
        if camera is None or clip not in self.rows:
            return
        self.close_player()
        self.cancel_previews()
        self.selected = clip
        self.timeline.select(clip)
        row, _ = self.rows[clip]
        row.add_css_class('sd-clip-selected')
        self.player.set_visible(True)
        GLib.idle_add(self.reveal_row, clip)
        self.previous.set_sensitive(self.items.index(clip) < len(self.items) - 1)
        self.next.set_sensitive(self.items.index(clip) > 0)
        hour = clip.started.astimezone(self.tz).strftime('%H:%M:%S')
        self.play_status.set_label(f'{hour} · Ouverture du clip…')
        frames = Queue(maxsize=100)

        async def play_clip():
            stop = asyncio.Event()

            async def frame(data, codec, fps):
                if frames.full():
                    raise CameraError('Le lecteur microSD n’arrive pas à suivre le flux.')
                frames.put_nowait((data, codec, fps))

            await self.worker.account.play_sd_clip(camera, clip, frame, stop)

        task = self.play_task = self.worker.submit(play_clip())
        self.frame_timer = GLib.timeout_add(16, self.drain, task, frames)
        task.add_done_callback(lambda future: GLib.idle_add(self.play_finished, future, frames, hour))

    def reveal_row(self, clip):
        if clip == self.selected:
            row = self.rows[clip][0]
            valid, bounds = row.compute_bounds(self.clips)
            if valid:
                adjustment = self.scroll.get_vadjustment()
                adjustment.clamp_page(bounds.get_y(), bounds.get_y() + bounds.get_height())
        return GLib.SOURCE_REMOVE

    def drain(self, task, frames):
        if task is not self.play_task:
            return GLib.SOURCE_REMOVE
        for _ in range(12):
            try:
                data, codec, fps = frames.get_nowait()
            except Empty:
                break
            try:
                if self.preview is None or self.preview.codec != codec:
                    if self.preview is not None:
                        self.preview.close()
                    self.preview = self.preview_factory(self.picture, codec, fps,
                                                        lambda: self.first_image(task),
                                                        lambda: self.decoder_failed(task))
                if self.preview.push(data) != Gst.FlowReturn.OK:
                    raise CameraError('Décodage interrompu.')
            except (CameraError, GLib.Error, KeyError, RuntimeError):
                self.frame_timer = None
                self.decoder_failed(task)
                return GLib.SOURCE_REMOVE
        return GLib.SOURCE_CONTINUE

    def first_image(self, task):
        if task is self.play_task and self.selected is not None and not task.done():
            hour = self.selected.started.astimezone(self.tz).strftime('%H:%M:%S')
            self.play_status.set_label(f'{hour} · Lecture du clip')
        return GLib.SOURCE_REMOVE

    def decoder_failed(self, task):
        if task is self.play_task:
            self.close_player()
            self.player.set_visible(True)
            self.previous.set_sensitive(False)
            self.next.set_sensitive(False)
            self.play_status.set_label('Ubuntu n’a pas pu décoder le clip microSD.')

    def play_finished(self, future, frames, hour):
        if future is not self.play_task:
            return GLib.SOURCE_REMOVE
        if not frames.empty() and self.frame_timer is not None:
            GLib.timeout_add(20, self.play_finished, future, frames, hour)
            return GLib.SOURCE_REMOVE
        self.remove_frame_timer()
        self.play_task = None
        try:
            future.result()
        except CameraError as exc:
            self.play_status.set_label(str(exc))
        except Exception:
            self.play_status.set_label('La lecture du clip microSD s’est interrompue.')
        else:
            self.play_status.set_label(f'{hour} · Lecture terminée')
        self.schedule_previews()
        return GLib.SOURCE_REMOVE

    def remove_frame_timer(self):
        if self.frame_timer is not None:
            GLib.source_remove(self.frame_timer)
            self.frame_timer = None

    def close_player(self, *_):
        self.remove_frame_timer()
        if self.play_task is not None:
            self.play_task.cancel()
            self.play_task = None
        if self.preview is not None:
            self.preview.close()
            self.preview = None
        if self.selected in self.rows:
            self.rows[self.selected][0].remove_css_class('sd-clip-selected')
        self.selected = None
        self.timeline.select(None)
        self.player.set_visible(False)
        self.schedule_previews()
