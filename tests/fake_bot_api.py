"""Local fake of the Telegram Bot API for end-to-end runs of the real bot process.

The bot is started with TELEGRAM_API_URL=http://127.0.0.1:PORT and talks HTTP to this
server exactly as it would to api.telegram.org. The test pushes updates (candidate and
owner messages, Business connection changes), injects failures and reads what was sent.
"""
import email
import email.policy
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BOT_ID = 4242
TOKEN = f'{BOT_ID}:FAKE-TOKEN'


class FakeBotAPI:
    def __init__(self):
        self.lock = threading.Condition()
        self.updates = []          # every update ever pushed; getUpdates filters by offset
        self.sent = []             # what the bot sent: dicts with method/chat_id/text/...
        self.calls = []            # method names in call order
        self.next_uid = 1
        self.mids = {}             # chat -> last message_id (one counter per chat, like Telegram)
        self.connection = None     # answer of getBusinessConnection
        self.failures = {}         # method -> list of injected failures, consumed in order
        server = ThreadingHTTPServer(('127.0.0.1', 0), self._handler())
        server.daemon_threads = True
        self.server = server
        self.url = f'http://127.0.0.1:{server.server_address[1]}'
        threading.Thread(target=server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()

    # ------------------------------------------------------------------ test side

    def mid(self, chat):
        with self.lock:
            self.mids[chat] = self.mids.get(chat, 0) + 1
            return self.mids[chat]

    def push(self, **update):
        with self.lock:
            update['update_id'] = self.next_uid
            self.next_uid += 1
            self.updates.append(update)
            self.lock.notify_all()
            return update['update_id']

    def connect(self, owner, enabled=True, can_reply=True, conn_id='conn-1'):
        """Owner connects (or changes) the bot in Telegram Business settings."""
        self.connection = {'id': conn_id, 'user': {'id': owner, 'is_bot': False, 'first_name': 'Owner'},
                           'user_chat_id': owner, 'date': int(time.time()), 'is_enabled': enabled,
                           'rights': {'can_reply': can_reply}}
        return self.push(business_connection=self.connection)

    def candidate_says(self, chat, text, conn_id='conn-1', sender=None, chat_type='private', is_bot=False):
        message = {'message_id': self.mid(chat), 'date': int(time.time()),
                   'chat': {'id': chat, 'type': chat_type},
                   'from': {'id': sender or chat, 'is_bot': is_bot, 'first_name': 'C'},
                   'business_connection_id': conn_id, 'text': text}
        self.push(business_message=message)
        return message

    def owner_edits(self, message, text):
        self.push(edited_business_message=dict(message, text=text, edit_date=int(time.time())))

    def owner_command(self, owner, text, sender=None):
        self.push(message={'message_id': self.mid(owner), 'date': int(time.time()),
                           'from': {'id': sender or owner}, 'chat': {'id': owner, 'type': 'private'},
                           'text': text})

    def fail(self, method, kind, **extra):
        """kind: an HTTP code (403, 429, 500...) or 'drop' (connection closed without an answer)."""
        with self.lock:
            self.failures.setdefault(method, []).append((kind, extra))

    def to(self, chat):
        with self.lock:
            return [m for m in self.sent if m['chat_id'] == chat]

    def wait(self, predicate, timeout=10.0, what='condition'):
        deadline = time.time() + timeout
        while time.time() < deadline:
            with self.lock:
                if predicate():
                    return
            time.sleep(0.05)
        raise AssertionError(f'Timed out waiting for {what}; sent={self.sent}')

    def settle(self, quiet=0.4, timeout=10.0):
        """Wait until the bot has consumed all updates and sent nothing for `quiet` seconds."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            with self.lock:
                consumed = self.offset > self.updates[-1]['update_id'] if self.updates else True
                count = len(self.calls)
            time.sleep(quiet)
            with self.lock:
                if consumed and len(self.calls) == count:
                    return
        raise AssertionError('Bot did not settle')

    offset = 0

    # ------------------------------------------------------------------ bot side

    def _answer(self, method, params):
        with self.lock:
            self.calls.append(method)
            queue = self.failures.get(method)
            failure = queue.pop(0) if queue else None
        if failure:
            kind, extra = failure
            if kind == 'drop':
                return 'drop'
            body = {'ok': False, 'error_code': kind, 'description': extra.get('description', 'Injected')}
            if 'retry_after' in extra:
                body['parameters'] = {'retry_after': extra['retry_after']}
            return kind, body

        if method == 'getMe':
            return 200, {'ok': True, 'result': {'id': BOT_ID, 'is_bot': True, 'username': 'fake_bot',
                                                'can_connect_to_business': True}}
        if method == 'getWebhookInfo':
            return 200, {'ok': True, 'result': {'url': '', 'pending_update_count': 0}}
        if method == 'getBusinessConnection':
            c = self.connection
            if not c or c['id'] != params.get('business_connection_id'):
                return 400, {'ok': False, 'error_code': 400, 'description': 'Bad Request: BUSINESS_CONNECTION_INVALID'}
            return 200, {'ok': True, 'result': c}
        if method == 'getUpdates':
            offset = int(params.get('offset') or 0)
            deadline = time.time() + min(float(params.get('timeout') or 0), 2.0)
            with self.lock:
                self.offset = max(self.offset, offset)
                while True:
                    ready = [u for u in self.updates if u['update_id'] >= offset][:int(params.get('limit') or 100)]
                    left = deadline - time.time()
                    if ready or left <= 0:
                        return 200, {'ok': True, 'result': ready}
                    self.lock.wait(left)
        if method in ('sendMessage', 'sendDocument'):
            chat = int(params['chat_id'])
            record = {'method': method, 'chat_id': chat, 'text': params.get('text'),
                      'caption': params.get('caption'),
                      'business_connection_id': params.get('business_connection_id'),
                      'filename': params.get('_filename'), 'content_type': params.get('_content_type'),
                      'size': params.get('_size')}
            mid = self.mid(chat)
            with self.lock:
                self.sent.append(record)
            return 200, {'ok': True, 'result': {'message_id': mid, 'chat': {'id': chat}, 'date': int(time.time())}}
        return 404, {'ok': False, 'error_code': 404, 'description': f'Not Found: {method}'}

    def _handler(self):
        api = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                parts = self.path.strip('/').split('/')
                if len(parts) != 2 or parts[0] != 'bot' + TOKEN:
                    self.send_error(401)
                    return
                raw = self.rfile.read(int(self.headers.get('Content-Length') or 0))
                ctype = self.headers.get('Content-Type', '')
                if ctype.startswith('multipart/form-data'):
                    params = parse_multipart(ctype, raw)
                else:
                    params = json.loads(raw or b'{}')
                answer = api._answer(parts[1], params)
                if answer == 'drop':
                    self.close_connection = True
                    self.connection.close()
                    return
                code, body = answer
                data = json.dumps(body).encode()
                self.send_response(code)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(data)))
                try:
                    self.end_headers()
                    self.wfile.write(data)
                except (BrokenPipeError, ConnectionResetError):
                    pass   # бот остановлен посреди долгого опроса — это нормально

        return Handler


def parse_multipart(content_type, raw):
    msg = email.message_from_bytes(b'Content-Type: ' + content_type.encode() + b'\r\n\r\n' + raw,
                                   policy=email.policy.HTTP)
    params = {}
    for part in msg.iter_parts():
        name = part.get_param('name', header='content-disposition')
        payload = part.get_payload(decode=True)
        if part.get_filename():
            params.update(_filename=part.get_filename(), _content_type=part.get_content_type(),
                          _size=len(payload))
        else:
            params[name] = payload.decode()
    return params
