"""Official HTTPS Bot API adapter; stdlib only, no user sessions."""
import asyncio
import json
import mimetypes
import urllib.request
import urllib.error
import uuid

class APIError(Exception):
    pass

class DeliveryUnknown(Exception):
    pass

class Telegram:
    def __init__(self, token):
        if not token or ':' not in token:
            raise ValueError('Missing/invalid BOT_TOKEN')
        self._root = 'https://api.telegram.org/bot' + token + '/'
        self._file_root = 'https://api.telegram.org/file/bot' + token + '/'

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
            name = document.name.replace('"','_').replace('\r','_').replace('\n','_')
            pieces.append((f'--{boundary}\r\nContent-Disposition: form-data; name="document"; filename="{name}"\r\nContent-Type: application/octet-stream\r\n\r\n').encode())
            pieces.extend([document.read_bytes(), f'\r\n--{boundary}--\r\n'.encode()])
            data = b''.join(pieces); content_type = 'multipart/form-data; boundary=' + boundary
        request = urllib.request.Request(self._root + method, data=data, headers={'Content-Type':content_type})
        mutating = method.startswith(('send', 'edit', 'delete'))
        try:
            with urllib.request.urlopen(request, timeout=35 if method=='getUpdates' else 20) as response:
                body = json.loads(response.read(10_000_000))
            if not body.get('ok'):
                raise APIError('Telegram rejected operation')
            return body['result']
        except urllib.error.HTTPError as exc:
            if mutating and exc.code >= 500:
                raise DeliveryUnknown('Telegram response uncertain') from None
            raise APIError(f'Telegram HTTP {exc.code}') from None
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
