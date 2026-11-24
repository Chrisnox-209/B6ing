"""Bounded, cancellable session using the validated R2 wire protocol."""
import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from queue import Empty
import socket
import time
from .camera_protocol import (
    PTZ_COMMAND, PTZ_POSITION_REQUEST, PTZ_POSITION_RESPONSE, ControlStream, TalkFormat,
    ack_datagram, auth_datagram, control_datagram, ptz_datagram, ptz_goto_payload,
    ptz_position_from_response, rc4,
)
from .wire import SERVERS, identity, relay_request, relay_join, relay_target

RELAYS = [('172.235.190.14', 12300), ('172.236.209.56', 12300)]
AUDIO_DATA = 0x103
AUDIO_START = 0x104
AUDIO_STOP = 0x106
SET_VIDEO_QUALITY = 0x10e
TALK_START, TALK_RESPONSE, TALK_STOP = 0x108, 0x109, 0x10c

class RelayTimeout(Exception):
    pass

class CameraAuthenticationError(Exception):
    pass


@dataclass(frozen=True)
class SessionRequest:
    command: int
    payload: bytes = b''
    teardown: tuple[int, bytes] | None = None


@dataclass
class PendingCommand:
    packet: bytes
    next_retry: float
    attempts: int = 1
    voice: bool = False


@dataclass(frozen=True)
class PositionRequest:
    target: tuple[int, int] | None
    complete: Callable[[tuple[int, int] | None], None]


