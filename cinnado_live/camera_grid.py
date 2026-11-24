"""Adjustable camera grid with locally saved layouts."""

from dataclasses import replace

from gi.repository import Adw, Gdk, Gtk, Pango

from .preferences import GridLayout


def name_prompt(parent, title, detail, accept):
    dialog = Adw.AlertDialog(heading=title, body=detail)
    entry = Gtk.Entry(placeholder_text='Nom', max_length=40, activates_default=True)
    entry.update_property([Gtk.AccessibleProperty.LABEL], ['Nom'])
    dialog.set_extra_child(entry)
    dialog.add_response('cancel', 'Annuler')
    dialog.add_response('save', 'Enregistrer')
    dialog.set_response_appearance('save', Adw.ResponseAppearance.SUGGESTED)
    dialog.set_default_response('save')
    dialog.set_close_response('cancel')
    dialog.set_response_enabled('save', False)
    entry.connect('changed', lambda edit: dialog.set_response_enabled('save', bool(edit.get_text().strip())))
    dialog.connect('response::save', lambda *_: accept(entry.get_text().strip()))
    dialog.present(parent)


def icon_button(icon, label):
    button = Gtk.Button(icon_name=icon, tooltip_text=label, css_classes=['control-icon'])
    button.update_property([Gtk.AccessibleProperty.LABEL], [label])
    return button


