from datetime import date, datetime, timezone
import struct
import unittest
from zoneinfo import ZoneInfo

from cinnado_live.vendor_r2 import sd


class SdProtocolTests(unittest.TestCase):
    def test_calendar_decodes_unique_days_and_accepts_an_empty_reply(self):
        entries = b'20261202\0\0' + b'20261122\0\0' * 2
        payload = struct.pack('<BBH8x', 0, 0, 3) + entries
        self.assertEqual(sd.parse_calendar(payload), (date(2026, 11, 22), date(2026, 12, 2)))
        self.assertEqual(sd.parse_calendar(bytes(12)), ())
        with self.assertRaises(sd.InvalidCardResponse):
            sd.parse_calendar(payload[:-1])

    def test_list_request_covers_the_camera_day_on_a_clock_change(self):
        day = date(2026, 10, 25)
        request = sd.list_request(day, ZoneInfo('Europe/Paris'))
        name, start, end = struct.unpack('<16sII8x', request.payload)
        self.assertEqual(request.command, sd.CLIPS)
        self.assertEqual(name.rstrip(b'\0'), b'20261025')
        self.assertEqual(end - start, 25 * 3600)

    def test_list_is_bound_to_the_selected_day_and_checks_packet_length(self):
        day = date(2026, 11, 22)
        group = int(datetime(2026, 11, 22, 7, tzinfo=timezone.utc).timestamp())
        header = struct.pack('<10sBBH6x', b'20261122', 0, 0, 1)
        reply = header + struct.pack('<II', group, 14 << 8 | 30)
        self.assertEqual(sd.parse_clips(reply, day), (sd.SdClip(day, group, 30),))
        self.assertEqual(sd.parse_clips(struct.pack('<10sBBH6x', b'20261122', 0, 0, 0), day), ())
        with self.assertRaises(sd.CardListUnavailable):
            sd.parse_clips(struct.pack('<10sBBH6x', b'20261122', 255, 0, 0), day)
        with self.assertRaises(sd.InvalidCardResponse):
            sd.parse_clips(reply, date(2026, 11, 23))
        with self.assertRaises(sd.InvalidCardResponse):
            sd.parse_clips(reply[:-1], day)

    def test_clip_controls_target_the_same_day_and_group(self):
        clip = sd.SdClip(date(2026, 11, 22), 1795330800, 30)
        request = sd.playback_request(clip)
        self.assertEqual(request.command, sd.PLAY)
        self.assertEqual(request.payload, struct.pack('<10sHIH6x', b'20261122', 1, clip.group_id, 0))
        self.assertEqual(request.teardown, (sd.STOP, struct.pack('<12sI8x', b'20261122', clip.group_id)))
        reply = struct.pack('<BB10sI', 0, 0, b'20261122', clip.group_id)
        self.assertTrue(sd.is_playback_reply(reply, clip))
        self.assertFalse(sd.is_playback_reply(reply, sd.SdClip(clip.day, clip.group_id + 1, 30)))
