"""Per-camera configuration and direct controls."""

from gi.repository import Adw, Gtk

from .camera_grid import icon_button
from .engine import FAVORITE_SLOTS


class CameraConfiguration(Gtk.Box):
    SENSITIVITY = {1: 'Faible', 2: 'Plutôt faible', 3: 'Normale', 4: 'Plutôt élevée', 5: 'Élevée'}

    def __init__(self, toggle_power, apply_sensitivity):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=8, visible=False,
                         css_classes=['camera-configuration'])
        self.saved_sensitivity = None
        header = Gtk.Box(spacing=8)
        header.append(Gtk.Label(label='Configuration de la caméra', xalign=0, hexpand=True, wrap=True,
                                css_classes=['heading']))
        self.power_content = Adw.ButtonContent(icon_name='system-shutdown-symbolic')
        self.power = Gtk.Button(child=self.power_content, css_classes=['control-icon'])
        self.power.connect('clicked', toggle_power)
        header.append(self.power)
        self.append(header)

        self.motion = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=5)
        title = Gtk.Box(spacing=6)
        title.append(Gtk.Label(label='Sensibilité aux mouvements', xalign=0, hexpand=True, wrap=True))
        self.level = Gtk.Label(css_classes=['dim-label'])
        title.append(self.level)
        self.motion.append(title)

        row = Gtk.Box(spacing=8)
        self.scale = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 1, 5, 1)
        self.scale.set_hexpand(True)
        self.scale.set_draw_value(False)
        self.scale.set_round_digits(0)
        self.scale.update_property([Gtk.AccessibleProperty.LABEL], ['Sensibilité aux mouvements'])
        for level in self.SENSITIVITY:
            self.scale.add_mark(level, Gtk.PositionType.BOTTOM, None)
        self.scale.connect('value-changed', self.update_level)
        row.append(self.scale)
        self.apply = Gtk.Button(label='Appliquer', valign=Gtk.Align.CENTER, css_classes=['suggested-action'])
        self.apply.connect('clicked', lambda *_: apply_sensitivity(int(self.scale.get_value())))
        row.append(self.apply)
        self.motion.append(row)
        self.append(self.motion)

    def refresh(self, options, busy):
        power = options.hibernating if options is not None else None
        level = options.motion_sensitivity if options is not None else None
        self.set_visible(power is not None or level is not None)
        self.power.set_visible(power is not None)
        self.power.set_sensitive(not busy)
        label, description = ('Réveiller', 'Réveiller la caméra') if power is True else (
            'Mettre en veille', 'Mettre la caméra en veille',
        )
        self.power_content.set_label(label)
        self.power.set_tooltip_text(description)
        self.power.update_property([Gtk.AccessibleProperty.LABEL], [description])
        if power is True:
            self.power.add_css_class('wake-camera')
        else:
            self.power.remove_css_class('wake-camera')

        self.motion.set_visible(level is not None)
        self.scale.set_sensitive(not busy)
        if level is not None and level != self.saved_sensitivity:
            self.scale.set_value(level)
        self.saved_sensitivity = level
        self.update_level(self.scale)

    def update_level(self, scale):
        level = int(scale.get_value())
        name = self.SENSITIVITY[level]
        self.level.set_label(name)
        scale.update_property([Gtk.AccessibleProperty.VALUE_TEXT], [name])
        self.apply.set_sensitive(self.saved_sensitivity is not None and level != self.saved_sensitivity
                                 and scale.get_sensitive())


