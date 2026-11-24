from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from cinnado_live.alerts import AlertEntry, AlertJournal, MAX_ALERTS
from cinnado_live.engine import _alert_video_format, _motion_alerts


class AlertJournalTests(unittest.TestCase):
    def setUp(self):
        self.directory = Path(self.enterContext(TemporaryDirectory()))
        self.journal = AlertJournal.for_account(self.directory, 'test@example.com')
        self.occurred_at = datetime(2026, 1, 2, 9, 10, tzinfo=timezone.utc)

    def test_reload_keeps_latest_first_and_read_status(self):
        older = AlertEntry('camera-a', 'event', 'Entrée', self.occurred_at)
        newer = AlertEntry('camera-b', 'event', 'Jardin', self.occurred_at + timedelta(minutes=1))
        self.assertTrue(self.journal.add(newer))
        self.assertTrue(self.journal.add(older))
        self.assertFalse(self.journal.add(newer))
        self.assertEqual(self.journal.unread_count, 2)

        self.journal.mark_read()
        reopened = AlertJournal.for_account(self.directory, ' TEST@EXAMPLE.COM ')
        self.assertEqual([entry.key for entry in reopened.entries], [newer.key, older.key])
        self.assertEqual(reopened.unread_count, 0)

    def test_single_and_bulk_removal_are_persisted_only_for_this_account(self):
        other = AlertJournal.for_account(self.directory, 'other@example.com')
        entrance = AlertEntry('camera-a', 'event', 'Entrée', self.occurred_at)
        garden = AlertEntry('camera-b', 'event', 'Jardin', self.occurred_at)
        self.journal.add(entrance)
        self.journal.add(garden)
        other.add(entrance)

        self.journal.remove(entrance.key)
        self.assertEqual(AlertJournal(self.journal.path).entries, (garden,))
        self.journal.clear()
        self.assertEqual(AlertJournal(self.journal.path).entries, ())
        self.assertEqual(AlertJournal(other.path).entries, (entrance,))

    def test_journal_keeps_at_most_the_last_200_entries(self):
        for index in range(MAX_ALERTS + 1):
            self.journal.add(AlertEntry('camera-a', str(index), 'Entrée',
                                        self.occurred_at + timedelta(seconds=index)))
        reopened = AlertJournal(self.journal.path)
        self.assertEqual(len(reopened.entries), MAX_ALERTS)
        self.assertEqual(reopened.entries[-1].identifier, '1')

    def test_import_marks_older_alerts_read_and_does_not_reintroduce_deleted_entries(self):
        older = AlertEntry('camera-a', 'old', 'Entrée', self.occurred_at)
        self.journal.import_history('camera-a', [older])
        self.assertEqual(self.journal.unread_count, 0)
        self.assertEqual(self.journal.entries[0].key, older.key)

        self.journal.clear(('camera-a', 'camera-b'))
        reopened = AlertJournal(self.journal.path)
        reopened.import_history('camera-a', [older])
        reopened.import_history('camera-b', [AlertEntry('camera-b', 'old', 'Jardin', self.occurred_at)])
        self.assertEqual(reopened.entries, ())


class AlertClipTests(unittest.TestCase):
    def test_motion_alert_video_is_bound_to_its_camera_and_a_known_https_host(self):
        trusted = 'https://media.example.myqcloud.com/alert.ts?signature=example'
        row = {'did': 'camera-a', 'category': 50, 'type': 5010, '_id': 'fixture', 'cts': 1767345000000}
        foreign = {'tags': ['video'], 'url': 'https://myqcloud.com.example.org/foreign.ts'}
        userinfo = {'tags': ['video'], 'url': 'https://other@media.example.myqcloud.com/alert.ts'}
        valid = {'tags': ['video'], 'url': trusted}
        alert, = _motion_alerts({'alarms': [dict(row, avs=[foreign, userinfo, valid]),
                                          dict(row, did='camera-b', avs=[valid])]}, 'camera-a')
        self.assertEqual(alert.clip_url, trusted)
        self.assertNotIn(trusted, repr(alert))

        alert, = _motion_alerts({'alarms': [dict(row, avs=[foreign, userinfo])]}, 'camera-a')
        self.assertIsNone(alert.clip_url)

    def test_clip_file_requires_a_video_container(self):
        self.assertEqual(_alert_video_format(b'\x00\x00\x00\x20ftypisom'), 'mp4')
        self.assertEqual(_alert_video_format((b'\x47' + bytes(187)) * 3), 'ts')
        self.assertIsNone(_alert_video_format(b'#EXTM3U\nhttp://example.org'))

    def test_preview_prefers_a_trusted_thumbnail_and_falls_back_to_the_alert_image(self):
        row = {'did': 'camera-a', 'category': 50, 'type': 5010, '_id': 'fixture', 'cts': 1767345000000}
        picture = {'url': 'https://media.example.myqcloud.com/event.jpg'}
        thumbnail = {'tags': 'thumb', 'url': 'https://media.example.myqcloud.com/small.jpg'}
        invalid = {'tags': 'thumb', 'url': 'https://media.example.com/small.jpg'}
        alert, = _motion_alerts({'alarms': [dict(row, images=[picture, invalid, thumbnail])]}, 'camera-a')
        self.assertEqual(alert.preview_url, thumbnail['url'])
        alert, = _motion_alerts({'alarms': [dict(row, images=[picture, invalid])]}, 'camera-a')
        self.assertEqual(alert.preview_url, picture['url'])
