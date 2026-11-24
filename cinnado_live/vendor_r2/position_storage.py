"""Upload the placeholder required by the vendor's saved-position API."""

import base64
from email.utils import formatdate
import hmac
import json
import re
from urllib.parse import quote, urlsplit


# A transparent PNG keeps camera images out of position uploads.
PLACEHOLDER = base64.b64decode(
    'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNgYAAAAAMAASsJTYQAAAAASUVORK5CYII='
)


async def upload_position_placeholder(session, grant, slot):
    endpoint = urlsplit(grant['uploadUrl'])
    host = endpoint.hostname or ''
    if (endpoint.scheme != 'https' or endpoint.netloc != host or endpoint.path not in ('', '/')
            or endpoint.query or endpoint.fragment or not re.fullmatch(r'oss-[a-z0-9-]+\.aliyuncs\.com', host)):
        raise ValueError('Unexpected position storage host')

    credentials = json.loads(grant['uploadToken'])
    bucket = credentials['name']
    resource = grant['resourceId']
    if (not re.fullmatch(r'[a-z0-9][a-z0-9-]{1,61}[a-z0-9]', bucket)
            or not isinstance(resource, str) or len(resource) > 256
            or not all(re.fullmatch(r'[\w-]+', part, re.ASCII) for part in resource.split('/'))):
        raise ValueError('Unexpected position storage path')
    key = f'{resource}/0-{slot}.png'
    date = formatdate(usegmt=True)
    content_type = 'image/png'
    token = credentials['stsToken']
    canonical = f'PUT\n\n{content_type}\n{date}\nx-oss-security-token:{token}\n/{bucket}/{key}'
    signature = base64.b64encode(hmac.digest(credentials['accessKeySecret'].encode(), canonical.encode(), 'sha1')).decode()
    headers = {
        'Content-Type': content_type,
        'Date': date,
        'x-oss-security-token': token,
        'Authorization': f"OSS {credentials['accessKeyId']}:{signature}",
    }
    async with session.put(f'https://{bucket}.{host}/{quote(key)}', data=PLACEHOLDER,
                           headers=headers, allow_redirects=False) as response:
        if response.status != 200:
            raise RuntimeError('The position placeholder was not accepted')
    return key
