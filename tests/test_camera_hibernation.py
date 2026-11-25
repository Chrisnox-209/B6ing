from copy import deepcopy
import unittest
from unittest.mock import AsyncMock

from cinnado_live.engine import Account, Camera, CameraError, CameraOptions


def b6_info(enabled='0'):
    return {
        'capability': {'privateBase': '1', 'privateTimedHibernate': '0', 'hibernate': '0'},
        'hibernateConfig': {'enable': enabled},
        'autoHibernateConfig': {
            'enable': enabled,
            'timePolicies': [
                {'no': '1', 'enable': '1', 'startTime': '000000', 'endTime': '235959', 'weekDays': list(range(1, 8))},
                {'no': '2', 'enable': '0', 'startTime': '120000', 'endTime': '130000', 'weekDays': [1]},
            ],
        },
    }


class CameraHibernationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.gateway = 'https://test.ajcloud.net/api'
        self.camera = Camera('Jardin', {'deviceId': 'demo', 'context': {'config': {'appGatewayUrl': self.gateway}}})
        self.account = Account()
        self.account.cameras = (self.camera,)
        self.account._client = AsyncMock()

    def test_reads_privacy_hibernation_on_b6_with_legacy_capability_disabled(self):
        self.assertIs(CameraOptions.from_info(b6_info('1')).hibernating, True)
        self.assertIs(CameraOptions.from_info(b6_info('0')).hibernating, False)
        self.assertIsNone(CameraOptions.from_info({'hibernateConfig': {'enable': '0'}}).hibernating)

    async def test_uses_permanent_privacy_policy_and_checks_readback(self):
        for target in (True, False):
            with self.subTest(target=target):
                initial = b6_info('0' if target else '1')
                original = deepcopy(initial)
                self.account._camera_info = AsyncMock(side_effect=(initial, b6_info(str(int(target)))))
                self.account._client.post.reset_mock()

                result = await self.account.set_hibernation(self.camera, target)

                self.assertIs(result.hibernating, target)
                self.assertEqual(self.account._camera_info.await_count, 2)
                self.assertEqual(initial, original)
                expected = original['autoHibernateConfig']['timePolicies']
                if target:
                    for policy in expected:
                        policy['enable'] = int(policy['no'] == '1')
                self.account._client.post.assert_awaited_once_with(
                    '/v1/auto-hibernate-config', {'enable': str(int(target)), 'timePolicies': expected},
                    signed=True, base=self.gateway, device_id=self.camera.identifier,
                )

    async def test_unknown_state_and_already_set_state_do_not_write(self):
        self.account._camera_info = AsyncMock(side_effect=({}, b6_info('0')))
        with self.assertRaises(CameraError):
            await self.account.set_hibernation(self.camera, True)
        self.assertIs((await self.account.set_hibernation(self.camera, False)).hibernating, False)
        self.account._client.post.assert_not_awaited()
