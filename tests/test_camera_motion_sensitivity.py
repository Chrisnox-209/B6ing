import unittest
from unittest.mock import AsyncMock

from cinnado_live.engine import Account, Camera, CameraError, CameraOptions


class CameraMotionSensitivityTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.gateway = 'https://test.ajcloud.net/api'
        self.camera = Camera('Jardin', {'deviceId': 'demo', 'deviceType': '4',
                                      'context': {'config': {'appGatewayUrl': self.gateway}}})
        self.account = Account()
        self.account.cameras = (self.camera,)
        self.account._client = AsyncMock()

    async def test_fetch_includes_the_camera_type_needed_for_motion_settings(self):
        self.account._client.post.return_value = {'infos': [
            {'did': 'demo', 'info': {'motionDetectConfig': {'susceptiveness': '3'}}},
        ]}
        options = await self.account.read_camera_options(self.camera)
        self.assertEqual(options.motion_sensitivity, 3)
        self.account._client.post.assert_awaited_once_with(
            '/v1/fetch-infos', {'devices': [{'did': 'demo', 'scopes': [], 'isShare': False, 'deviceType': 4}]},
            signed=True, base=self.gateway,
        )

    async def test_change_preserves_other_motion_parameters_and_reads_back_the_level(self):
        current = {'level': 'Normal', 'susceptiveness': '3', 'pirDetectRange': '2', 'retriggerInterval': '20',
                   'pirLingerDuration': '0', 'aovWorkInterval': '60', 'aovWorkMinBattery': '30'}
        expected = {**current, 'level': 'Custom', 'susceptiveness': '2'}
        self.account._camera_info = AsyncMock(side_effect=(
            {'motionDetectConfig': current, 'detectionsConfig': {'share': {'areas': ['zone']}}},
            {'motionDetectConfig': expected},
        ))

        result = await self.account.set_motion_sensitivity(self.camera, 2)

        self.assertEqual(result.motion_sensitivity, 2)
        self.assertEqual(current['susceptiveness'], '3')
        self.assertEqual(self.account._camera_info.await_count, 2)
        self.account._client.post.assert_awaited_once_with(
            '/v1/motion-detect-config', expected, signed=True, base=self.gateway, device_id='demo',
        )

    async def test_invalid_missing_and_unchanged_levels_do_not_write(self):
        self.assertIsNone(CameraOptions.from_info({'motionDetectConfig': {'susceptiveness': '6'}}).motion_sensitivity)
        self.account._camera_info = AsyncMock(side_effect=({}, {'motionDetectConfig': {'susceptiveness': '3'}}))
        with self.assertRaises(CameraError):
            await self.account.set_motion_sensitivity(self.camera, 0)
        with self.assertRaises(CameraError):
            await self.account.set_motion_sensitivity(self.camera, 3)
        self.assertEqual((await self.account.set_motion_sensitivity(self.camera, 3)).motion_sensitivity, 3)
        self.account._client.post.assert_not_awaited()
