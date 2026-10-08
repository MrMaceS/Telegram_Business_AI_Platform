"""Official HTTPS Bot API adapter; stdlib only, no user sessions."""
import asyncio
import json
import os
import mimetypes
import urllib.request
import urllib.error
import uuid

MAX_UPLOAD = 50 * 1024 * 1024  # Bot API sendDocument limit

class APIError(Exception):
    """Telegram definitely did not perform the operation."""
    def __init__(self, message='Telegram request failed', code=None, description='', retry_after=None):
        super().__init__(message)
        self.code, self.description, self.retry_after = code, description, retry_after

class Forbidden(APIError):
    """403: bot blocked by user, Business rights revoked or chat not allowed."""

class RateLimited(APIError):
    """429: flood control; retry_after seconds before the next request."""

class Conflict(APIError):
    """409: another getUpdates poller or a webhook owns this token."""

class DeliveryUnknown(Exception):
    pass

def rejection(body):
    """Typed error from Telegram's {"ok":false,"error_code":..} body; description never holds the token."""
    code = body.get('error_code')
    description = str(body.get('description', ''))[:300]
    retry_after = (body.get('parameters') or {}).get('retry_after')
    cls = {403: Forbidden, 429: RateLimited, 409: Conflict}.get(code, APIError)
    return cls(f'Telegram HTTP {code}', code, description, retry_after)

def api_base():
    """TELEGRAM_API_URL points the bot at a local fake Bot API for end-to-end tests.
    Loopback only: the token is part of every URL and must never go to another host."""
    url = os.environ.get('TELEGRAM_API_URL', '').rstrip('/')
    if not url:
        return 'https://api.telegram.org'
    if not url.startswith(('http://127.0.0.1:', 'http://localhost:')):
        raise ValueError('TELEGRAM_API_URL must be http://127.0.0.1:PORT or http://localhost:PORT')
    return url

class Telegram:
    def __init__(self, token):
        if not token or ':' not in token:
            raise ValueError('Missing/invalid BOT_TOKEN')
        base = api_base()
        self._root = base + '/bot' + token + '/'
        self._file_root = base + '/file/bot' + token + '/'

    async def call(self, method, payload=None, document=None):
        return await asyncio.to_thread(self._call, method, payload or {}, document)

    def _call(self, method, payload, document):
        if document is None:
            data = json.dumps(payload).encode()
            content_type = 'application/json'
        else:
            boundary = 'tgcore' + uuid.uuid4().hex
            pieces = []
            for k, v in payload.items():
                pieces.append((f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n').encode())
            # Read before any network I/O: a missing/locked file is a definite "not sent", never a crash.
            try:
                content = document.read_bytes()
            except OSError:
                raise APIError('Document unavailable') from None
            if not content or len(content) > MAX_UPLOAD:
                raise APIError('Document empty or over 50 MB')
            name = document.name.replace('"','_').replace('\r','_').replace('\n','_')
            mime = mimetypes.guess_type(name)[0] or 'application/octet-stream'
            pieces.append((f'--{boundary}\r\nContent-Disposition: form-data; name="document"; filename="{name}"\r\nContent-Type: {mime}\r\n\r\n').encode())
            pieces.extend([content, f'\r\n--{boundary}--\r\n'.encode()])
            data = b''.join(pieces); content_type = 'multipart/form-data; boundary=' + boundary
        request = urllib.request.Request(self._root + method, data=data, headers={'Content-Type':content_type})
        mutating = method.startswith(('send', 'edit', 'delete'))
        try:
            with urllib.request.urlopen(request, timeout=35 if method=='getUpdates' else 20) as response:
                body = json.loads(response.read(10_000_000))
            if not body.get('ok'):
                raise rejection(body)
            return body['result']
        except urllib.error.HTTPError as exc:
            if mutating and exc.code >= 500:
                raise DeliveryUnknown('Telegram response uncertain') from None
            try:
                body = json.loads(exc.read(100_000))
            except Exception:
                body = {}
            body.setdefault('error_code', exc.code)
            raise rejection(body) from None
        except (urllib.error.URLError, TimeoutError, OSError, ValueError, KeyError):
            if mutating:
                raise DeliveryUnknown('Telegram response uncertain') from None
            raise APIError('Telegram request failed') from None

    async def download(self, file_id, destination, limit):
        info = await self.call('getFile', {'file_id':file_id})
        if info.get('file_size', 0) > limit:
            raise APIError('File too large')
        path = info['file_path']
        if path.startswith('/') or '..' in path.split('/'):
            raise APIError('Unexpected Telegram file path')
        def read():
            count = 0
            try:
                with urllib.request.urlopen(self._file_root + path, timeout=30) as stream, destination.open('xb') as out:
                    while chunk := stream.read(65536):
                        count += len(chunk)
                        if count > limit:
                            raise APIError('File too large')
                        out.write(chunk)
                destination.chmod(0o600)
            except Exception:
                destination.unlink(missing_ok=True)
                raise APIError('File download failed') from None
        await asyncio.to_thread(read)
