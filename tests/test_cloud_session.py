import asyncio
import json
import unittest
from unittest.mock import AsyncMock, MagicMock

from cinnado_live.vendor_r2.cloud import Client, CloudAuthenticationError, _Authorization


class CloudSessionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.session = MagicMock()
        self.response = self.session.post.return_value.__aenter__.return_value
        self.response.status = 200
        self.response.json = AsyncMock(return_value={'status': 'ok', 'result': {'ok': True}})
        self.client = Client(self.session)
        self.renewed = _Authorization('new-token', 'new-key', float('inf'))
        self.client._sign_in = AsyncMock(return_value=_Authorization('old-token', 'old-key', float('inf')))
        await self.client.login('example@example.org', 'example-password')
        self.client._sign_in.reset_mock()
        self.client._sign_in.return_value = self.renewed

    async def test_expired_session_renews_once_for_concurrent_camera_requests(self):
        self.client._authorization = _Authorization('old-token', 'old-key', 0)

        async def sign_in(*_):
            await asyncio.sleep(0)
            return self.renewed

        self.client._sign_in.side_effect = sign_in
        replies = await asyncio.gather(
            self.client.post('/v1/fetch-infos', {}, signed=True),
            self.client.post('/v1/wakeup', {}, signed=True),
            self.client.post('/v1/auto-hibernate-config', {'enable': '0'}, signed=True),
        )

        self.assertEqual(replies, [{'ok': True}] * 3)
        self.client._sign_in.assert_awaited_once()
        for call in self.session.post.call_args_list:
            self.assertEqual(call.kwargs['headers']['Authorization'], 'Bearer new-token')
            self.assertEqual(json.loads(call.kwargs['data'])['meta']['accessToken'], 'new-token')

    async def test_unauthorized_request_renews_and_resends_once(self):
        self.response.status = 401
        accepted = MagicMock(status=200, json=self.response.json)
        self.session.post.return_value.__aenter__.side_effect = [self.response, accepted]

        await self.client.post('/v1/wakeup', {'deviceId': 'demo'}, signed=True)

        self.client._sign_in.assert_awaited_once()
        first, second = [call.kwargs['headers'] for call in self.session.post.call_args_list]
        self.assertEqual(first['Authorization'], 'Bearer old-token')
        self.assertEqual(second['Authorization'], 'Bearer new-token')
        self.assertNotEqual(first['X-UAC-Signature'], second['X-UAC-Signature'])

    async def test_other_errors_do_not_trigger_a_second_camera_command(self):
        self.response.status = 503

        with self.assertRaisesRegex(RuntimeError, '503'):
            await self.client.post('/v1/auto-hibernate-config', {'enable': '0'}, signed=True)

        self.client._sign_in.assert_not_awaited()
        self.session.post.assert_called_once()

    async def test_expired_session_recovers_after_a_failed_renewal(self):
        self.client._authorization = _Authorization('old-token', 'old-key', 0)
        self.client._sign_in.side_effect = [TimeoutError(), self.renewed]

        for _ in range(2):
            with self.assertRaises(CloudAuthenticationError):
                await self.client.post('/v1/wakeup', {}, signed=True)
        self.client._sign_in.assert_awaited_once()
        self.session.post.assert_not_called()

        self.client._retry_at = 0
        self.assertEqual(await self.client.post('/v1/wakeup', {}, signed=True), {'ok': True})

    async def test_command_can_renew_immediately_after_a_status_check_is_cancelled(self):
        self.client._authorization = _Authorization('old-token', 'old-key', 0)
        self.client._sign_in.side_effect = [asyncio.CancelledError(), self.renewed]

        with self.assertRaises(asyncio.CancelledError):
            await self.client.post('/v1/fetch-infos', {}, signed=True)
        self.assertEqual(await self.client.post('/v1/wakeup', {}, signed=True), {'ok': True})

    async def test_logout_during_renewal_does_not_restore_the_session(self):
        self.client._authorization = _Authorization('old-token', 'old-key', 0)

        async def sign_in(*_):
            self.client.clear_authentication()
            return self.renewed

        self.client._sign_in.side_effect = sign_in
        with self.assertRaises(CloudAuthenticationError):
            await self.client.post('/v1/wakeup', {}, signed=True)

        self.assertIsNone(self.client._authorization)
        self.session.post.assert_not_called()
