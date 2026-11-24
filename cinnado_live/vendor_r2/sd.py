"""Read-only commands for the camera's priv2 microSD playback protocol."""

import asyncio
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
import struct
from time import monotonic

from .session import RelayTimeout, SessionRequest, run_session

CALENDAR, CLIPS, PLAY, STOP = 337, 339, 343, 345


class InvalidCardResponse(ValueError):
    pass


class CardListUnavailable(Exception):
    pass


@dataclass(frozen=True)
class SdClip:
    day: date
    group_id: int
    duration: int

    @property
    def started(self):
        return datetime.fromtimestamp(self.group_id, timezone.utc)


def _date_code(day):
    return day.strftime('%Y%m%d').encode('ascii')


def _parse_date(raw):
    if len(raw) != 8 or not raw.isdigit():
        raise InvalidCardResponse()
    try:
        return date.fromisoformat(raw.decode('ascii'))
    except ValueError as exc:
        raise InvalidCardResponse() from exc


def _entries(payload, offset, header_size, entry_size):
    if len(payload) < header_size:
        raise InvalidCardResponse()
    count = struct.unpack_from('<H', payload, offset)[0]
    if len(payload) != header_size + count * entry_size:
        raise InvalidCardResponse()
    return count


def parse_calendar(payload):
    _entries(payload, 2, 12, 10)
    if payload[0] != 0:
        raise InvalidCardResponse()
    return tuple(sorted({_parse_date(payload[i:i + 8]) for i in range(12, len(payload), 10)}))


def list_request(day, tz):
    start = datetime.combine(day, time.min, tz)
    end = datetime.combine(day + timedelta(days=1), time.min, tz)
    return SessionRequest(CLIPS, struct.pack('<16sII8x', _date_code(day), int(start.timestamp()),
                                            int(end.timestamp())))


def parse_clips(payload, day):
    count = _entries(payload, 12, 20, 8)
    if _parse_date(payload[:8]) != day:
        raise InvalidCardResponse()
    if payload[10] == 255 and count == 0:
        raise CardListUnavailable()
    if payload[10] != 0:
        raise InvalidCardResponse()
    return tuple(sorted((SdClip(day, group, flags & 0xff)
                         for group, flags in struct.iter_unpack('<II', payload[20:]) if group > 0),
                        key=lambda clip: clip.group_id, reverse=True))


def playback_request(clip):
    day = _date_code(clip.day)
    start = struct.pack('<10sHIH6x', day, 1, clip.group_id, 0)
    stop = struct.pack('<12sI8x', day, clip.group_id)
    return SessionRequest(PLAY, start, (STOP, stop))


def is_playback_reply(payload, clip):
    return (len(payload) >= 16 and payload[2:10] == _date_code(clip.day)
            and struct.unpack_from('<I', payload, 12)[0] == clip.group_id)


async def _ignore_frame(*_):
    pass


async def read_reply(device_id, key, request):
    stop = asyncio.Event()
    response = None

    async def on_control(command, payload):
        nonlocal response
        if command == request.command + 1:
            response = payload
            stop.set()

    await run_session(device_id, key, _ignore_frame, stop, monotonic() + 34, {}, request=request, on_control=on_control)
    if response is None:
        raise RelayTimeout()
    return response


