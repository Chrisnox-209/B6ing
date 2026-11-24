from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from cinnado_live.engine import CameraOptions
from cinnado_live.preferences import GridLayout, Preferences


class CameraViewTests(unittest.TestCase):
    def test_saved_layout_ignores_missing_cameras_and_appends_new_ones(self):
        layout = GridLayout.from_data({'order': ['b', 'b', 'missing', 'a'], 'large': ['b']})
        self.assertEqual(layout.arrange(('a', 'b', 'new')), ('b', 'a', 'new'))
        self.assertEqual(layout.large, {'b'})

    def test_preferences_keep_legacy_fields_and_save_layouts_and_positions(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / 'settings.json'
            path.write_text('{"motion_notifications": false, "camera_names": {"a": "Balcon"}}')
            legacy = Preferences.load(path)
            self.assertFalse(legacy.motion_notifications)
            self.assertEqual(legacy.camera_names, {'a': 'Balcon'})
            self.assertEqual(legacy.camera_grid, GridLayout())
            layout = GridLayout(('b', 'a'), frozenset({'b'}))
            preferences = Preferences(camera_names=legacy.camera_names, motion_notifications=False,
                                      camera_grid=layout, camera_views={'Entrée': layout},
                                      camera_positions={'a': {'Portail': (-13, 123)}})
            preferences.save(path)
            self.assertEqual(Preferences.load(path), preferences)

    def test_camera_favorites_show_only_saved_slots_for_the_primary_lens(self):
        metadata = {
            'capability': {'ptz': '1', 'viewAngles': '1'},
            'viewAnglesConfig': {'viewAngles': [
                {'sensorIdx': '0', 'viewAngle': '4', 'url': 'fixture'},
                {'sensorIdx': '0', 'viewAngle': 3, 'name': 'Placeholder', 'url': ''},
                {'sensorIdx': '1', 'viewAngle': 2, 'url': 'other lens'},
                {'sensorIdx': '0', 'viewAngle': '1', 'url': 'fixture'},
                {'sensorIdx': '0', 'viewAngle': 5, 'url': 'out of range'},
            ]},
        }
        self.assertEqual(CameraOptions.from_info(metadata).favorite_slots, (1, 4))
        self.assertIsNone(CameraOptions.from_info({}).favorite_slots)
