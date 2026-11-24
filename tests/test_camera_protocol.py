import unittest

from cinnado_live.vendor_r2.camera_protocol import ControlStream, TalkFormat, ptz_datagram, rc4


class ControlStreamTests(unittest.TestCase):
    def test_reassembles_control_data_across_sequence_rollover(self):
        stream = ControlStream()
        stream.next_sequence = 65534
        payload = b'control fixture'
        header = bytearray(48)
        header[:4] = b'HH01'
        header[6:8] = (257).to_bytes(2, 'little')
        header[8:12] = len(payload).to_bytes(4, 'little')

        self.assertEqual(stream.feed(stream.absolute_sequence(65534), header[:24]), [])
        self.assertEqual(stream.feed(stream.absolute_sequence(0), payload), [])
        self.assertEqual(stream.feed(stream.absolute_sequence(65535), header[24:]), [(257, payload)])
        self.assertEqual(stream.next_sequence, 65537)
        self.assertEqual(stream.feed(stream.absolute_sequence(65535), header[24:]), [])

    def test_ptz_packets_match_continuous_motion_and_explicit_stop(self):
        for direction, command in (('up', 1), ('down', 2), ('left', 3), ('right', 4), (None, 0)):
            with self.subTest(direction=direction):
                packet = ptz_datagram('fixture', direction, 9)
                self.assertEqual(packet[6:8], b'\x00\x09')
                self.assertEqual(packet[14:16], b'\x10\x01')
                self.assertEqual(rc4(b'fixture', packet[56:]), bytes((command, 1)) + bytes(14))

    def test_talk_requires_accepted_format_and_encodes_audio_header(self):
        response = b'\x00\x10\x01\x00\x02\x01\x40\x1f'
        format = TalkFormat.from_response(response)
        self.assertEqual(format, TalkFormat(0x102, 8000))
        self.assertIsNone(TalkFormat.from_response(b'\x01' + response[1:]))
        self.assertIsNone(TalkFormat.from_response(response[:7]))
        packet = format.datagram(b'\xd5' * 640, 7, 123)
        self.assertEqual(packet[6:14], b'\x00\x07HH02\x7f\x00')
        self.assertEqual(packet[14:24], b'\x02\x01\x40\x1f\x10\x01\x80\x02\x00\x00')
        self.assertEqual(int.from_bytes(packet[24:32], 'little'), 123)
        self.assertEqual(packet[56:], b'\xd5' * 640)
