import io
import json
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

_SRC = str(Path(__file__).resolve().parents[1] / 'src')
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from assistant_core.main import sync_connection
from assistant_core.storage import Store
from assistant_core.telegram import (
    APIError,
    Conflict,
    DeliveryUnknown,
    Forbidden,
    RateLimited,
    Telegram,
)
from assistant_core.workflow import Engine

TOKEN = '123:SECRET'


def http_error(code, body):
    raw = json.dumps(body).encode() if isinstance(body, dict) else body
    return urllib.error.HTTPError(
        'https://api.telegram.org/bot' + TOKEN + '/x',
        code, 'err', {}, io.BytesIO(raw))


class Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class TelegramErrorTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tg = Telegram(TOKEN)

    async def fails(self, method, error):
        with mock.patch('urllib.request.urlopen', side_effect=error):
            return await self.tg.call(method, {'chat_id': 2, 'text': 'x'})

    async def test_403_is_forbidden_with_description(self):
        error = http_error(403, {
            'ok': False, 'error_code': 403,
            'description': 'Forbidden: bot was blocked by the user'})
        with self.assertRaises(Forbidden) as ctx:
            await self.fails('sendMessage', error)
        self.assertEqual(ctx.exception.code, 403)
        self.assertIn('blocked', ctx.exception.description)

    async def test_429_carries_retry_after(self):
        error = http_error(429, {
            'ok': False, 'error_code': 429,
            'description': 'Too Many Requests',
            'parameters': {'retry_after': 17}})
        with self.assertRaises(RateLimited) as ctx:
            await self.fails('sendMessage', error)
        self.assertEqual(ctx.exception.retry_after, 17)

    async def test_409_is_conflict(self):
        error = http_error(409, {
            'ok': False, 'error_code': 409,
            'description': 'Conflict: terminated by other getUpdates '
                           'request'})
        with self.assertRaises(Conflict):
            await self.fails('getUpdates', error)

    async def test_400_is_plain_api_error(self):
        error = http_error(400, {
            'ok': False, 'error_code': 400,
            'description': 'Bad Request: chat not found'})
        with self.assertRaises(APIError) as ctx:
            await self.fails('sendMessage', error)
        self.assertIs(type(ctx.exception), APIError)
        self.assertEqual(ctx.exception.code, 400)

    async def test_non_json_error_body_keeps_status(self):
        with self.assertRaises(Forbidden):
            await self.fails('sendMessage',
                             http_error(403, b'<html>proxy</html>'))

    async def test_5xx_on_send_stays_unknown(self):
        error = http_error(502, {
            'ok': False, 'error_code': 502, 'description': 'Bad Gateway'})
        with self.assertRaises(DeliveryUnknown):
            await self.fails('sendMessage', error)

    async def test_ok_false_in_200_body_is_typed(self):
        body = json.dumps({
            'ok': False, 'error_code': 403,
            'description': 'Forbidden'}).encode()
        with mock.patch('urllib.request.urlopen',
                        return_value=Response(body)):
            with self.assertRaises(Forbidden):
                await self.tg.call('sendMessage', {})

    async def test_token_never_in_error_text(self):
        error = http_error(400, {
            'ok': False, 'error_code': 400, 'description': 'Bad Request'})
        with self.assertRaises(APIError) as ctx:
            await self.fails('sendMessage', error)
        exc = ctx.exception
        text = str(exc) + exc.description + repr(exc.args)
        self.assertNotIn('SECRET', text)


class DocumentUploadTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.tg = Telegram(TOKEN)

    def tearDown(self):
        self.tmp.cleanup()

    async def send(self, path):
        ok = Response(json.dumps(
            {'ok': True, 'result': {'message_id': 5}}).encode())
        with mock.patch('urllib.request.urlopen',
                        return_value=ok) as urlopen:
            try:
                result = await self.tg.call(
                    'sendDocument', {'chat_id': 2, 'caption': 'c'},
                    document=path)
                return result, urlopen
            except APIError as exc:
                return exc, urlopen

    async def test_pdf_sent_as_application_pdf(self):
        pdf = self.dir / 'company_presentation.pdf'
        pdf.write_bytes(b'%PDF-1.4 demo')
        result, urlopen = await self.send(pdf)
        self.assertEqual(result, {'message_id': 5})
        body = urlopen.call_args[0][0].data
        self.assertIn(
            b'filename="company_presentation.pdf"\r\n'
            b'Content-Type: application/pdf', body)
        self.assertIn(b'%PDF-1.4 demo', body)

    async def test_missing_file_is_definite_failure_without_network(self):
        result, urlopen = await self.send(self.dir / 'gone.pdf')
        self.assertIsInstance(result, APIError)
        self.assertNotIsInstance(result, DeliveryUnknown)
        urlopen.assert_not_called()

    async def test_empty_file_rejected_without_network(self):
        empty = self.dir / 'empty.pdf'
        empty.write_bytes(b'')
        result, urlopen = await self.send(empty)
        self.assertIsInstance(result, APIError)
        urlopen.assert_not_called()


