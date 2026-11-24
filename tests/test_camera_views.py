from pathlib import Path
import struct
from tempfile import TemporaryDirectory
import unittest

from cinnado_live.preferences import GridLayout, Preferences
from cinnado_live.vendor_r2.camera_protocol import ptz_goto_payload, ptz_position_from_response


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

    def test_position_reading_and_recall_serialize_the_same_coordinates(self):
        position = ptz_position_from_response(struct.pack('<ii', -13, 36123))
        self.assertEqual(ptz_goto_payload(position), struct.pack('<B3xHH8x', 112, 65523, 36123))
        self.assertIsNone(ptz_position_from_response(bytes(4)))
        self.assertIsNone(ptz_position_from_response(struct.pack('<ii', 1 << 30, 0)))
