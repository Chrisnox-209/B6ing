"""Direct controls for the B6 light, siren, and night vision."""

from gi.repository import Gtk


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
