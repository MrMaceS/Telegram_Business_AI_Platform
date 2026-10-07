import io
import json
import unittest
import urllib.error
from unittest import mock
from assistant_core.telegram import Telegram, APIError, Forbidden, RateLimited, Conflict, DeliveryUnknown

TOKEN='123:SECRET'

def http_error(code,body):
    raw=json.dumps(body).encode() if isinstance(body,dict) else body
    return urllib.error.HTTPError('https://api.telegram.org/bot'+TOKEN+'/x',code,'err',{},io.BytesIO(raw))

class Response(io.BytesIO):
    def __enter__(self):return self
    def __exit__(self,*a):return False

class TelegramErrorTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):self.tg=Telegram(TOKEN)
    async def fails(self,method,error):
        with mock.patch('urllib.request.urlopen',side_effect=error):
            return await self.tg.call(method,{'chat_id':2,'text':'x'})

    async def test_403_is_forbidden_with_description(self):
        with self.assertRaises(Forbidden) as ctx:
            await self.fails('sendMessage',http_error(403,{'ok':False,'error_code':403,'description':'Forbidden: bot was blocked by the user'}))
        self.assertEqual(ctx.exception.code,403);self.assertIn('blocked',ctx.exception.description)
    async def test_429_carries_retry_after(self):
        with self.assertRaises(RateLimited) as ctx:
            await self.fails('sendMessage',http_error(429,{'ok':False,'error_code':429,'description':'Too Many Requests','parameters':{'retry_after':17}}))
        self.assertEqual(ctx.exception.retry_after,17)
    async def test_409_is_conflict(self):
        with self.assertRaises(Conflict):
            await self.fails('getUpdates',http_error(409,{'ok':False,'error_code':409,'description':'Conflict: terminated by other getUpdates request'}))
    async def test_400_is_plain_api_error(self):
        with self.assertRaises(APIError) as ctx:
            await self.fails('sendMessage',http_error(400,{'ok':False,'error_code':400,'description':'Bad Request: chat not found'}))
        self.assertIs(type(ctx.exception),APIError);self.assertEqual(ctx.exception.code,400)
    async def test_non_json_error_body_keeps_status(self):
        with self.assertRaises(Forbidden):
            await self.fails('sendMessage',http_error(403,b'<html>proxy</html>'))
    async def test_5xx_on_send_stays_unknown(self):
        with self.assertRaises(DeliveryUnknown):
            await self.fails('sendMessage',http_error(502,{'ok':False,'error_code':502,'description':'Bad Gateway'}))
    async def test_ok_false_in_200_body_is_typed(self):
        body=json.dumps({'ok':False,'error_code':403,'description':'Forbidden'}).encode()
        with mock.patch('urllib.request.urlopen',return_value=Response(body)):
            with self.assertRaises(Forbidden):await self.tg.call('sendMessage',{})
    async def test_token_never_in_error_text(self):
        with self.assertRaises(APIError) as ctx:
            await self.fails('sendMessage',http_error(400,{'ok':False,'error_code':400,'description':'Bad Request'}))
        e=ctx.exception;self.assertNotIn('SECRET',str(e)+e.description+repr(e.args))

if __name__=='__main__':unittest.main()
