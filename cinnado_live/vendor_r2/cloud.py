#!/usr/bin/env python3
"""Authentication and signed requests for the AJCloud API."""
import asyncio
import base64
from dataclasses import dataclass
from datetime import datetime
import hashlib
import hmac
import json
import os
import time
from urllib.parse import urlsplit
import uuid

import aiohttp
from nacl.public import Box, PrivateKey, PublicKey

BASE = 'https://uac-eu.ajcloud.net/api'
RENEWAL_RETRY_SECONDS = 30
SAFE = {'rtc','localRtsp','onvif','privLiveStream','battery','prodName','fwVersion','build','deviceMode','vendorCode','deviceType','onlineStatus','conStatus','webrtcEnable','codec','quality'}

def sanitized(value, key=''):
    if key in {'context', 'config'} and isinstance(value, str):
        try:
            decoded=json.loads(value)
        except (ValueError, TypeError):
            return '<masqué>'
        if isinstance(decoded, (dict,list)):
            return sanitized(decoded)
    if isinstance(value, dict):
        return {k: sanitized(v, k) for k,v in value.items()}
    if isinstance(value, list):
        return [sanitized(v, key) for v in value]
    if key in SAFE or isinstance(value, (bool, type(None))):
        return value
    return '<masqué>'

class CloudAuthenticationError(RuntimeError):
    """A session error that is safe to display without the server response."""


class _AuthenticationRejected(RuntimeError):
    pass


@dataclass(frozen=True, repr=False)
class _Authorization:
    access: str
    signature: str
    renew_at: float