class CameraControls(Gtk.Box):
    def __init__(self, apply):
        super().__init__(spacing=8, css_classes=['camera-controls'])
        theme = Gtk.IconTheme.get_for_display(self.get_display())
        light_on = ('torch-enabled-symbolic' if theme.has_icon('torch-enabled-symbolic')
                    else 'display-brightness-symbolic')
        light_off = ('torch-disabled-symbolic' if theme.has_icon('torch-disabled-symbolic')
                     else 'system-shutdown-symbolic')

        self.groups = {}
        self.vision_buttons = {}
        for option, capability, name, actions in (
            ('floodlight', 'floodlight', 'Lumière', (
                (True, light_on, 'Allumer la lumière'),
                (False, light_off, 'Éteindre la lumière'),
            )),
            ('siren', 'siren', 'Sirène', (
                (True, 'preferences-system-notifications-symbolic', 'Déclencher la sirène'),
                (False, 'notifications-disabled-symbolic', 'Arrêter la sirène'),
            )),
            ('vision', 'night_vision', 'Vision jour/nuit', (
                ('day', 'weather-clear-symbolic', 'Jour · couleur'),
                ('auto_ir', 'weather-clear-night-symbolic', 'Nuit automatique · infrarouge'),
                ('ir_led_off', 'night-light-disabled-symbolic', 'Nuit · LED infrarouges éteintes'),
            )),
        ):
            group = self.groups[capability] = Gtk.Box(spacing=1, css_classes=['control-cluster'])
            group.update_property([Gtk.AccessibleProperty.LABEL], [name])
            self.append(group)
            for value, icon, label in actions:
                button = Gtk.Button(icon_name=icon, tooltip_text=label, css_classes=['control-icon'])
                button.update_property([Gtk.AccessibleProperty.LABEL], [label])
                button.connect('clicked', lambda _button, action=option, state=value: apply(action, state))
                group.append(button)
                if option == 'siren' and value:
                    button.add_css_class('destructive-action')
                if option == 'vision':
                    self.vision_buttons[value] = button

    def refresh(self, options, busy):
        for capability, group in self.groups.items():
            available = options is None or getattr(options, capability)
            group.set_sensitive(available and not busy)
            group.set_tooltip_text(None if available else 'Cette caméra ne propose pas cette commande.')
        current = options.vision_mode if options is not None else None
        for mode, button in self.vision_buttons.items():
            if mode == current:
                button.add_css_class('suggested-action')
                button.update_property([Gtk.AccessibleProperty.DESCRIPTION], ['Mode actif'])
            else:
                button.remove_css_class('suggested-action')
                button.reset_property(Gtk.AccessibleProperty.DESCRIPTION)


class FavoriteShortcuts(Gtk.Box):
    def __init__(self, turn):
        super().__init__(spacing=2, visible=False, css_classes=['control-cluster', 'favorite-shortcuts'])
        self.turn = turn
        self.slots = ()

    def refresh(self, slots, busy):
        slots = slots or ()
        self.set_sensitive(not busy)
        if slots == self.slots:
            return
        self.slots = slots
        while child := self.get_first_child():
            self.remove(child)
        self.set_visible(bool(slots))
        if not slots:
            return
        home = icon_button('go-home-symbolic', f'Maison · revenir à la position {slots[0]}')
        home.connect('clicked', lambda *_: self.turn(self.slots[0]))
        self.append(home)
        for slot in slots:
            self.append(Gtk.Separator(orientation=Gtk.Orientation.VERTICAL))
            label = f'Rappeler la position {slot}'
            button = Gtk.Button(label=str(slot), tooltip_text=label, css_classes=['control-icon', 'numeric'])
            button.update_property([Gtk.AccessibleProperty.LABEL], [label])
            button.connect('clicked', lambda _, number=slot: self.turn(number))
            self.append(button)


class FavoriteSettings(Gtk.Box):
    def __init__(self, save, remove):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=6, visible=False)
        self.position_buttons = Gtk.Box(spacing=4)
        self.save_button = Gtk.Button(label='Mémoriser la position actuelle')
        self.save_button.connect('clicked', lambda *_: save())
        self.append(self.save_button)
        self.append(self.position_buttons)
        self.removers = {}
        for slot in FAVORITE_SLOTS:
            button = Gtk.Button(tooltip_text=f'Supprimer la position {slot}', css_classes=['control-icon'])
            button.set_child(Gtk.Label(label=f'{slot} ×'))
            button.update_property([Gtk.AccessibleProperty.LABEL], [f'Supprimer la position {slot}'])
            button.connect('clicked', lambda _, number=slot: remove(number))
            self.position_buttons.append(button)
            self.removers[slot] = button

    def refresh(self, slots, *, live, busy):
        self.set_visible(slots is not None)
        if slots is None:
            return
        self.save_button.set_sensitive(live and not busy and len(slots) < len(FAVORITE_SLOTS))
        if len(slots) == len(FAVORITE_SLOTS):
            reason = 'Les quatre positions sont déjà mémorisées.'
        elif not live:
            reason = 'Ouvre le direct pour mémoriser l’orientation actuelle.'
        else:
            reason = None
        self.save_button.set_tooltip_text(reason)
        self.position_buttons.set_visible(bool(slots))
        for slot, button in self.removers.items():
            button.set_visible(slot in slots)
            button.set_sensitive(not busy)