class LiveControls:
    def __init__(self, key, network, peer):
        self.key, self.network, self.peer = key, network, peer
        self.sequence = 2
        self.direction = None
        self.quality = None
        self.listening = False
        self.pending = {}
        self.talk = None
        self.talk_format = None
        self.talk_deadline = None
        self.voice_acknowledged = 0
        self.position = None
        self.last_position = None
        self.position_next = 0
        self.position_deadline = 0

    async def send(self, make_packet, now, *, voice=False):
        sequence = self.sequence
        self.sequence = (sequence + 1) & 0xffff
        packet = make_packet(sequence)
        self.pending[sequence] = PendingCommand(packet, now + 0.25, voice=voice)
        await self.network.send(packet, self.peer)

    async def set_direction(self, direction, now):
        self.direction = direction
        self.last_position = None
        await self.send(lambda sequence: ptz_datagram(self.key, direction, sequence), now)

    async def command(self, code, payload, now):
        await self.send(lambda sequence: control_datagram(self.key, code, payload, sequence), now)

    async def update(self, desired, now, *, quality=None, listening=False, talk=None, positions=None):
        if self.direction is not None and self.direction != desired:
            await self.set_direction(None, now)
        if self.position is not None and (desired is not None or now >= self.position_deadline):
            self.finish_position(None)
        if self.talk is not None and self.talk is not talk:
            await self.stop_talk(now)
        if self.talk_deadline is not None and now >= self.talk_deadline:
            self.talk_deadline = None
            self.talk.ready(None)
        for command in self.pending.values():
            if now >= command.next_retry:
                if command.attempts == 4:
                    raise RelayTimeout()
                await self.network.send(command.packet, self.peer)
                command.attempts += 1
                command.next_retry = now + 0.25
        if self.pending:
            if self.talk_format is not None and talk is self.talk and len(self.pending) < 4:
                if all(command.voice for command in self.pending.values()):
                    await self.send_voice(now)
            return
        if desired is not None and self.direction is None:
            await self.set_direction(desired, now)
        elif listening != self.listening:
            self.listening = listening
            await self.command(AUDIO_START if listening else AUDIO_STOP, b'', now)
        elif quality is not None and quality != self.quality:
            self.quality = quality
            await self.command(SET_VIDEO_QUALITY, bytes((quality, 0, 0, 0)), now)
        elif talk is not None and self.talk is None:
            self.talk = talk
            self.talk_deadline = now + 4
            await self.command(TALK_START, b'', now)
        elif positions is not None and desired is None and await self.update_position(positions, now):
            return
        elif self.talk_format is not None and talk is self.talk:
            await self.send_voice(now)

    async def update_position(self, requests, now):
        if self.position is None:
            try:
                self.position = requests.get_nowait()
            except Empty:
                return False
            if self.position.target is None and self.last_position is not None:
                self.finish_position(self.last_position)
                return True
            self.position_deadline = now + (7 if self.position.target is not None else 4)
            if self.position.target is not None:
                self.last_position = None
                await self.command(PTZ_COMMAND, ptz_goto_payload(self.position.target), now)
                self.position_next = now + .4
                return True
            self.position_next = now
        if now < self.position_next:
            return False
        await self.command(PTZ_POSITION_REQUEST, b'', now)
        self.position_next = now + .6
        return True

    def position_response(self, payload):
        value = ptz_position_from_response(rc4(self.key.encode(), payload))
        if value is None:
            return
        if self.direction is None:
            self.last_position = value
        if self.position is None:
            return
        target = self.position.target
        if target is None or all(abs(a - b) <= 1 for a, b in zip(target, value)):
            self.finish_position(value)

    def finish_position(self, value):
        request, self.position = self.position, None
        if request is not None:
            request.complete(value)

    def talk_response(self, payload):
        if self.talk_deadline is not None:
            self.talk_deadline = None
            self.talk_format = TalkFormat.from_response(rc4(self.key.encode(), payload))
            self.talk.ready(self.talk_format)

    async def send_voice(self, now):
        data = self.talk.next_frame()
        if data:
            await self.send(lambda sequence: self.talk_format.datagram(data, sequence, time.time_ns() // 1_000_000),
                            now, voice=True)

    async def stop_talk(self, now):
        self.talk = self.talk_format = self.talk_deadline = None
        await self.command(TALK_STOP, b'', now)

    def acknowledge(self, sequences):
        for sequence in sequences:
            if (command := self.pending.pop(sequence, None)) is not None and command.voice:
                self.voice_acknowledged += 1

    async def stop(self):
        self.finish_position(None)
        if self.direction is not None:
            await self.set_direction(None, time.monotonic())
        if self.talk is not None:
            await self.stop_talk(time.monotonic())
        if self.listening:
            await self.command(AUDIO_STOP, b'', time.monotonic())
        for command in tuple(self.pending.values()):
            await self.network.send(command.packet, self.peer)


class UdpNetwork:
    def __init__(self):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(('0.0.0.0', 0))
        self.sock.setblocking(False)
        self.loop = asyncio.get_running_loop()

    async def send(self, data, peer):
        await self.loop.sock_sendto(self.sock, data, peer)

    async def receive(self):
        return await asyncio.wait_for(self.loop.sock_recvfrom(self.sock, 4096), .2)

    def close(self):
        self.sock.close()

async def run_session(device_id, key, on_frame, stop, deadline, stats, network_factory=UdpNetwork, *, direction=None,
                      quality=None, listening=None, on_audio=None, requested_talk=None,
                      request: SessionRequest | None = None, on_control=None, on_video_clock=None,
                      reconnect=None, position_requests=None):
    """Deliver Annex B frames until stop/deadline; never write media or keys to disk."""
    network = network_factory()
    allocations = {}
    requests = {}
    targets = {}
    selected = None
    auth = None
    auth_acked = False
    video_acked = False
    auth_tries = video_tries = 0
    video_packet = None
    auth_at = video_at = None
    first_frame = False
    first_media = None
    stream = ControlStream()
    started = time.monotonic()
    next_send = 0
    last_media = None
    controls = None
    try:
        while not stop.is_set() and time.monotonic() < deadline:
            now = time.monotonic()
            if selected is None and now-started >= 16:
                raise RelayTimeout()
            if auth_at is not None and video_at is None and now-auth_at >= 8:
                raise RelayTimeout()
            if reconnect is not None and reconnect.is_set():
                return
            if video_at is not None and last_media is None and now-video_at >= (20 if request else 10):
                raise RelayTimeout()
            if last_media is not None and now-last_media >= 8:
                raise RelayTimeout()
            if now >= next_send:
                if selected is None:
                    for peer in RELAYS:
                        if peer[0] not in {ip for ip, port in allocations}:
                            for data in (b'\xf1\x00\0\0', b'\xf1\x70\0\0', b'\xf1\x72\0\0'):
                                await network.send(data, peer)
                    for peer in SERVERS:
                        await network.send(b'\xf1\x00\0\0', peer)
                        for packet in requests.values():
                            await network.send(packet, peer)
                    for peer, nonce in targets.items():
                        await network.send(relay_join(device_id, nonce, peer), peer)
                else:
                    if not auth_acked and auth_tries < 5:
                        await network.send(auth, selected)
                        auth_tries += 1
                    if video_packet and not video_acked and video_tries < 5:
                        await network.send(video_packet, selected)
                        video_tries += 1
                    await network.send(b'\xf1\xe0\0\0', selected)
                next_send = now + 1
            if controls is not None and first_frame:
                await controls.update(direction() if direction is not None else None, now,
                                      quality=quality() if quality is not None else None,
                                      listening=listening() if listening is not None else False,
                                      talk=requested_talk() if requested_talk is not None else None,
                                      positions=position_requests)
            try:
                data, peer = await network.receive()
            except asyncio.TimeoutError:
                continue
            if len(data)<4 or data[0]!=0xf1 or int.from_bytes(data[2:4], 'big')!=len(data)-4:
                continue
            if selected is not None:
                if peer != selected:
                    continue
                if data[:2] == b'\xf1\xf0':
                    if not first_frame: raise RelayTimeout()
                    break
                if data[:2] == b'\xf1\xe0':
                    await network.send(b'\xf1\xe1\0\0', peer)
                if data[:4] == bytes.fromhex('f1830054') and len(data)==88:
                    if data[8:11]==b'ajy' and data[20:84].split(b'\0',1)[0]==device_id.encode() and data[4:8]==targets.get(peer):
                        ready=bytearray(92)
                        ready[:4]=bytes.fromhex('f1840058')
                        identity(ready,4,device_id)
                        await network.send(ready,peer)
                if data[:2] == b'\xf1\xd1' and len(data)>=8 and data[4:6]==b'\xd1\0':
                    count=int.from_bytes(data[6:8], 'big')
                    if len(data)==8+2*count:
                        sequences={int.from_bytes(data[i:i+2],'big') for i in range(8,len(data),2)}
                        auth_acked |= 0 in sequences
                        video_acked |= 1 in sequences
                        if controls is not None:
                            controls.acknowledge(sequences)
                            if controls.voice_acknowledged:
                                stats['voice_acknowledged'] = controls.voice_acknowledged
                if data[:2] != b'\xf1\xd0' or len(data)<8 or data[4]!=0xd1:
                    continue
                channel=data[5]
                sequence=int.from_bytes(data[6:8], 'big')
                # Do not acknowledge unbuffered data beyond the reordering window.
                position = stream.absolute_sequence(sequence)
                if channel == 0 and position >= stream.next_sequence + 128:
                    continue
                await network.send(ack_datagram(channel,sequence),peer)
                if channel != 0:
                    continue
                for command, payload in stream.feed(position,data[8:]):
                    if command == 151 and len(payload)==4:
                        status=int.from_bytes(rc4(key.encode(),payload),'little',signed=True)
                        if status != 0: raise CameraAuthenticationError()
                        auth_acked=True
                        stats['authenticated']=True
                        if video_packet is None:
                            code, body = (request.command, request.payload) if request else (256, bytes(4))
                            video_packet=control_datagram(key,code,body,1)
                            await network.send(video_packet,peer)
                            video_tries=1
                            video_at=time.monotonic()
                    elif command == 257 and request is None:
                        video_acked=True
                    elif command == TALK_RESPONSE and controls is not None:
                        controls.talk_response(payload)
                    elif command == PTZ_POSITION_RESPONSE and controls is not None:
                        controls.position_response(payload)
                    elif command == 'media' and video_at is not None:
                        codec=int.from_bytes(payload[6:8],'little')
                        raw=payload[48:]
                        if codec not in (2,3):
                            if codec == AUDIO_DATA and on_audio is not None and controls is not None and controls.listening:
                                await on_audio(raw)
                            continue
                        if not first_frame:
                            if payload[24]!=0 or not raw.startswith((b'\0\0\0\1',b'\0\0\1')):
                                continue
                            first_frame=True
                            first_media=time.monotonic()
                        last_media=time.monotonic()
                        stats['media_seconds']=last_media-first_media
                        stats['frames']=stats.get('frames',0)+1
                        stats['bytes']=stats.get('bytes',0)+len(raw)
                        stats['codec']='h264' if codec==2 else 'hevc'
                        if on_video_clock is not None:
                            on_video_clock(int.from_bytes(payload[16:24], 'little'))
                        await on_frame(raw,stats['codec'],payload[25] or 15)
                    elif command != 'media' and video_at is not None:
                        if on_control is not None:
                            await on_control(command, rc4(key.encode(), payload))
                continue
            if peer in RELAYS and data[:4]==bytes.fromhex('f1710000'):
                await network.send(b'\xf1\x72\0\0',peer)
            if peer in RELAYS and data[:4]==bytes.fromhex('f1730008') and len(data)==12:
                port=int.from_bytes(data[8:10],'big')
                target=(peer[0],port)
                if port and target not in allocations and len(allocations)<4:
                    allocations[target]=data[4:8]
                    packet=relay_request(device_id,data[4:8],target)
                    requests[target]=packet
                    for server in SERVERS:
                        await network.send(packet,server)
            if peer in SERVERS:
                offer=relay_target(data)
                if offer:
                    target,nonce=offer
                    if target in allocations and nonce == allocations[target]:
                        targets[target]=nonce
                        await network.send(b'\xf1\x00\0\0',target)
                        await network.send(relay_join(device_id,nonce,target),target)
            if peer in allocations and data[:4]==bytes.fromhex('f1840058') and len(data)==92:
                if data[4:7]==b'ajy' and data[16:80].split(b'\0',1)[0]==device_id.encode():
                    selected=peer
                    if any(control is not None for control in
                           (direction, quality, listening, requested_talk, position_requests)):
                        controls = LiveControls(key, network, peer)
                    stats['relay_selected']=True
                    auth=auth_datagram(key,int(time.time()))
                    auth_at=time.monotonic()
                    await network.send(auth,peer)
                    auth_tries=1
        if not first_frame and not stop.is_set():
            raise RelayTimeout()
    finally:
        if selected is not None:
            if controls is not None:
                try:
                    await asyncio.wait_for(controls.stop(), 0.5)
                except (OSError, asyncio.TimeoutError):
                    pass
            try:
                teardown = request.teardown if request else (258, b'')
                if video_packet is not None and teardown is not None:
                    sequence = controls.sequence if controls is not None else 2
                    code, body = teardown
                    await asyncio.wait_for(network.send(control_datagram(key,code,body,sequence),selected),1)
                await asyncio.wait_for(network.send(b'\xf1\xf0\0\0',selected),1)
            except (OSError,asyncio.TimeoutError):
                pass
        network.close()