class CameraGrid(Gtk.Box):
    def __init__(self, preferences, save):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.layout = preferences.camera_grid
        self.views = preferences.camera_views
        self.save = save
        self.tiles = {}
        self.editors = {}

        toolbar = Gtk.Box(spacing=8, css_classes=['camera-grid-toolbar'])
        self.append(toolbar)
        self.view_buttons = Gtk.Box(spacing=5)
        strip = Gtk.ScrolledWindow(hexpand=True, vscrollbar_policy=Gtk.PolicyType.NEVER,
                                   hscrollbar_policy=Gtk.PolicyType.EXTERNAL, child=self.view_buttons)
        toolbar.append(strip)
        self.edit = Gtk.ToggleButton(label='Modifier la grille', tooltip_text='Organiser et agrandir les caméras')
        self.edit.connect('toggled', self.refresh_editors)
        toolbar.append(self.edit)
        save_view = icon_button('document-save-symbolic', 'Mémoriser la mise en page actuelle')
        save_view.connect('clicked', self.save_view)
        toolbar.append(save_view)
        self.manage = Gtk.MenuButton(icon_name='view-more-symbolic', tooltip_text='Gérer les mises en page',
                                    css_classes=['control-icon'])
        toolbar.append(self.manage)

        self.grid = Gtk.Grid(column_spacing=14, row_spacing=14, column_homogeneous=True,
                             margin_start=18, margin_end=18, margin_top=14, margin_bottom=18)
        scrolled = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER, vexpand=True, child=self.grid)
        self.append(scrolled)
        self.refresh_views()

    def add(self, tile):
        key = tile.camera.identifier
        self.tiles[key] = tile
        editor = tile.layout_controls
        grip = Gtk.Image(icon_name='list-drag-handle-symbolic', pixel_size=18,
                         tooltip_text='Glisser pour déplacer la caméra', css_classes=['camera-drag-handle'])
        grip.set_cursor_from_name('grab')
        editor.append(grip)
        drag = Gtk.DragSource(actions=Gdk.DragAction.MOVE)
        drag.connect('prepare', lambda *_: Gdk.ContentProvider.new_for_value(key))
        grip.add_controller(drag)
        target = Gtk.DropTarget.new(str, Gdk.DragAction.MOVE)
        target.connect('drop', lambda _target, source, *_: self.drop(source, key))
        tile.add_controller(target)
        up = icon_button('go-up-symbolic', 'Placer cette caméra avant')
        down = icon_button('go-down-symbolic', 'Placer cette caméra après')
        up.connect('clicked', lambda *_: self.step(key, -1))
        down.connect('clicked', lambda *_: self.step(key, 1))
        editor.append(up)
        editor.append(down)
        size = Gtk.Button()
        size.connect('clicked', lambda *_: self.change_size(key))
        editor.append(size)
        self.editors[key] = up, down, size
        self.grid.attach(tile, 0, len(self.tiles) - 1, 1, 1)

    def clear(self):
        self.edit.set_active(False)
        for tile in self.tiles.values():
            self.grid.remove(tile)
        self.tiles.clear()
        self.editors.clear()

    def arrange(self):
        column = row = 0
        manager = self.grid.get_layout_manager()
        for key in self.layout.arrange(self.tiles):
            large = key in self.layout.large
            tile = self.tiles[key]
            if large and column:
                column = 0
                row += 1
            child = manager.get_layout_child(tile)
            child.set_column(column)
            child.set_row(row)
            child.set_column_span(2 if large else 1)
            tile.video_area.set_size_request(300, 295 if large else 165)
            if large or column:
                column = 0
                row += 1
            else:
                column = 1
        self.refresh_editors()

    def refresh_editors(self, *_):
        editing = self.edit.get_active()
        order = self.layout.arrange(self.tiles)
        for index, key in enumerate(order):
            self.tiles[key].layout_controls.set_visible(editing)
            up, down, size = self.editors[key]
            up.set_sensitive(index > 0)
            down.set_sensitive(index < len(order) - 1)
            size.set_label('Réduire' if key in self.layout.large else 'Agrandir')

    def drop(self, source, target):
        if not self.edit.get_active() or source not in self.tiles or source == target:
            return False
        order = list(self.layout.arrange(self.tiles))
        index = order.index(target)
        order.remove(source)
        order.insert(index, source)
        return self.apply(replace(self.layout, order=tuple(order)), self.views)

    def step(self, key, delta):
        order = self.layout.arrange(self.tiles)
        index = order.index(key) + delta
        if 0 <= index < len(order):
            self.drop(key, order[index])

    def change_size(self, key):
        self.apply(replace(self.layout, large=self.layout.large ^ {key}), self.views)

    def save_view(self, *_):
        def add_view(name):
            label = next((existing for existing in self.views if name.casefold() == existing.casefold()), name)
            current = replace(self.layout, order=self.layout.arrange(self.tiles))
            self.apply(current, {**self.views, label: current})

        name_prompt(self, 'Mémoriser la mise en page', 'Un nom existant met à jour la vue correspondante.', add_view)

    def remove_view(self, label):
        self.apply(self.layout, {name: view for name, view in self.views.items() if name != label})

    def apply(self, layout, views):
        if not self.save(layout, views):
            return False
        self.layout, self.views = layout, views
        self.arrange()
        self.refresh_views()
        return True

    def refresh_views(self):
        while child := self.view_buttons.get_first_child():
            self.view_buttons.remove(child)
        default = GridLayout()
        active = next((name for name, layout in self.views.items() if layout == self.layout), None)
        for label, layout in [('Grille', default), *self.views.items()]:
            text = Gtk.Label(label=label, ellipsize=Pango.EllipsizeMode.END, max_width_chars=20)
            button = Gtk.Button(child=text, tooltip_text=label, css_classes=['flat', 'camera-view-button'])
            if (active == label and layout == self.layout) or (active is None and layout == self.layout):
                button.add_css_class('active-view')
            button.connect('clicked', lambda _, value=layout: self.apply(value, self.views))
            self.view_buttons.append(button)
        menu = Gtk.Popover()
        rows = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=5)
        for label in self.views:
            row = Gtk.Box(spacing=8)
            row.append(Gtk.Label(label=label, xalign=0, hexpand=True, ellipsize=Pango.EllipsizeMode.END,
                                 max_width_chars=24))
            remove = icon_button('user-trash-symbolic', f'Supprimer la mise en page « {label} »')
            remove.connect('clicked', lambda _, name=label: self.remove_view(name))
            row.append(remove)
            rows.append(row)
        menu.set_child(rows)
        self.manage.set_popover(menu)
        self.manage.set_visible(bool(self.views))
