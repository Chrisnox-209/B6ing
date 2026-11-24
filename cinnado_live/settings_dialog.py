"""GTK settings for local media and camera names."""

from pathlib import Path

from gi.repository import Adw, Gio, GLib, Gtk

from .desktop import APP_NAME, autostart_enabled
from .preferences import Preferences


class FolderRow(Adw.ActionRow):
    def __init__(self, title, default_folder, folder, toasts):
        super().__init__(title=title, use_markup=False, subtitle_lines=2)
        self.default_folder = default_folder
        self.toasts = toasts

        self.reset_folder = Gtk.Button(icon_name='edit-clear-symbolic', valign=Gtk.Align.CENTER,
                                       tooltip_text='Rétablir le dossier par défaut')
        self.reset_folder.update_property([Gtk.AccessibleProperty.LABEL],
                                          [f'Rétablir le {title.lower()} par défaut'])
        self.reset_folder.connect('clicked', lambda *_: self.set_folder(None))
        self.add_suffix(self.reset_folder)
        choose = Gtk.Button(label='Choisir…', valign=Gtk.Align.CENTER)
        choose.update_property([Gtk.AccessibleProperty.LABEL], [f'Choisir le {title.lower()}'])
        choose.connect('clicked', self.choose_folder)
        self.add_suffix(choose)
        self.set_activatable_widget(choose)
        self.set_folder(folder)

    def set_folder(self, folder):
        self.folder = folder
        path = str(folder or self.default_folder)
        self.set_subtitle(path)
        self.set_tooltip_text(path)
        self.reset_folder.set_visible(folder is not None)

    def choose_folder(self, *_):
        path = self.folder or self.default_folder
        if not path.is_dir():
            path = path.parent if path.parent.is_dir() else Path.home()
        dialog = Gtk.FileDialog(title=f'Choisir le {self.get_title().lower()}',
                                initial_folder=Gio.File.new_for_path(str(path)))
        dialog.select_folder(self.get_root(), None, self.folder_chosen)

    def folder_chosen(self, dialog, result):
        if self.get_root() is None:
            return
        try:
            path = dialog.select_folder_finish(result).get_path()
        except GLib.Error as exc:
            if not exc.matches(Gtk.dialog_error_quark(), Gtk.DialogError.DISMISSED):
                self.toasts.add_toast(Adw.Toast.new('Impossible d’ouvrir ce dossier.'))
            return
        if path and Path(path).is_dir():
            self.set_folder(Path(path))
        else:
            self.toasts.add_toast(Adw.Toast.new('Choisis un dossier accessible depuis ce PC.'))


class SettingsDialog(Adw.Dialog):
    def __init__(self, preferences, default_recording_folder, default_photo_folder, cameras, save):
        super().__init__(title='Réglages', content_width=520)
        self.preferences = preferences
        self.save = save
        self.entries = {}

        toolbar = Adw.ToolbarView()
        self.toasts = Adw.ToastOverlay(child=toolbar)
        self.set_child(self.toasts)
        header = Adw.HeaderBar()
        toolbar.add_top_bar(header)
        apply = Gtk.Button(label='Enregistrer')
        apply.add_css_class('suggested-action')
        apply.connect('clicked', self.apply)
        header.pack_end(apply)

        page = Adw.PreferencesPage()
        toolbar.set_content(page)
        general = Adw.PreferencesGroup(title='Général')
        self.autostart = Adw.SwitchRow(
            title='Lancer au démarrage',
            subtitle='Démarre réduit dans la zone de notification à l’ouverture de session.',
            active=autostart_enabled(),
        )
        general.add(self.autostart)
        self.motion_notifications = Adw.SwitchRow(
            title='Alertes de mouvement sur ce PC',
            subtitle=f'Ajoutées au journal tant que {APP_NAME} reste ouvert, même réduit.',
            active=preferences.motion_notifications,
        )
        general.add(self.motion_notifications)
        page.add(general)
        media = Adw.PreferencesGroup(title='Vidéos et photos')
        page.add(media)
        self.video_folder = FolderRow('Dossier des vidéos', default_recording_folder,
                                      preferences.recording_folder, self.toasts)
        self.photo_folder = FolderRow('Dossier des photos', default_photo_folder, preferences.photo_folder, self.toasts)
        media.add(self.video_folder)
        media.add(self.photo_folder)

        names = Adw.PreferencesGroup(title='Noms des caméras',
                                     description=f'Ces noms apparaissent dans {APP_NAME} et dans les noms des fichiers.')
        page.add(names)
        for camera in cameras:
            row = Adw.EntryRow(title=camera.name, use_markup=False)
            row.set_text(preferences.camera_names.get(camera.identifier, ''))
            row.get_delegate().set_max_length(70)
            names.add(row)
            self.entries[camera.identifier] = (camera.name, row)

    def apply(self, *_):
        names = self.preferences.camera_names.copy()
        for identifier, (default_name, row) in self.entries.items():
            name = row.get_text().strip()
            if name and name != default_name:
                names[identifier] = name
            else:
                names.pop(identifier, None)
        try:
            preferences = Preferences(recording_folder=self.video_folder.folder, camera_names=names,
                                      photo_folder=self.photo_folder.folder,
                                      motion_notifications=self.motion_notifications.get_active())
            self.save(preferences, autostart=self.autostart.get_active())
        except (OSError, UnicodeError):
            self.toasts.add_toast(Adw.Toast.new('Impossible de sauvegarder les réglages.'))
        else:
            self.close()
