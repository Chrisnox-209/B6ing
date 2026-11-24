"""Play authenticated streams from Cinnado B6 cameras on the user account."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import re
import socket
from urllib.parse import urlsplit

import aiohttp

from .vendor_r2.cloud import Client
from .vendor_r2.session import CameraAuthenticationError, RelayTimeout, run_session

MOTION_POLL_SECONDS = 20
MAX_ALERT_CLIP_BYTES = 32 * 1024 * 1024
MAX_ALERT_PREVIEW_BYTES = 1024 * 1024
ALERT_MEDIA_DOMAINS = ('ajcloud.net', 'myqcloud.com')


class CameraError(RuntimeError):
    """A message which is safe to display in the app."""


@dataclass(frozen=True)
class Camera:
    name: str
    data: dict = field(repr=False)

    @property
    def identifier(self) -> str:
        return self.data['deviceId']

    @property
    def connection_config(self) -> dict:
        return _record(_record(self.data.get('context')).get('config'))


VISION_PROFILES = {
    'auto_ir': ('0', '0'),
    'ir_led_off': ('0', '1'),
    'day': ('2', '1'),
}


def _measure(value, minimum: int, maximum: int) -> int | None:
    try:
        number = int(str(value))
    except ValueError:
        return None
    return number if minimum <= number <= maximum else None


def _reported_at(value) -> datetime | None:
    try:
        milliseconds = int(str(value))
        return datetime.fromtimestamp(milliseconds / 1000, timezone.utc) if milliseconds > 0 else None
    except (ValueError, OverflowError, OSError):
        return None


@dataclass(frozen=True)
class BatteryReading:
    percent: int
    reported_at: datetime | None

    @classmethod
    def from_info(cls, info: dict) -> BatteryReading | None:
        base = _record(info.get('base'))
        percent = _measure(base.get('batteryCapacity'), 0, 100)
        if percent is None:
            return None
        return cls(percent, _reported_at(base.get('batteryCapacityTs')))


@dataclass(frozen=True)
class WifiReading:
    percent: int | None
    dbm: int | None
    percent_at: datetime | None
    dbm_at: datetime | None

    @property
    def reported_at(self) -> datetime | None:
        return self.percent_at if self.percent is not None else self.dbm_at

    @classmethod
    def from_info(cls, info: dict) -> WifiReading | None:
        base = _record(info.get('base'))
        network = _record(info.get('networkConfig'))

        def signal(name, minimum, maximum):
            value = _measure(base.get(name), minimum, maximum)
            if value is not None:
                return value, _reported_at(base.get(f'{name}Ts'))
            return _measure(network.get(name), minimum, maximum), None

        percent, percent_at = signal('wifiSignal', 0, 100)
        dbm, dbm_at = signal('wifiRssi', -120, -1)
        if percent is None and dbm is None:
            return None
        return cls(percent, dbm, percent_at, dbm_at)


@dataclass(frozen=True)
class MotionAlert:
    identifier: str
    occurred_at: datetime
    clip_url: str | None = field(default=None, repr=False, compare=False)
    preview_url: str | None = field(default=None, repr=False, compare=False)


def _alert_media_url(url) -> str | None:
    if not isinstance(url, str) or not 1 <= len(url) <= 8192:
        return None
    try:
        parsed = urlsplit(url)
        host = parsed.hostname or ''
        recognized = any(host == domain or host.endswith('.' + domain) for domain in ALERT_MEDIA_DOMAINS)
        if (recognized and parsed.scheme == 'https' and parsed.netloc in (host, f'{host}:443')
                and parsed.path.startswith('/') and not parsed.fragment):
            return url
    except ValueError:
        pass
    return None


def _alert_asset_urls(row: dict, field: str):
    assets = row.get(field)
    if not isinstance(assets, list):
        return
    for asset in assets:
        if isinstance(asset, dict) and (url := _alert_media_url(asset.get('url'))):
            yield str(asset.get('tags', '')).casefold(), url


def _alert_video_format(data: bytes) -> str | None:
    if data[4:8] == b'ftyp':
        return 'mp4'
    if len(data) >= 377 and data[0] == data[188] == data[376] == 0x47:
        return 'ts'
    return None


def _motion_alerts(response, camera_id: str) -> list[MotionAlert]:
    rows = _record(response).get('alarms')
    if not isinstance(rows, list):
        raise CameraError('AJCloud n’a pas fourni de liste d’alertes.')
    alerts = []
    for item in rows:
        row = _record(item)
        if row.get('did') != camera_id or str(row.get('category')) != '50' or str(row.get('type')) != '5010':
            continue
        identifier = row.get('_id')
        if not isinstance(identifier, str) or not re.fullmatch(r'[A-Za-z0-9_:-]{1,128}', identifier):
            continue
        occurred_at = _reported_at(row.get('tsStart')) or _reported_at(row.get('cts'))
        if occurred_at is not None:
            clip = next((url for tags, url in _alert_asset_urls(row, 'avs') if 'video' in tags), None)
            images = list(_alert_asset_urls(row, 'images'))
            preview = next((url for tags, url in images if 'thumb' in tags), images[0][1] if images else None)
            alerts.append(MotionAlert(identifier, occurred_at, clip, preview))
    return sorted(alerts, key=lambda item: item.occurred_at)


@dataclass(frozen=True)
class CameraOptions:
    floodlight: bool
    siren: bool
    night_vision: bool
    vision_mode: str | None
    battery: BatteryReading | None = None
    wifi: WifiReading | None = None

    @classmethod
    def from_info(cls, info: dict) -> CameraOptions:
        capabilities = _record(info.get('capability'))
        vision = _record(info.get('nightVision2Config'))
        profile = str(vision.get('nightMode')), str(vision.get('ledMode'))
        return cls(
            floodlight=capabilities.get('floodlight') in (1, '1'),
            siren=capabilities.get('siren') in (1, '1'),
            night_vision=capabilities.get('nightVision2') in (1, '1'),
            vision_mode=next((mode for mode, values in VISION_PROFILES.items() if values == profile), None),
            battery=BatteryReading.from_info(info),
            wifi=WifiReading.from_info(info),
        )


def _record(value) -> dict:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return {}
    return value if isinstance(value, dict) else {}


def _endpoint(value) -> str:
    if not isinstance(value, str):
        raise CameraError('Le service ne fournit pas de passerelle pour cette caméra.')
    try:
        url = urlsplit(value)
        host = url.hostname or ''
        valid = (url.scheme == 'https' and host.endswith('.ajcloud.net')
                 and url.port in (None, 443) and not (url.username or url.password or url.query or url.fragment)
                 and url.path.rstrip('/') in ('', '/api') and url.netloc in (host, f'{host}:443'))
    except ValueError:
        valid = False
    if not valid:
        raise CameraError('La caméra a indiqué une passerelle AJCloud non reconnue.')
    return f'https://{host}/api'


def list_b6(response: dict) -> tuple[Camera, ...]:
    aliases = {entry.get('deviceId'): entry.get('aliasName')
               for entry in response.get('conDevices', ()) if isinstance(entry, dict)}
    found = {}
    for device in response.get('devGenerals', ()):
        if not isinstance(device, dict) or device.get('prodName') != 'B6':
            continue
        did = device.get('deviceId')
        if not isinstance(did, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,59}', did) or did in found:
            continue
        alias = aliases.get(did)
        title = alias.strip()[:70] if isinstance(alias, str) and alias.strip() and alias.strip() != did else f'Caméra {len(found) + 1}'
        found[did] = Camera(title, device)
    return tuple(found.values())


class Account:
    def __init__(self):
        self._session: aiohttp.ClientSession | None = None
        self._client: Client | None = None
        self.cameras: tuple[Camera, ...] = ()

    async def login(self, email: str, password: str) -> tuple[Camera, ...]:
        await self.close()
        session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=20, connect=8, sock_read=12),
            connector=aiohttp.TCPConnector(family=socket.AF_INET),
            trust_env=False,
        )
        client = Client(session)
        try:
            await client.login(email, password)
            response = await client.post('/v3/device-list', {}, signed=True)
            cameras = list_b6(response)
            if not cameras:
                raise CameraError('Ce compte ne retourne aucune caméra Cinnado B6.')
        except (TimeoutError, aiohttp.ClientError) as exc:
            await session.close()
            raise CameraError('Impossible de joindre AJCloud. Vérifie la connexion Internet.') from exc
        except CameraError:
            await session.close()
            raise
        except (KeyError, ValueError, TypeError, RuntimeError) as exc:
            await session.close()
            raise CameraError('Connexion refusée ou réponse inattendue d’AJCloud. Vérifie ton compte Wansview Cloud.') from exc
        except BaseException:
            await session.close()
            raise
        self._session, self._client = session, client
        self.cameras = cameras
        return cameras

    async def close(self) -> None:
        self.cameras = ()
        self._client = None
        if self._session:
            session, self._session = self._session, None
            await session.close()

    def _client_for(self, camera: Camera) -> Client:
        if self._client is None or not any(item is camera for item in self.cameras):
            raise CameraError('Sélectionne une caméra B6 de ton compte.')
        return self._client

    @staticmethod
    async def _camera_info(client: Client, camera: Camera, gateway: str) -> dict:
        response = await client.post('/v1/fetch-infos', {
            'devices': [{'did': camera.identifier, 'scopes': [], 'isShare': False}],
        }, signed=True, base=gateway)
        infos = response.get('infos')
        if not isinstance(infos, list) or len(infos) != 1:
            raise CameraError('AJCloud ne donne pas accès à cette caméra.')
        entry = _record(infos[0])
        if entry.get('did') not in (None, camera.identifier) or entry.get('deviceId') not in (None, camera.identifier):
            raise CameraError('La réponse AJCloud concerne un autre appareil.')
        return _record(entry.get('info'))

    async def read_camera_options(self, camera: Camera) -> CameraOptions:
        client = self._client_for(camera)
        gateway = _endpoint(camera.connection_config.get('appGatewayUrl'))
        try:
            return CameraOptions.from_info(await self._camera_info(client, camera, gateway))
        except CameraError:
            raise
        except (aiohttp.ClientError, TimeoutError, AttributeError, ValueError, TypeError, RuntimeError) as exc:
            raise CameraError('Impossible de lire les commandes de cette caméra. Réessaie dans un instant.') from exc

    async def _motion_access_key(self, client: Client, camera: Camera, gateway: str) -> str:
        info = await self._camera_info(client, camera, gateway)
        access_key = _record(info.get('base')).get('accessKey')
        if not isinstance(access_key, str) or not 1 <= len(access_key.encode()) <= 256:
            raise CameraError('AJCloud ne fournit pas l’accès aux alertes de cette caméra.')
        return access_key

    @staticmethod
    async def _read_motion_alerts(
        client: Client, camera: Camera, emc: str, access_key: str, *, before: datetime | None = None,
    ) -> list[MotionAlert]:
        device_type = str(camera.data.get('deviceType'))
        route = f'/v1/{device_type}/alarms-list-ftr' if device_type in ('5', '6') else '/v1/alarms-list-ftr'
        cursor = str(int(before.timestamp() * 1000)) if before is not None else '0'
        response = await client.post(route, {'deviceId': camera.identifier, 'accessKey': access_key, 'cts': cursor},
                                     signed=True, base=emc, device_id=camera.identifier)
        return _motion_alerts(response, camera.identifier)

    async def _find_motion_alerts(
        self, camera: Camera, entries: dict[str, datetime],
    ) -> tuple[Client, dict[str, MotionAlert]]:
        client = self._client_for(camera)
        config = camera.connection_config
        gateway = _endpoint(config.get('appGatewayUrl'))
        emc = _endpoint(config.get('appEmcUrl'))
        try:
            access_key = await self._motion_access_key(client, camera, gateway)
            alerts = await self._read_motion_alerts(client, camera, emc, access_key)
            found = {item.identifier: item for item in alerts if item.identifier in entries}
            for identifier, occurred_at in sorted(entries.items(), key=lambda item: item[1], reverse=True):
                if identifier not in found:
                    # The service paginates by creation time, which can follow the start of the event.
                    before = occurred_at + timedelta(minutes=1)
                    page = await self._read_motion_alerts(client, camera, emc, access_key, before=before)
                    found.update((item.identifier, item) for item in page if item.identifier in entries)
        except CameraError:
            raise
        except (aiohttp.ClientError, TimeoutError, ValueError, TypeError, RuntimeError) as exc:
            raise CameraError('Impossible de retrouver l’alerte sur AJCloud. Réessaie dans un instant.') from exc
        if self._client is not client:
            raise CameraError('La session Wansview a été fermée.')
        return client, found

    async def read_alert_previews(self, camera: Camera, entries: dict[str, datetime]) -> dict[str, bytes]:
        client, found = await self._find_motion_alerts(camera, entries)
        limit = asyncio.Semaphore(4)
        timeout = aiohttp.ClientTimeout(total=8)

        async def read_image(alert):
            async with limit:
                try:
                    async with client.session.get(alert.preview_url, timeout=timeout, allow_redirects=False) as response:
                        if response.status != 200 or (response.content_length or 0) > MAX_ALERT_PREVIEW_BYTES:
                            return b''
                        try:
                            await response.content.readexactly(MAX_ALERT_PREVIEW_BYTES + 1)
                        except asyncio.IncompleteReadError as exc:
                            if exc.partial.startswith((b'\xff\xd8\xff', b'\x89PNG\r\n\x1a\n')):
                                return exc.partial
                except (aiohttp.ClientError, TimeoutError):
                    pass
                return b''

        alerts = [alert for alert in found.values() if alert.preview_url]
        images = await asyncio.gather(*(read_image(alert) for alert in alerts))
        return {alert.identifier: image for alert, image in zip(alerts, images) if image}

    async def download_alert_clip(
        self, camera: Camera, identifier: str, occurred_at: datetime, destination: Path,
    ) -> str:
        client, found = await self._find_motion_alerts(camera, {identifier: occurred_at})
        alert = found.get(identifier)
        if alert is None or alert.clip_url is None:
            raise CameraError('Ce clip d’alerte n’est plus proposé par AJCloud ou n’a pas de vidéo associée.')

        timeout = aiohttp.ClientTimeout(total=30, connect=8, sock_read=12)
        try:
            async with client.session.get(alert.clip_url, timeout=timeout, allow_redirects=False) as response:
                if response.status != 200:
                    raise CameraError('Le clip d’alerte est indisponible sur AJCloud.')
                if response.content_length is not None and response.content_length > MAX_ALERT_CLIP_BYTES:
                    raise CameraError('Le clip d’alerte est trop volumineux pour cette relecture.')
                header = await response.content.readexactly(512)
                extension = _alert_video_format(header)
                if extension is None:
                    raise CameraError('AJCloud a envoyé un format vidéo incompatible.')
                with destination.open('xb') as output:
                    try:
                        output.write(header)
                        size = len(header)
                        async for chunk in response.content.iter_chunked(65536):
                            size += len(chunk)
                            if size > MAX_ALERT_CLIP_BYTES:
                                raise CameraError('Le clip d’alerte est trop volumineux pour cette relecture.')
                            output.write(chunk)
                    except BaseException:
                        destination.unlink(missing_ok=True)
                        raise
                return extension
        except (aiohttp.ClientError, TimeoutError, asyncio.IncompleteReadError) as exc:
            raise CameraError('Impossible de télécharger le clip d’alerte. Réessaie dans un instant.') from exc

    async def watch_motion(
        self, camera: Camera, notify: Callable[[Camera, MotionAlert], None],
        *, import_history: Callable[[Camera, list[MotionAlert]], None] | None = None,
    ) -> None:
        client = self._client_for(camera)
        config = camera.connection_config
        gateway = _endpoint(config.get('appGatewayUrl'))
        emc = _endpoint(config.get('appEmcUrl'))
        started = datetime.now(timezone.utc)
        access_key = None
        seen = {}
        while self._client is client:
            try:
                if access_key is None:
                    access_key = await self._motion_access_key(client, camera, gateway)
                alerts = await self._read_motion_alerts(client, camera, emc, access_key)
            except (aiohttp.ClientError, TimeoutError, AttributeError, ValueError, TypeError, RuntimeError):
                access_key = None
                await asyncio.sleep(2 * MOTION_POLL_SECONDS)
                continue

            now = datetime.now(timezone.utc)
            if import_history is not None:
                import_history(camera, [alert for alert in alerts if alert.occurred_at < started])
                import_history = None
            cutoff = max(started, now - timedelta(minutes=5))
            seen = {identifier: occurred for identifier, occurred in seen.items() if occurred >= cutoff}
            for alert in alerts:
                if cutoff <= alert.occurred_at <= now and alert.identifier not in seen:
                    seen[alert.identifier] = alert.occurred_at
                    notify(camera, alert)
            await asyncio.sleep(MOTION_POLL_SECONDS)

    async def set_camera_option(self, camera: Camera, option: str, value: bool | str) -> None:
        client = self._client_for(camera)
        if option == 'vision' and isinstance(value, str) and value in VISION_PROFILES:
            route = '/v1/night-vision2-config'
            night, led = VISION_PROFILES[value]
            data = {'nightMode': night, 'ledMode': led}
        elif option in ('floodlight', 'siren') and isinstance(value, bool):
            route = '/v1/floodlight-control' if option == 'floodlight' else '/v1/siren-control'
            data = {'toggle': 'on' if value else 'off'}
            if option == 'floodlight':
                data['brightness'] = 1
        else:
            raise CameraError('Cette commande n’est pas disponible pour la caméra.')

        gateway = _endpoint(camera.connection_config.get('appGatewayUrl'))
        try:
            await client.post(route, data, signed=True, base=gateway, device_id=camera.identifier)
        except (aiohttp.ClientError, TimeoutError, AttributeError, ValueError, TypeError, RuntimeError) as exc:
            raise CameraError('La commande n’a pas été confirmée. Vérifie que la caméra répond et réessaie.') from exc

    async def play(
        self, camera: Camera, stop: asyncio.Event,
        frame: Callable[[bytes, str, int, int], Awaitable[None]], status: Callable[[str], None],
        *, direction: Callable[[int], str | None] | None = None,
        ptz_available: Callable[[bool], None] | None = None,
        quality: Callable[[], int | None] | None = None,
        listening: Callable[[], bool] | None = None,
        audio: Callable[[bytes, int], Awaitable[None]] | None = None,
        requested_talk=None,
    ) -> None:
        client = self._client_for(camera)
        config = camera.connection_config
        gateway = _endpoint(config.get('appGatewayUrl'))
        wakeup = _endpoint(config.get('appKeepAliveUrl'))

        try:
            status('Autorisation de la caméra…')
            info = await self._camera_info(client, camera, gateway)
            key = _record(info.get('base')).get('accessKey')
            if not isinstance(key, str) or not 1 <= len(key.encode()) <= 256:
                raise CameraError('AJCloud ne fournit pas la clé vidéo de cette caméra.')
            if stop.is_set():
                return
            motorized = _record(info.get('capability')).get('ptz') in (1, '1', True)
            if ptz_available is not None:
                ptz_available(motorized)
            wake_request = {
                'deviceId': camera.identifier, 'timeZone': 'Europe/Paris',
                'deviceMode': camera.data['deviceMode'], 'vendorCode': camera.data['vendorCode'],
            }
        except (aiohttp.ClientError, TimeoutError) as exc:
            raise CameraError('AJCloud ne répond pas pendant l’ouverture de la caméra.') from exc
        except CameraError:
            raise
        except (ValueError, KeyError, TypeError, RuntimeError) as exc:
            raise CameraError('AJCloud a refusé l’ouverture ou retourné une réponse incompatible.') from exc
        epoch = 0
        failed_attempts = 0
        received_frame = False
        last_failure = ''

        async def receive(data: bytes, codec: str, fps: int) -> None:
            nonlocal received_frame
            if not received_frame:
                received_frame = True
                status('Flux reçu · décodage…')
            await frame(data, codec, fps, epoch)

        def desired_direction():
            return direction(epoch)

        async def receive_audio(data):
            if listening is not None and listening():
                await audio(data, epoch)

        while not stop.is_set():
            epoch += 1
            received_frame = False
            stats = {}
            try:
                if epoch == 1:
                    status('Réveil de la caméra…')
                await client.post('/v1/wakeup', wake_request, signed=True, base=wakeup, device_id=camera.identifier)
            except (aiohttp.ClientError, TimeoutError):
                message = 'AJCloud ne répond pas pendant le réveil de la caméra.'
            except (KeyError, ValueError, TypeError, RuntimeError) as exc:
                raise CameraError('AJCloud a refusé le réveil ou retourné une réponse incompatible.') from exc
            else:
                if stop.is_set():
                    return
                if epoch == 1:
                    status('Connexion au relais vidéo…')
                try:
                    await run_session(camera.identifier, key, receive, stop, float('inf'), stats,
                                      direction=desired_direction if motorized and direction is not None else None,
                                      quality=quality, listening=listening, on_audio=receive_audio if audio else None,
                                      requested_talk=requested_talk)
                except CameraAuthenticationError as exc:
                    raise CameraError('La B6 a refusé l’authentification vidéo.') from exc
                except RelayTimeout:
                    pass
                except OSError:
                    stats['transport_error'] = True
                except (ValueError, RuntimeError) as exc:
                    raise CameraError('Réponse vidéo incompatible avec le protocole R2.') from exc
                if stop.is_set():
                    return
                if received_frame:
                    message = 'Le relais n’envoie plus de vidéo.'
                elif stats.get('transport_error'):
                    message = 'Erreur de transport lors de la connexion au relais vidéo.'
                elif stats.get('authenticated'):
                    message = 'La caméra est authentifiée mais n’envoie pas de vidéo compatible.'
                elif stats.get('relay_selected'):
                    message = 'La caméra ne répond pas à l’authentification via le relais vidéo.'
                else:
                    message = 'Le relais ne parvient pas à ouvrir une session vidéo avec cette caméra.'

            if stats.get('media_seconds', 0) >= 5:
                failed_attempts = 0
                last_failure = ''
            else:
                failed_attempts += 1
                if failed_attempts == 2:
                    if last_failure and last_failure != message:
                        message = f'{last_failure} {message}'
                    raise CameraError(message)
                last_failure = message
            status('Direct interrompu · reconnexion…' if received_frame else
                   'Nouvelle tentative de connexion…')
            await asyncio.sleep(2)