class Client:
    def __init__(self, session):
        self.session = session
        self.agent = str(uuid.uuid4())
        self._authorization = None
        self._credentials = None
        self._renewal_lock = asyncio.Lock()
        self._retry_at = 0

    def clear_authentication(self):
        self._authorization = None
        self._credentials = None
        self._retry_at = 0

    async def _renew_authorization(self, previous):
        async with self._renewal_lock:
            credentials = self._credentials
            if credentials is None:
                raise CloudAuthenticationError('La session Wansview a été fermée.')
            if self._authorization is not previous:
                return self._authorization
            message = 'AJCloud n’a pas renouvelé la connexion Wansview. Réessaie dans un instant.'
            if time.monotonic() < self._retry_at:
                raise CloudAuthenticationError(message)
            # Status checks, alerts and live sessions share one renewal attempt.
            self._retry_at = time.monotonic() + RENEWAL_RETRY_SECONDS
            try:
                authorization = await self._sign_in(*credentials)
            except asyncio.CancelledError:
                # A user command can cancel a background camera status check.
                self._retry_at = 0
                raise
            except (aiohttp.ClientError, TimeoutError, KeyError, TypeError, ValueError, RuntimeError) as exc:
                raise CloudAuthenticationError(message) from exc
            if self._credentials is not credentials:
                raise CloudAuthenticationError('La session Wansview a été fermée.')
            self._authorization = authorization
            return authorization

    async def post(self, route, data, signed=False, base=BASE, device_id=None):
        if not signed:
            return await self._post(route, data, base, device_id)

        authorization = self._authorization
        if authorization is None:
            raise CloudAuthenticationError('La session Wansview a été fermée.')
        if time.time() >= authorization.renew_at:
            authorization = await self._renew_authorization(authorization)
        try:
            return await self._post(route, data, base, device_id, authorization)
        except _AuthenticationRejected:
            # Retry only explicit authentication rejections, never other failed camera commands.
            authorization = await self._renew_authorization(authorization)
        try:
            return await self._post(route, data, base, device_id, authorization)
        except _AuthenticationRejected as exc:
            raise CloudAuthenticationError('AJCloud a refusé l’autorisation Wansview.') from exc

    async def _post(self, route, data, base, device_id, authorization=None):
        meta={'locale':'fr','localtz':int(datetime.now().astimezone().utcoffset().total_seconds()/60),
              'localtzn':'Europe/Paris','appName':'wansview','appVersion':'2.0.26012704',
              'appVendorCode':'WVC','agentName':'SM-S926B','appCountryCode':'FR','appContinentCode':'EU'}
        if authorization is not None: meta['accessToken'] = authorization.access
        if device_id: meta['deviceId']=device_id
        body=json.dumps({'meta':meta,'data':data},separators=(',',':'),ensure_ascii=False)
        parsed=urlsplit(base)
        if parsed.scheme!='https' or not parsed.hostname or not (parsed.hostname=='ajcloud.net' or parsed.hostname.endswith('.ajcloud.net')) or parsed.username or parsed.password:
            raise RuntimeError('Serveur inattendu : requête annulée')
        url=base.rstrip('/')+route
        headers={'Content-Type':'application/json;charset=utf-8','x-agent-token':self.agent,
                 'User-Agent':'15; en-us; SM-S926B Build/AP3A.240905.015.A2;wansview/V2.0.26012704'}
        if authorization is not None:
            ts=str(int(time.time()*1000)+1000)
            canonical='POST\n'+urlsplit(url).path+'\n'+hashlib.sha256(body.encode()).hexdigest()+'\n'
            message='HMAC-SHA256\n'+ts+'\n'+hashlib.sha256(canonical.encode()).hexdigest()
            sig=base64.urlsafe_b64encode(hmac.new(authorization.signature.encode(),message.encode(),hashlib.sha256).digest()).decode().rstrip('=')
            headers.update({'Authorization':'Bearer '+authorization.access,'X-UAC-Signature':'UAC1-HMAC-SHA256;'+ts+';'+sig})
        try:
            async with self.session.post(url,data=body,headers=headers,allow_redirects=False) as response:
                if authorization is not None and response.status == 401:
                    raise _AuthenticationRejected()
                if response.status!=200: raise RuntimeError('Réponse HTTP '+str(response.status))
                result=await response.json(content_type=None)
        except Exception as error:
            error.diagnostic_hostname = parsed.hostname
            error.diagnostic_route = route
            raise
        if not isinstance(result, dict):
            raise ValueError('Réponse AJCloud incompatible.')
        if result.get('status')!='ok' or result.get('code') not in (None,0,'0'):
            if authorization is not None and result.get('code') in (401, '401'):
                raise _AuthenticationRejected()
            raise RuntimeError('AJCloud refuse la requête ; code='+str(result.get('code')))
        return result.get('result',{})

    async def login(self, email, password):
        self._authorization = await self._sign_in(email, password)
        # Keep the credentials in memory for reauthentication until the account is closed.
        self._credentials = (email, password)

    async def _sign_in(self, email, password):
        challenge=await self.post('/v3/outer-challenge',{'username':email,'action':'signin','agentName':'SM-S926B','agentToken':self.agent})
        nonce=os.urandom(24)
        box=Box(PrivateKey(base64.b64decode(challenge['clientSecretKey'])),PublicKey(base64.b64decode(challenge['serverPubKey'])))
        encrypted=base64.b64encode(box.encrypt(password.encode(),nonce).ciphertext).decode()
        result=await self.post('/v1/signin',{'username':email,'password':encrypted,'nonce':base64.b64encode(nonce).decode(),
            'agentName':'SM-S926B','modelName':'SM-S926B','agentToken':self.agent,'osName':'android','origin':'mobile phone','grantType':'password','scope':'all'})
        access, signature = result['accessToken'], result['signToken']
        if not all(isinstance(token, str) and token for token in (access, signature)):
            raise ValueError('Autorisations AJCloud absentes.')
        try:
            lifetime = int(str(result.get('accessExpiresIn')))
        except ValueError:
            lifetime = 0
        renew_at = time.time() + lifetime - min(60, lifetime / 10) if lifetime > 0 else float('inf')
        return _Authorization(access, signature, renew_at)