class FakeTelegram:
    def __init__(self, result=None, error=None):
        self.result, self.error, self.calls = result, error, []

    async def call(self, method, payload=None, document=None):
        self.calls.append((method, payload))
        if self.error:
            raise self.error
        return self.result


def conn(enabled=True, can_reply=True, owner=1):
    return {
        'id': 'c', 'user': {'id': owner}, 'is_enabled': enabled,
        'rights': {'can_reply': can_reply},
    }


class SyncConnectionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Store(Path(self.tmp.name), 'a')
        self.cfg = SimpleNamespace(business_owner_id=1)
        self.db.set('connection', json.dumps(conn()))

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def stored(self):
        return json.loads(self.db.get('connection'))

    async def test_unchanged_connection_is_ok(self):
        tg = FakeTelegram(conn())
        self.assertIsNone(await sync_connection(tg, self.db, self.cfg))
        self.assertEqual(
            tg.calls,
            [('getBusinessConnection', {'business_connection_id': 'c'})])

    async def test_revoked_while_offline_is_stored(self):
        tg = FakeTelegram(conn(enabled=False))
        self.assertIsNotNone(await sync_connection(tg, self.db, self.cfg))
        self.assertFalse(self.stored()['is_enabled'])

    async def test_can_reply_removed_warns(self):
        tg = FakeTelegram(conn(can_reply=False))
        self.assertIn('can_reply',
                      await sync_connection(tg, self.db, self.cfg))
        self.assertFalse(self.stored()['rights']['can_reply'])

    async def test_definitive_refusal_fails_closed(self):
        tg = FakeTelegram(error=APIError('x', 400, 'Bad Request'))
        await sync_connection(tg, self.db, self.cfg)
        self.assertFalse(self.stored()['is_enabled'])

    async def test_network_failure_keeps_state(self):
        tg = FakeTelegram(error=APIError())
        self.assertIsNotNone(await sync_connection(tg, self.db, self.cfg))
        self.assertTrue(self.stored()['is_enabled'])

    async def test_foreign_owner_fails_closed(self):
        tg = FakeTelegram(conn(owner=99))
        await sync_connection(tg, self.db, self.cfg)
        self.assertFalse(self.stored()['is_enabled'])
        self.assertEqual(self.stored()['user']['id'], 1)

    async def test_no_connection_yet_skips_api(self):
        self.db.set('connection', '{}')
        tg = FakeTelegram()
        self.assertIsNotNone(await sync_connection(tg, self.db, self.cfg))
        self.assertEqual(tg.calls, [])


class AdmissionTests(unittest.IsolatedAsyncioTestCase):
    """Only private chats of real candidates reach the workflow."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Store(Path(self.tmp.name), 'a')
        self.cfg = SimpleNamespace(
            owner_id=10, business_owner_id=1, contacts=())
        self.db.set('connection', json.dumps(conn()))
        self.e = Engine(self.cfg, self.db, FakeTelegram())

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    async def admit(self, chat=2, sender=None, chat_type='private',
                    is_bot=False, uid=1):
        m = {
            'message_id': uid, 'date': 0,
            'chat': {'id': chat, 'type': chat_type},
            'business_connection_id': 'c',
            'from': {'id': sender or chat, 'is_bot': is_bot},
            'text': 'Интересует вакансия',
        }
        update = {'update_id': uid, 'business_message': m}
        self.db.ingest(update)
        return await self.e.admit(update)

    async def test_candidate_admitted(self):
        self.assertTrue(await self.admit())
        self.assertIsNotNone(self.db.conversation(2))

    async def test_owner_service_account_not_candidate(self):
        self.assertFalse(await self.admit(chat=10))
        self.assertIsNone(self.db.conversation(10))

    async def test_business_owner_saved_messages_not_candidate(self):
        self.assertFalse(await self.admit(chat=1))
        self.assertIsNone(self.db.conversation(1))

    async def test_bot_not_candidate(self):
        self.assertFalse(await self.admit(chat=5, is_bot=True))
        self.assertIsNone(self.db.conversation(5))

    async def test_group_ignored(self):
        self.assertFalse(await self.admit(chat=-100, chat_type='group'))


if __name__ == '__main__':
    unittest.main()