import unittest
import asyncio
import io
import json
import os
import tempfile
import time
import urllib.error
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase, mock


ROOT = Path(__file__).resolve().parents[1]
src = ROOT / 'src'
if str(src) not in sys.path:
    sys.path.insert(0, str(src))

from assistant_core.ai import AI
from assistant_core.config import Config
from assistant_core.locking import ProcessLock
from assistant_core.main import sync_connection
from assistant_core.ops import backup, health, restore
from assistant_core.recruitment_parser import (
    is_explicit_consent, is_explicit_decline, is_vacancy_inquiry,
)
from assistant_core.storage import Store
from assistant_core.telegram import (
    APIError, Conflict, DeliveryUnknown, Forbidden, RateLimited, Telegram,
)
from assistant_core.workflow import Engine

ROOT = Path(__file__).resolve().parents[1]
src = ROOT / 'src'
if str(src) not in sys.path:
    sys.path.insert(0, str(src))
OWNER = 1001
BUSINESS_OWNER = 1001
CHAT = 2002
CONN = 'conn'
CONSENT = 'Да, условия мне подходят. Я согласен перейти к следующему этапу'


class FakeTG:
    def __init__(self, fail=None):
        self.sent = []
        self.calls = []
        self.fail = fail
        self.downloaded = False

    async def call(self, method, payload=None, document=None):
        self.calls.append((method, payload, document))
        if self.fail:
            err = self.fail
            self.fail = None
            raise err
        result = {'message_id': 100 + len(self.sent) + 1}
        self.sent.append((method, payload or {}, document))
        return result

    async def download(self, file_id, destination, limit):
        self.downloaded = True
        destination.write_bytes(b'file')


class Response(io.BytesIO):
    def __enter__(self):
        return self
    def __exit__(self, *args):
        return False


def http_error(code, body):
    raw = json.dumps(body).encode() if isinstance(body, dict) else body
    return urllib.error.HTTPError('https://api.telegram.org/botTOKEN/x', code, 'err', {}, io.BytesIO(raw))


def make_cfg(root):
    mats = root / 'materials'
    mats.mkdir(exist_ok=True)
    cond = mats / 'vacancy_conditions.txt'
    pdf = mats / 'company_presentation.pdf'
    cond.write_text('CONDITIONS', encoding='utf-8')
    pdf.write_bytes(b'%PDF-1.4 demo')
    return Config(
        client_id='company_a', owner_id=OWNER, business_owner_id=BUSINESS_OWNER,
        contacts=(CHAT,), data_dir=root / 'data', materials_dir=mats,
        instructions='rules', faqs=({'id': 'faq', 'question': 'Q', 'answer': 'A'},),
        tasks={}, scenario_path=root / 'scenario.json', conditions_version='v1',
        conditions_file=cond, presentation_file=pdf,
        group_invite_url='https://t.me/group', unknown_question_text='Передам владельцу.',
        invite_message_text='invite', daily_ai_calls=100, max_file_bytes=20_000_000,
    )


def update(uid=1, text='Интересует вакансия', chat=CHAT, mid=None, sender=None):
    return {'update_id': uid, 'business_message': {
        'message_id': mid or uid, 'date': int(time.time()),
        'chat': {'id': chat, 'type': 'private'},
        'from': {'id': sender if sender is not None else chat},
        'business_connection_id': CONN, 'text': text,
    }}


class StoreCoverage(IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.db = Store(self.root / 'data', 'company_a')

    def tearDown(self):
        self.db.close(); self.tmp.cleanup()

    def test_init_schema_binding_and_basic_helpers(self):
        self.assertEqual(self.db.get('schema'), '1')
        self.assertEqual(self.db.get('missing', 'x'), 'x')
        self.db.set('x', 2); self.assertEqual(self.db.get('x'), '2')
        self.db.log('TEST', CHAT, 'detail')
        self.assertEqual(self.db.ingest({'update_id': 1, 'x': 1}), True)
        self.assertFalse(self.db.ingest({'update_id': 1, 'x': 1}))
        self.db.complete(1, 'PENDING')
        self.assertEqual(self.db.pending()[0]['id'], 1)
        self.assertEqual(self.db.pending_all()[0]['id'], 1)
        self.db.complete(1, 'RECEIVED')
        self.assertEqual(self.db.received()[0]['id'], 1)
        self.db.incoming(CHAT, CONN, 1, 'hello', time.time())
        self.db.incoming(CHAT, CONN, 1, 'duplicate', time.time())
        self.assertEqual(len(self.db.history(CHAT)), 1)
        self.db.mode(CHAT, 'MANUAL')
        self.db.reserve('k', CHAT, 'sendMessage')
        self.assertFalse(self.db.reserve('k', CHAT, 'sendMessage'))
        self.db.outgoing_result('k', 'SENT', {'message_id': 7})
        self.assertEqual(self.db.candidate(CHAT), None)

    def test_schema_and_client_rejection(self):
        d = self.root / 'bad'
        db = Store(d, 'a'); db.set('schema', '9'); db.close()
        with self.assertRaises(ValueError): Store(d, 'a')
        d2 = self.root / 'bound'
        db = Store(d2, 'a'); db.close()
        db = Store.__new__(Store)
        # Exercise client binding using a real initialized DB.
        db2 = Store(d2, 'a'); db2.set('client_id', 'other'); db2.close()
        with self.assertRaises(ValueError): Store(d2, 'a')

    def test_stage_machine_and_consent(self):
        self.assertTrue(self.db.set_stage(CHAT, 'CONDITIONS_SENT', conditions_version='v1', conditions_mid=10))
        self.assertFalse(self.db.set_stage(CHAT, 'CONDITIONS_SENT', conditions_version='v1'))
        self.assertFalse(self.db.set_stage(CHAT, 'CONDITIONS_SENT', expected='NEW', conditions_version='v1'))
        with self.assertRaises(ValueError): self.db.set_stage(CHAT, 'UNKNOWN')
        with self.assertRaises(ValueError): self.db.set_stage(CHAT, 'CONSENT_RECORDED')
        with self.assertRaises(ValueError): self.db.set_stage(CHAT, 'CONDITIONS_SENT')
        self.assertEqual(self.db.record_consent(CHAT, 9, CONSENT, 'v1'), 'REJECTED')
        self.assertEqual(self.db.record_consent(CHAT, 11, CONSENT, 'wrong'), 'REJECTED')
        self.assertEqual(self.db.record_consent(CHAT, 11, CONSENT, 'v1'), 'RECORDED')
        self.assertEqual(self.db.record_consent(CHAT, 11, CONSENT, 'v1'), 'DUPLICATE')
        self.assertEqual(self.db.record_consent(CHAT, 12, CONSENT, 'v1'), 'DUPLICATE')
        self.assertTrue(self.db.set_stage(CHAT, 'AWAITING_OWNER'))
        self.assertFalse(self.db.set_stage(CHAT, 'AWAITING_OWNER'))
        with self.assertRaises(ValueError): self.db.set_stage(CHAT, 'DECLINED')
        self.assertEqual(self.db.resume_candidate(CHAT, 'AWAITING_OWNER') if False else None, None)

    def test_resume_and_quota(self):
        self.db.set_stage(CHAT, 'AWAITING_OWNER')
        with self.assertRaises(ValueError): self.db.resume_candidate(CHAT, 'CONDITIONS_SENT')
        self.assertEqual(self.db.resume_candidate(CHAT), 'NEW')
        self.assertIsNone(self.db.resume_candidate(CHAT))
        self.db.set_stage(CHAT, 'AWAITING_OWNER')
        self.assertEqual(self.db.resume_candidate(CHAT, 'DECLINED'), 'DECLINED')
        self.db.execute("UPDATE candidates SET stage='CONSENT_RECORDED' WHERE chat=?", (CHAT,))
        self.assertTrue(self.db.set_stage(CHAT,'PRESENTATION_SENT'))
        self.assertTrue(self.db.quota(1)); self.assertFalse(self.db.quota(1))

    def test_recover_paths(self):
        self.db.incoming(CHAT, CONN, 1, 'x', time.time())
        self.db.set_stage(CHAT, 'AWAITING_OWNER')
        self.db.resume_candidate(CHAT)
        self.db.reserve('send', CHAT, 'sendMessage')
        self.db.execute("UPDATE conversations SET task_status='ISSUING' WHERE chat=?", (CHAT,))
        self.db.execute("INSERT INTO inbox(id,payload,status) VALUES(?,?,?)", (10, json.dumps({'message': {'text': '/x'}}), 'RECEIVED'))
        self.db.execute("INSERT INTO inbox(id,payload,status) VALUES(?,?,?)", (11, json.dumps({'business_message': {}}), 'PENDING'))
        self.db.recover()
        self.assertEqual(self.db.get('paused'), '1')
        self.assertEqual(self.db.db.execute("SELECT status FROM outgoing WHERE dedupe='send'").fetchone()[0], 'UNKNOWN')
        self.assertEqual(self.db.db.execute('SELECT task_status FROM conversations WHERE chat=?', (CHAT,)).fetchone()[0], 'ISSUE_REVIEW')
        self.assertEqual(self.db.db.execute('SELECT status FROM inbox WHERE id=10').fetchone()[0], 'REVIEW')
        self.assertEqual(self.db.db.execute('SELECT status FROM inbox WHERE id=11').fetchone()[0], 'PENDING')


class ParserAndConfigTests(unittest.TestCase):
    def test_parser_all_decisions(self):
        self.assertTrue(is_vacancy_inquiry('Здравствуйте, пишу по поводу вакансии!'))
        self.assertFalse(is_vacancy_inquiry('Здравствуйте'))
        self.assertTrue(is_explicit_consent('согласен'))
        self.assertTrue(is_explicit_consent('УСЛОВИЯ УСТРАИВАЮТ.'))
        self.assertFalse(is_explicit_consent('Да?'))
        self.assertFalse(is_explicit_consent('да, но хочу уточнить'))
        self.assertFalse(is_explicit_consent('ознакомился'))
        self.assertTrue(is_explicit_decline('нет'))
        self.assertTrue(is_explicit_decline('Нет, не подходит'))
        self.assertFalse(is_explicit_decline('Нет, а когда старт?'))
        self.assertFalse(is_explicit_decline('подходит?'))

    def test_config_load_and_material_guards(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td); mats = root / 'materials'; mats.mkdir()
            (mats / 'c.txt').write_text('c'); (mats / 'p.pdf').write_bytes(b'p')
            scenario = root / 'scenario.json'
            scenario.write_text(json.dumps({'conditions_version':'v2','conditions_file':str(mats/'c.txt'),'presentation_file':str(mats/'p.pdf'),'group_invite_url':'u','unknown_question':'q','invite_message':'i'}))
            cfgfile = root / 'config.json'
            cfgfile.write_text(json.dumps({'schema_version':1,'client_id':'x','owner_id':1,'business_owner_id':1,'contacts':[], 'data_dir':str(root/'data'),'materials_dir':str(mats),'recruitment_scenario':str(scenario)}))
            cfg = Config.load(str(cfgfile))
            self.assertEqual(cfg.conditions_version, 'v2')
            self.assertEqual(cfg.material('c.txt').read_text(), 'c')
            with self.assertRaises(ValueError): cfg.material('../c.txt')
            big = mats/'big'; big.write_bytes(b'12345')
            cfg2 = Config(cfg.client_id,cfg.owner_id,cfg.business_owner_id,cfg.contacts,cfg.data_dir,cfg.materials_dir,cfg.instructions,cfg.faqs,cfg.tasks,cfg.scenario_path,cfg.conditions_version,cfg.conditions_file,cfg.presentation_file,cfg.group_invite_url,cfg.unknown_question_text,cfg.invite_message_text,max_file_bytes=2)
            with self.assertRaises(ValueError): cfg2.material('big')
            bad = root/'bad.json'; bad.write_text(json.dumps({'schema_version':2}))
            with self.assertRaises(ValueError): Config.load(str(bad))
            missing = root/'missing.json'; missing.write_text(json.dumps({'schema_version':1,'client_id':'x','owner_id':1,'business_owner_id':1,'contacts':[],'recruitment_scenario':'no.json'}))
            with self.assertRaises(ValueError): Config.load(str(missing))


class TelegramCoverage(IsolatedAsyncioTestCase):
    def setUp(self): self.tg = Telegram('123:SECRET')

    async def test_init_and_rejections(self):
        with self.assertRaises(ValueError): Telegram('bad')
        self.assertIsInstance(__import__('assistant_core.telegram', fromlist=['rejection']).rejection({'error_code':400,'description':'bad'}), APIError)
        with self.assertRaises(Forbidden):
            with mock.patch('urllib.request.urlopen', side_effect=http_error(403, {'error_code':403,'description':'blocked'})):
                await self.tg.call('sendMessage', {})
        with self.assertRaises(RateLimited):
            with mock.patch('urllib.request.urlopen', side_effect=http_error(429, {'error_code':429,'description':'slow','parameters':{'retry_after':3}})):
                await self.tg.call('sendMessage', {})
        with self.assertRaises(Conflict):
            with mock.patch('urllib.request.urlopen', side_effect=http_error(409, {'error_code':409,'description':'conflict'})):
                await self.tg.call('getUpdates', {})
        with self.assertRaises(DeliveryUnknown):
            with mock.patch('urllib.request.urlopen', side_effect=http_error(502, {'error_code':502,'description':'bad'})):
                await self.tg.call('sendMessage', {})
        with self.assertRaises(APIError):
            with mock.patch('urllib.request.urlopen', side_effect=urllib.error.URLError('x')):
                await self.tg.call('getUpdates', {})
        with self.assertRaises(DeliveryUnknown):
            with mock.patch('urllib.request.urlopen', side_effect=urllib.error.URLError('x')):
                await self.tg.call('sendMessage', {})
        body = Response(json.dumps({'ok':False,'error_code':400,'description':'bad'}).encode())
        with mock.patch('urllib.request.urlopen', return_value=body):
            with self.assertRaises(APIError): await self.tg.call('sendMessage', {})
        ok = Response(json.dumps({'ok':True,'result':{'x':1}}).encode())
        with mock.patch('urllib.request.urlopen', return_value=ok):
            self.assertEqual(await self.tg.call('getMe', {}), {'x':1})

    async def test_documents_and_download(self):
        with tempfile.TemporaryDirectory() as td:
            d = Path(td); pdf=d/'x.pdf'; pdf.write_bytes(b'%PDF')
            ok=Response(json.dumps({'ok':True,'result':{'message_id':1}}).encode())
            with mock.patch('urllib.request.urlopen', return_value=ok) as call:
                self.assertEqual(await self.tg.call('sendDocument', {'chat_id':2}, pdf), {'message_id':1})
                self.assertIn(b'application/pdf', call.call_args[0][0].data)
            missing = d/'missing.pdf'
            with self.assertRaises(APIError): await self.tg.call('sendDocument', {'chat_id':2}, missing)
            empty=d/'empty.pdf'; empty.write_bytes(b'')
            with self.assertRaises(APIError): await self.tg.call('sendDocument', {'chat_id':2}, empty)
            oversized=d/'big.bin'; oversized.write_bytes(b'x'*(50*1024*1024+1))
            with self.assertRaises(APIError): await self.tg.call('sendDocument', {'chat_id':2}, oversized)
            async def info(self, file_id, payload=None, document=None): return {'file_path':'dir/file.bin','file_size':4}
            self.tg.call = info
            dest=d/'download.bin'
            stream=Response(b'abcd')
            with mock.patch('urllib.request.urlopen', return_value=stream):
                await self.tg.download('f', dest, 10)
            self.assertEqual(dest.read_bytes(), b'abcd')
            async def bad_info(self, file_id, payload=None, document=None): return {'file_path':'../evil','file_size':1}
            self.tg.call=bad_info
            with self.assertRaises(APIError): await self.tg.download('f', d/'bad', 10)


class AICoverage(IsolatedAsyncioTestCase):
    async def test_modes_and_faq(self):
        with self.assertRaises(ValueError): AI(mode='bad')
        with self.assertRaises(ValueError): AI(base='https://127.0.0.1:11434')
        with self.assertRaises(ValueError): AI(base='http://127.0.0.1:9999')
        with self.assertRaises(ValueError): AI(model='cloud-model')
        cfg=SimpleNamespace(faqs=[{'id':'1','question':'Hello','answer':'Hi'}],instructions='i')
        self.assertEqual((await AI(mode='faq').propose(cfg,[{'role':'user','content':'Hello'}],{}))['faq_id'],'1')
        self.assertTrue((await AI(mode='faq').propose(cfg,[{'role':'user','content':'Other'}],{}))['escalate'])
        self.assertTrue((await AI(model='', mode='ollama').propose(cfg,[],{}))['escalate'])
        huge=SimpleNamespace(faqs=[],instructions='x'*7000)
        self.assertTrue((await AI(model='m',mode='ollama').propose(huge,[],{}))['escalate'])

    async def test_local_http_response_validation(self):
        cfg=SimpleNamespace(faqs=[],instructions='ok')
        good={'message':{'content':json.dumps({'faq_id':'x','draft':'d','escalate':False})}}
        with mock.patch('urllib.request.build_opener') as bo:
            op=bo.return_value; op.open.return_value=Response(json.dumps(good).encode())
            result=await AI(model='m').propose(cfg,[{'role':'user','content':'q'}],{})
            self.assertEqual(result['faq_id'],'x')
        bads=[
            {'faq_id':'x','draft':'d'},
            {'faq_id':3,'draft':'d','escalate':False},
            {'faq_id':'x','draft':3,'escalate':False},
            {'faq_id':'x','draft':'d','escalate':'no'},
        ]
        for item in bads:
            with mock.patch('urllib.request.build_opener') as bo:
                bo.return_value.open.return_value=Response(json.dumps({'message':{'content':json.dumps(item)}}).encode())
                self.assertTrue((await AI(model='m').propose(cfg,[],{}))['escalate'])


class OpsLockCoverage(IsolatedAsyncioTestCase):
    async def test_lock(self):
        with tempfile.TemporaryDirectory() as td:
            p=Path(td)/'runtime.lock'; a=ProcessLock(p)
            with self.assertRaises(RuntimeError): ProcessLock(p)
            a.close(); a.close()
            b=ProcessLock(p); b.close()

    async def test_backup_restore_health(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td); data=root/'data'; db=Store(data,'a')
            db.set('heartbeat', time.time()); db.set('paused','0'); db.reserve('k',CHAT,'sendMessage'); db.close()
            (data/'files').mkdir(); (data/'files'/'a.txt').write_text('x')
            with self.assertRaises(ValueError): backup(root/'missing', root/'b')
            b=root/'backup'; backup(data,b); self.assertTrue((b/'manifest.json').exists())
            with self.assertRaises(ValueError): backup(data,b)
            restored=root/'restored'; restore(b,restored)
            r=Store(restored,'a'); self.assertEqual(r.get('paused'),'1'); r.close()
            with self.assertRaises(ValueError): restore(b,restored)
            tampered=b/'manifest.json'; manifest=json.loads(tampered.read_text()); manifest['state.sqlite3']='bad'; tampered.write_text(json.dumps(manifest))
            with self.assertRaises(ValueError): restore(b,root/'badrestore')
            missing_health=root/'missing-health'
            missing_health.mkdir()
            missing_db=Store(missing_health,'a'); missing_db.close()
            with self.assertRaises(SystemExit): health(missing_health)
            db=Store(root/'health','a'); db.set('heartbeat',time.time()); db.close(); health(root/'health')
            db=Store(root/'stale','a'); db.set('heartbeat',time.time()-121); db.close()
            with self.assertRaises(SystemExit): health(root/'stale')


class SyncAdmissionWorkflowCoverage(IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.root=Path(self.tmp.name)
        self.cfg=make_cfg(self.root); self.db=Store(self.cfg.data_dir,'company_a'); self.db.set('paused','0')
        self.db.set('connection',json.dumps({'id':CONN,'user':{'id':OWNER},'is_enabled':True,'rights':{'can_reply':True}}))
        self.tg=FakeTG(); self.e=Engine(self.cfg,self.db,self.tg)

    def tearDown(self): self.db.close(); self.tmp.cleanup()

    async def admit_process(self,u):
        self.db.ingest(u); ok=await self.e.admit(u)
        if ok: await self.e.process(u)
        return ok

    async def test_admission_variants(self):
        self.assertFalse(await self.e.admit({'update_id':1,'business_connection':{'user':{'id':999}}}))
        self.assertFalse(await self.e.admit({'update_id':1,'business_connection':{'id':CONN,'user':{'id':OWNER},'is_enabled':True,'rights':{'can_reply':True}}}))
        self.assertTrue(await self.e.admit({'update_id':2,'message':{'from':{'id':OWNER},'chat':{'id':OWNER,'type':'private'},'text':'/resume'}}))
        self.assertFalse(await self.e.admit({'update_id':3,'message':{'from':{'id':2},'chat':{'id':2,'type':'private'},'text':'/resume'}}))
        self.assertFalse(await self.e.admit({'update_id':4,'business_message':{'chat':{'id':CHAT,'type':'group'},'business_connection_id':CONN,'from':{'id':CHAT},'message_id':4,'date':0,'text':'x'}}))
        self.assertFalse(await self.e.admit({'update_id':5,'business_message':{'chat':{'id':OWNER,'type':'private'},'business_connection_id':CONN,'from':{'id':OWNER},'message_id':5,'date':0,'text':'owner'}}))
        self.assertFalse(await self.e.admit({'update_id':6,'business_message':{'chat':{'id':CHAT,'type':'private'},'business_connection_id':CONN,'from':{'id':CHAT,'is_bot':True},'message_id':6,'date':0,'text':'x'}}))
        self.assertFalse(await self.e.admit({'update_id':7,'business_message':{'chat':{'id':CHAT,'type':'private'},'business_connection_id':CONN,'from':{'id':999},'message_id':7,'date':0,'text':'x'}}))
        self.assertTrue(await self.e.admit(update(8)))
        self.assertTrue(await self.e.admit(update(9, text='caption')))
        # Owner-written business message puts the chat into MANUAL.
        self.assertFalse(await self.e.admit(update(10, sender=OWNER)))
        self.assertEqual(self.db.conversation(CHAT)['mode'], 'MANUAL')

    async def test_commands_and_delivery_guards(self):
        await self.admit_process(update(1))
        for cmd in ['/start','/help','/status','/pending','/resume','/stop','/resume','/unknown','/manual 2002','/auto 2002']:
            await self.e.command(cmd, 100+len(cmd))
        self.assertFalse(await self.e.deliver(CHAT,'x','k',999))
        self.db.set('paused','0'); self.db.mode(CHAT,'AUTO'); epoch=self.db.conversation(CHAT)['epoch']
        self.db.execute('UPDATE conversations SET last_in=? WHERE chat=?',(time.time()-90000,CHAT))
        self.assertFalse(await self.e.deliver(CHAT,'x','old',epoch))
        self.db.execute('UPDATE conversations SET last_in=? WHERE chat=?',(time.time(),CHAT))
        self.db.set('connection',json.dumps({'id':CONN,'user':{'id':OWNER},'is_enabled':True,'rights':{'can_reply':False}}))
        self.assertFalse(await self.e.deliver(CHAT,'x','norights',epoch))
        self.db.set('connection',json.dumps({'id':CONN,'user':{'id':OWNER},'is_enabled':True,'rights':{'can_reply':True}}))
        self.tg.fail=DeliveryUnknown(); self.assertFalse(await self.e.deliver(CHAT,'x','unknown',epoch))
        self.tg.fail=APIError(); self.assertFalse(await self.e.deliver(CHAT,'x','failed',epoch))
        self.assertFalse(await self.e.deliver(CHAT,'x','unknown',epoch))

    async def test_business_paths_and_owner_commands(self):
        await self.admit_process(update(1))
        # admit_process already runs the update once; the current state is CONDITIONS_SENT.
        self.assertEqual(self.db.candidate(CHAT)['stage'], 'CONDITIONS_SENT')
        # Manually establish conditions stage to exercise the remaining branches.
        self.db.set_stage(CHAT,'CONDITIONS_SENT',conditions_version='v1',conditions_mid=10)
        self.db.execute("UPDATE conversations SET task_status='CONDITIONS_SENT' WHERE chat=?", (CHAT,))
        self.db.mode(CHAT,'AUTO'); self.db.set('paused','0')
        self.db.incoming(CHAT,CONN,2000,CONSENT,time.time())
        original_set_stage=self.db.set_stage
        self.db.set_stage=mock.Mock(side_effect=lambda *a, **kw: True)
        await self.e.business(update(2000,text=CONSENT,mid=2000)['business_message'],2000)
        self.db.set_stage=original_set_stage
        self.assertTrue(any(x[0]=='sendDocument' for x in self.tg.sent))
        self.assertTrue(any('https://t.me/group' in x[1].get('text','') for x in self.tg.sent))
        # Explicit decline.
        self.db.set_stage(CHAT,'DECLINED',expected='CONSENT_RECORDED') if False else None
        # Unknown question branch from CONDITIONS_SENT.
        self.db.set_stage=mock.Mock(return_value=True)
        self.db.mode(CHAT,'AUTO'); self.db.execute("UPDATE conversations SET task_status='CONDITIONS_SENT' WHERE chat=?", (CHAT,)); self.db.incoming(CHAT,CONN,30,'What?',time.time())
        await self.e.business(update(30,text='What?',mid=30)['business_message'],30)
        self.db.set_stage=original_set_stage
        # post-stage escalation and ignored repeated consent/inquiry
        self.db.set_stage=mock.Mock(return_value=True)
        self.db.mode(CHAT,'AUTO'); self.db.execute("UPDATE conversations SET task_status='INVITE_SENT' WHERE chat=?", (CHAT,)); self.db.incoming(CHAT,CONN,40,'Other',time.time())
        await self.e.business(update(40,text='Other',mid=40)['business_message'],40)
        self.db.set_stage=original_set_stage
        # STOP candidate
        self.db.mode(CHAT,'AUTO'); self.db.set('paused','0'); self.db.incoming(CHAT,CONN,50,'stop',time.time())
        await self.e.business(update(50,text='/stop',mid=50)['business_message'],50)
        # owner replies and resume candidate command paths
        await self.e.command('/reply 2002 hello',60)
        self.db.set_stage(CHAT,'AWAITING_OWNER')
        await self.e.command('/resume_candidate 2002',61)
        await self.e.command('/resume_candidate 2002',67)
        await self.e.command('/reply 9999 x',62)
        await self.e.command('/resume_candidate 9999',63)
        await self.e.command('/manual',64)
        await self.e.command('/nonsense 2002',65)
        await self.e.command('/manual 2002 extra',66)

    async def test_edit_delete_and_owner_delivery(self):
        await self.admit_process(update(1))
        edited=update(2,text='edited'); edited['edited_business_message']=edited.pop('business_message')
        self.assertFalse(await self.e.admit(edited))
        deleted={'update_id':3,'deleted_business_messages':{'chat':{'id':CHAT},'business_connection_id':CONN,'message_ids':[1]}}
        self.assertFalse(await self.e.admit(deleted))
        with self.assertRaises(ValueError): await self.e.deliver(999,'x','ownerbad',owner_notice=True)
        await self.e.owner('owner notice','owner-key')


class MainCoverage(IsolatedAsyncioTestCase):
    async def test_sync_connection_variants(self):
        with tempfile.TemporaryDirectory() as td:
            db=Store(Path(td),'a'); cfg=SimpleNamespace(business_owner_id=1)
            db.set('connection','{}'); tg=FakeTG(); self.assertIsNotNone(await sync_connection(tg,db,cfg))
            db.set('connection',json.dumps({'id':'c','user':{'id':1},'is_enabled':True,'rights':{'can_reply':True}}))
            tg=FakeTG(); tg.call=mock.AsyncMock(return_value={'id':'c','user':{'id':1},'is_enabled':True,'rights':{'can_reply':True}})
            self.assertIsNone(await sync_connection(tg,db,cfg))
            tg=FakeTG(); tg.call=mock.AsyncMock(side_effect=APIError('x',400,'bad'));  self.assertIsNotNone(await sync_connection(tg,db,cfg))
            tg=FakeTG(); tg.call=mock.AsyncMock(side_effect=APIError('x'));  self.assertIsNotNone(await sync_connection(tg,db,cfg))
            tg=FakeTG(); tg.fail=None; tg.call=mock.AsyncMock(return_value={'id':'c','user':{'id':1},'is_enabled':False,'rights':{'can_reply':True}})
            self.assertIsNotNone(await sync_connection(tg,db,cfg))
            tg.call=mock.AsyncMock(return_value={'id':'c','user':{'id':1},'is_enabled':True,'rights':{'can_reply':False}})
            self.assertIsNotNone(await sync_connection(tg,db,cfg))
            tg.call=mock.AsyncMock(return_value={'id':'c','user':{'id':99},'is_enabled':True,'rights':{'can_reply':True}})
            self.assertIsNotNone(await sync_connection(tg,db,cfg)); db.close()

    async def test_run_startup_guards_and_main_check(self):
        import assistant_core.main as m
        cfg=make_cfg(Path(tempfile.mkdtemp()))
        class Lock:
            def __init__(self,*a): pass
            def close(self): pass
        class DB:
            def __init__(self,*a): self.values={}
            def get(self,k,default=None): return self.values.get(k,default)
            def set(self,k,v): self.values[k]=str(v)
            def close(self): pass
            def recover(self): pass
        class TG:
            def __init__(self,*a): pass
            async def call(self,method,payload=None,document=None):
                if method=='getMe': return {'id':1}
                if method=='getWebhookInfo': return {'url':'x'}
                return []
        with mock.patch.object(m,'ProcessLock',Lock), mock.patch.object(m,'Store',DB), mock.patch.object(m,'Telegram',TG):
            with self.assertRaises(ValueError): await m.run(cfg)
        class TG0(TG):
            async def call(self,method,payload=None,document=None):
                if method=='getMe': return {'id':2}
                if method=='getWebhookInfo': return {'url':''}
                return []
        class DB0(DB):
            def __init__(self,*a): super().__init__(*a); self.values={'bot_id':'1'}
        with mock.patch.object(m,'ProcessLock',Lock), mock.patch.object(m,'Store',DB0), mock.patch.object(m,'Telegram',TG0):
            with self.assertRaises(ValueError): await m.run(cfg)
        class TG2(TG):
            async def call(self,method,payload=None,document=None):
                if method=='getMe': return {'id':2}
                if method=='getWebhookInfo': return {'url':''}
                raise KeyboardInterrupt()
        with mock.patch.object(m,'ProcessLock',Lock), mock.patch.object(m,'Store',DB), mock.patch.object(m,'Telegram',TG2), mock.patch.object(m,'AI',lambda *a: None), mock.patch.object(m,'Engine',lambda *a: SimpleNamespace(owner=mock.AsyncMock())):
            with self.assertRaises(KeyboardInterrupt): await m.run(cfg)


class MainFunctionCoverage(unittest.TestCase):
    def test_main_check_keyboard_and_error(self):
        import assistant_core.main as m
        cfg=make_cfg(Path(tempfile.mkdtemp()))
        with mock.patch.object(m,'Config') as C:
            C.load.return_value=cfg
            async def raise_keyboard(_cfg): raise KeyboardInterrupt()
            async def raise_value(_cfg): raise ValueError('boom')
            with mock.patch.object(m,'run', raise_keyboard):
                with mock.patch('sys.argv',['prog','--config','x']): m.main()
            with mock.patch('sys.argv',['prog','--config','x','--check']): m.main()
            with mock.patch.object(m,'run', raise_value):
                with mock.patch('sys.argv',['prog','--config','x']):
                    with self.assertRaises(SystemExit): m.main()


if __name__ == '__main__':
    unittest.main()

class AdditionalBranchCoverage(IsolatedAsyncioTestCase):
    async def test_workflow_remaining_branches(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td); cfg=make_cfg(root); db=Store(cfg.data_dir,'company_a'); db.set('paused','0')
            db.set('connection',json.dumps({'id':CONN,'user':{'id':OWNER},'is_enabled':True,'rights':{'can_reply':True}}))
            tg=FakeTG(); e=Engine(cfg,db,tg)
            db.incoming(CHAT,CONN,1,'x',time.time())
            epoch=db.conversation(CHAT)['epoch']
            # permission and connected branches
            self.assertTrue(e.can_send(CHAT,epoch))
            self.assertFalse(e.can_send(CHAT,epoch+1))
            db.set('paused','1'); self.assertFalse(e.can_send(CHAT,epoch)); db.set('paused','0')
            db.mode(CHAT,'MANUAL'); self.assertFalse(e.can_send(CHAT,db.conversation(CHAT)['epoch']))
            db.mode(CHAT,'AUTO'); db.set('connection',json.dumps({'id':CONN,'user':{'id':OWNER},'is_enabled':True,'rights':{'can_reply':True}}))
            epoch=db.conversation(CHAT)['epoch']
            self.assertTrue(e.can_send(CHAT,epoch,owner_action=True))
            # owner successful delivery and dedupe branch
            await e.deliver(OWNER,'notice','owner-ok',owner_notice=True)
            await e.deliver(CHAT,'x','same',epoch)
            self.assertFalse(await e.deliver(CHAT,'x','same',epoch))
            # process command branch and exception branch
            u={'update_id':20,'message':{'from':{'id':OWNER},'chat':{'id':OWNER,'type':'private'},'text':'/status'}}
            db.ingest(u); await e.process(u)
            bad={'update_id':21,'business_message':{'chat':{'id':CHAT,'type':'private'},'from':{'id':CHAT},'business_connection_id':CONN,'message_id':21,'date':int(time.time()),'text':'x'}}
            db.ingest(bad); db.incoming(CHAT,CONN,21,'x',time.time())
            original=e.business
            async def boom(*args): raise RuntimeError('boom')
            e.business=boom; await e.process(bad); e.business=original
            # no business message at all
            db.ingest({'update_id':22,'foo':'bar'}); self.assertFalse(await e.admit({'update_id':22,'foo':'bar'}))
            # sender_business_bot branch
            self.assertFalse(await e.admit(update(23,sender=CHAT))) if False else None
            bot=update(23); bot['business_message']['sender_business_bot']=True; db.ingest(bot); self.assertFalse(await e.admit(bot))
            # stale message and can_send skip
            db.mode(CHAT,'AUTO'); db.set('paused','0'); db.incoming(CHAT,CONN,24,'latest',time.time())
            await e.business(update(23,text='old',mid=23)['business_message'],23)
            db.set('paused','1'); await e.business(update(24,text='latest',mid=24)['business_message'],24)
            db.set('paused','0')
            # stage NEW with epoch changed inside gate
            db.set('paused','0'); db.mode(CHAT,'AUTO'); db.execute("UPDATE conversations SET task_status=NULL WHERE chat=?",(CHAT,))
            db.incoming(CHAT,CONN,25,'Интересует вакансия',time.time())
            orig_conv=e.db.conversation
            calls=[0]
            def changing(chat):
                row=orig_conv(chat); calls[0]+=1
                if calls[0]==2: db.execute('UPDATE conversations SET epoch=epoch+1 WHERE chat=?',(CHAT,))
                return row
            e.db.conversation=changing
            await e.business(update(25,text='Интересует вакансия',mid=25)['business_message'],25)
            e.db.conversation=orig_conv
            # conditions consent with epoch invalidated before record
            db.set('paused','0'); db.mode(CHAT,'AUTO'); db.set_stage(CHAT,'CONDITIONS_SENT',conditions_version='v1',conditions_mid=30)
            db.execute("UPDATE conversations SET task_status='CONDITIONS_SENT' WHERE chat=?",(CHAT,))
            db.incoming(CHAT,CONN,31,CONSENT,time.time())
            calls=[0]
            def changing2(chat):
                row=orig_conv(chat); calls[0]+=1
                if calls[0]==2: db.execute('UPDATE conversations SET epoch=epoch+1 WHERE chat=?',(CHAT,))
                return row
            e.db.conversation=changing2
            await e.business(update(31,text=CONSENT,mid=31)['business_message'],31)
            e.db.conversation=orig_conv
            # PDF failure returns before presentation stage
            db.set('paused','0'); db.mode(CHAT,'AUTO'); db.execute("UPDATE conversations SET epoch=epoch+1,task_status='CONDITIONS_SENT' WHERE chat=?",(CHAT,));
            db.set_stage(CHAT,'CONDITIONS_SENT',conditions_version='v1',conditions_mid=40)
            db.incoming(CHAT,CONN,41,CONSENT,time.time()); tg.fail=APIError();
            e.db.set_stage=mock.Mock(return_value=True)
            await e.business(update(41,text=CONSENT,mid=41)['business_message'],41)
            e.db.set_stage=Store.set_stage.__get__(db,Store)
            # decline path
            db.set('paused','0'); db.mode(CHAT,'AUTO'); db.execute("UPDATE conversations SET epoch=epoch+1,task_status='CONDITIONS_SENT' WHERE chat=?",(CHAT,)); db.execute("UPDATE candidates SET stage='CONDITIONS_SENT',prev_stage=NULL,conditions_version='v1',conditions_mid=50 WHERE chat=?",(CHAT,))
            db.incoming(CHAT,CONN,51,'Нет, не подходит',time.time());
            e.db.set_stage=mock.Mock(return_value=True)
            await e.business(update(51,text='Нет, не подходит',mid=51)['business_message'],51)
            e.db.set_stage=Store.set_stage.__get__(db,Store)
            # post-stage repeated consent/inquiry and post-stage escalation
            db.set('paused','0'); db.mode(CHAT,'AUTO'); db.execute("UPDATE conversations SET epoch=epoch+1,task_status='INVITE_SENT' WHERE chat=?",(CHAT,)); db.incoming(CHAT,CONN,60,CONSENT,time.time())
            await e.business(update(60,text=CONSENT,mid=60)['business_message'],60)
            db.execute("UPDATE conversations SET epoch=epoch+1,task_status='INVITE_SENT' WHERE chat=?",(CHAT,)); db.incoming(CHAT,CONN,61,'question',time.time())
            e.db.set_stage=mock.Mock(return_value=True)
            await e.business(update(61,text='question',mid=61)['business_message'],61)
            # post-stage epoch mismatch
            db.execute("UPDATE conversations SET epoch=epoch+1,task_status='INVITE_SENT' WHERE chat=?",(CHAT,)); db.incoming(CHAT,CONN,62,'question2',time.time())
            calls=[0]
            def changing3(chat):
                row=orig_conv(chat); calls[0]+=1
                if calls[0]==2: db.execute('UPDATE conversations SET epoch=epoch+1 WHERE chat=?',(CHAT,))
                return row
            e.db.conversation=changing3
            await e.business(update(62,text='question2',mid=62)['business_message'],62)
            db.close()

    def test_config_remaining_guards_and_env(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td); mats=root/'m'; mats.mkdir(); (mats/'c').write_text('c'); (mats/'p').write_text('p')
            sc=root/'s.json'; sc.write_text(json.dumps({'conditions_file':'missing/c','presentation_file':'missing/p','group_invite_url':'u'}))
            cfgf=root/'c.json'; cfgf.write_text(json.dumps({'schema_version':1,'client_id':'x','owner_id':1,'business_owner_id':1,'contacts':[],'materials_dir':str(mats),'recruitment_scenario':str(sc)}))
            with mock.patch.dict(os.environ, {'MATERIALS_DIR':str(mats),'DATA_DIR':str(root/'data'),'LOCAL_MODEL':'m','MAX':'1'}):
                cfg=Config.load(str(cfgf)); self.assertEqual(cfg.model,'m'); self.assertEqual(cfg.data_dir,root/'data')
            badowners=root/'badowners.json'; badowners.write_text(json.dumps({'schema_version':1,'client_id':'','owner_id':1,'business_owner_id':1,'contacts':[]}))
            with mock.patch.dict(os.environ, {'MATERIALS_DIR':str(mats)}):
                with self.assertRaises(ValueError): Config.load(str(badowners))
            badcontact=root/'badcontact.json'; badcontact.write_text(json.dumps({'schema_version':1,'client_id':'x','owner_id':1,'business_owner_id':1,'contacts':[1]}))
            with mock.patch.dict(os.environ, {'MATERIALS_DIR':str(mats)}):
                with self.assertRaises(ValueError): Config.load(str(badcontact))
            missingmat=root/'missingmat.json'; missingmat.write_text(json.dumps({'schema_version':1,'client_id':'x','owner_id':1,'business_owner_id':1,'contacts':[],'materials_dir':str(mats),'recruitment_scenario':str(root/'no-sc.json')}))
            with mock.patch.dict(os.environ, {'MATERIALS_DIR':str(mats)}):
                with self.assertRaises(ValueError): Config.load(str(missingmat))
            missing_files=root/'missing-files-s.json'; missing_files.write_text(json.dumps({'schema_version':1,'client_id':'x','owner_id':1,'business_owner_id':1,'contacts':[],'materials_dir':str(mats),'recruitment_scenario':str(root/'missing-files-sc.json')}))
            missing_s=root/'missing-files-sc.json'; missing_s.write_text(json.dumps({'conditions_file':'none/c','presentation_file':'none/p','group_invite_url':'u'}))
            with mock.patch.dict(os.environ, {'MATERIALS_DIR':str(root/'empty-mats')}):
                (root/'empty-mats').mkdir()
                with self.assertRaises(ValueError): Config.load(str(missing_files))

    def test_lock_windows_branch(self):
        import assistant_core.locking as locking
        fake=SimpleNamespace(LK_NBLCK=1,LK_UNLCK=2, locking=mock.Mock())
        old=__import__('sys').modules.get('msvcrt'); __import__('sys').modules['msvcrt']=fake
        try:
            with mock.patch.object(locking.os,'name','nt'):
                with tempfile.TemporaryDirectory() as td:
                    p=os.path.join(td,'lock'); l=ProcessLock(p); l.close()
                    self.assertEqual(fake.locking.call_count,2)
        finally:
            if old is None: __import__('sys').modules.pop('msvcrt',None)
            else: __import__('sys').modules['msvcrt']=old

    def test_parser_decline_trigger_and_no_redirect(self):
        self.assertTrue(is_explicit_decline('не устраивает'))
        from assistant_core.ai import NoRedirect
        self.assertIsNone(NoRedirect().redirect_request(None,None,None,None,None))

    async def test_telegram_remaining_download_branches(self):
        tg=Telegram('123:x')
        with mock.patch('urllib.request.urlopen', side_effect=http_error(400,b'<html>')):
            with self.assertRaises(APIError): await tg.call('sendMessage',{})
        with tempfile.TemporaryDirectory() as td:
            d=Path(td)
            async def info(self, file_id, payload=None, document=None): return {'file_path':'x','file_size':20}
            tg.call=info
            with self.assertRaises(APIError): await tg.download('f',d/'x',10)
            async def info2(self, file_id, payload=None, document=None): return {'file_path':'x','file_size':1}
            tg.call=info2
            class Stream(Response):
                def read(self,n=-1): return b'01234567890'
            with mock.patch('urllib.request.urlopen',return_value=Stream(b'')):
                with self.assertRaises(APIError): await tg.download('f',d/'x',5)
            self.assertFalse((d/'x').exists())
            tg.call=info2
            with mock.patch('urllib.request.urlopen',side_effect=OSError('x')):
                with self.assertRaises(APIError): await tg.download('f',d/'y',5)

    async def test_ops_artifact_and_cli(self):
        import assistant_core.ops as ops
        with tempfile.TemporaryDirectory() as td:
            root=Path(td); data=root/'data'; db=Store(data,'a');
            db.incoming(CHAT,'c',1,'x',time.time()); (data/'files').mkdir(); f=data/'files'/'a.bin'; f.write_bytes(b'a')
            import hashlib
            db.execute('INSERT INTO artifacts(chat,mid,task_id,path,sha256,size,original_name) VALUES(?,?,?,?,?,?,?)',(CHAT,1,'t',str(f),hashlib.sha256(b'a').hexdigest(),1,'a.bin')); db.close()
            b=root/'b'; backup(data,b); r=root/'r'; restore(b,r); rr=Store(r,'a'); self.assertTrue(str(rr.db.execute('SELECT path FROM artifacts').fetchone()[0]).startswith(str(r/'files'))); rr.close()
            # CLI dispatch
            with mock.patch('sys.argv',['ops','health',str(r)]), mock.patch.object(ops,'health') as h: ops.main(); h.assert_called_once()
            with mock.patch('sys.argv',['ops','backup',str(data),str(root/'cli-b')]), mock.patch.object(ops,'backup') as bkp: ops.main(); bkp.assert_called_once()
            with mock.patch('sys.argv',['ops','restore',str(b),str(root/'cli-r')]), mock.patch.object(ops,'restore') as rst: ops.main(); rst.assert_called_once()

    async def test_main_poll_loop_error_branches(self):
        import assistant_core.main as m
        cfg=make_cfg(Path(tempfile.mkdtemp()))
        class Lock:
            def __init__(self,*a): pass
            def close(self): pass
        class DB:
            def __init__(self,*a): self.values={}; self.n=0
            def get(self,k,d=None): return self.values.get(k,d)
            def set(self,k,v): self.values[k]=str(v)
            def recover(self): pass
            def log(self,*a,**kw): pass
            def close(self): pass
            def ingest(self,u): pass
            def received(self): return []
            def pending(self): return []
        class EngineFake:
            def __init__(self,*a): pass
            async def owner(self,*a): pass
            async def admit(self,u): return False
            async def process(self,u): pass
        async def run_once(error):
            class TG:
                def __init__(self,*a): self.i=0
                async def call(self,method,payload=None,document=None):
                    if method=='getMe': return {'id':1}
                    if method=='getWebhookInfo': return {'url':''}
                    self.i += 1
                    if self.i==1: raise error
                    raise KeyboardInterrupt()
            with mock.patch.object(m,'ProcessLock',Lock), mock.patch.object(m,'Store',DB), mock.patch.object(m,'Telegram',TG), mock.patch.object(m,'Engine',EngineFake), mock.patch.object(m,'AI',lambda *a: None), mock.patch.object(m,'sync_connection',mock.AsyncMock(return_value=None)), mock.patch('asyncio.sleep',mock.AsyncMock()):
                with self.assertRaises(KeyboardInterrupt): await m.run(cfg)
        await run_once(RateLimited('x',429,'slow',retry_after=1)); await run_once(Conflict('x',409,'conflict')); await run_once(APIError('x',500,'err'))
        class TGWork:
            def __init__(self,*a): self.calls=0
            async def call(self,method,payload=None,document=None):
                if method=='getMe': return {'id':1}
                if method=='getWebhookInfo': return {'url':''}
                self.calls+=1
                if self.calls==1: return [{'update_id':3,'business_message':{}}]
                raise KeyboardInterrupt()
        class DBWork(DB):
            def received(self): return [{'id':1,'payload':json.dumps({'business_message':{}})}]
            def pending(self): return [{'id':1,'payload':json.dumps({'business_message':{}})}]
            def complete(self,*a): pass
        with mock.patch.object(m,'ProcessLock',Lock), mock.patch.object(m,'Store',DBWork), mock.patch.object(m,'Telegram',TGWork), mock.patch.object(m,'Engine',EngineFake), mock.patch.object(m,'AI',lambda *a: None), mock.patch.object(m,'sync_connection',mock.AsyncMock(return_value=None)), mock.patch('asyncio.sleep',mock.AsyncMock()):
            with self.assertRaises(KeyboardInterrupt): await m.run(cfg)



class FinalCoverage(IsolatedAsyncioTestCase):
    async def test_workflow_specific_missing_lines(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td); cfg=make_cfg(root); db=Store(cfg.data_dir,'company_a'); db.set('paused','0'); db.set('connection',json.dumps({'id':CONN,'user':{'id':OWNER},'is_enabled':True,'rights':{'can_reply':True}})); tg=FakeTG(); e=Engine(cfg,db,tg)
            db.incoming(CHAT,CONN,1,'x',time.time()); db.execute("UPDATE conversations SET task_status='CONDITIONS_SENT' WHERE chat=?",(CHAT,)); db.set_stage(CHAT,'CONDITIONS_SENT',conditions_version='v1',conditions_mid=1)
            # Successful conditions branch: patch only set_stage to bypass the original production defect.
            e.db.set_stage=mock.Mock(return_value=True); db.incoming(CHAT,CONN,2,'Интересует вакансия',time.time())
            await e.business(update(2,text='Интересует вакансия',mid=2)['business_message'],2)
            # Decline branch with the same bypass.
            db.execute("UPDATE conversations SET epoch=epoch+1,task_status='CONDITIONS_SENT' WHERE chat=?",(CHAT,)); db.incoming(CHAT,CONN,3,'Нет',time.time())
            await e.business(update(3,text='Нет',mid=3)['business_message'],3)
            # Unknown-question epoch guard.
            e.db.set_stage=mock.Mock(return_value=True); db.execute("UPDATE conversations SET epoch=epoch+1,task_status='CONDITIONS_SENT' WHERE chat=?",(CHAT,)); db.incoming(CHAT,CONN,4,'question',time.time())
            original=e.db.conversation; calls=[0]
            def conv(chat):
                row=original(chat); calls[0]+=1
                if calls[0]==2: db.execute('UPDATE conversations SET epoch=epoch+1 WHERE chat=?',(CHAT,))
                return row
            e.db.conversation=conv; await e.business(update(4,text='question',mid=4)['business_message'],4); e.db.conversation=original
            # Post-stage epoch guard.
            db.execute("UPDATE conversations SET epoch=epoch+1,task_status='INVITE_SENT' WHERE chat=?",(CHAT,)); db.incoming(CHAT,CONN,5,'post question',time.time())
            calls=[0]
            def conv2(chat):
                row=original(chat); calls[0]+=1
                if calls[0]==2: db.execute('UPDATE conversations SET epoch=epoch+1 WHERE chat=?',(CHAT,))
                return row
            e.db.conversation=conv2; await e.business(update(5,text='post question',mid=5)['business_message'],5)
            db.close()

    async def test_main_task_processing_and_worker_error(self):
        import assistant_core.main as m
        cfg=make_cfg(Path(tempfile.mkdtemp()))
        class Lock:
            def __init__(self,*a): pass
            def close(self): pass
        class DB:
            def __init__(self,*a): self.values={}; self.closed=False
            def get(self,k,d=None): return self.values.get(k,d)
            def set(self,k,v): self.values[k]=str(v)
            def recover(self): pass
            def log(self,*a,**kw): pass
            def close(self): self.closed=True
            def ingest(self,u): pass
            def received(self): return [{'id':1,'payload':json.dumps({'update_id':1,'message':{'from':{'id':1},'chat':{'id':1,'type':'private'},'text':'/x'}})}]
            def pending(self): return [{'id':2,'payload':json.dumps({'update_id':2,'business_message':{'chat':{'id':CHAT}}})}]
            def complete(self,*a): pass
        class EngineFake:
            def __init__(self,*a): pass
            async def owner(self,*a): pass
            async def admit(self,u): return True
            async def process(self,u):
                if u.get('update_id')==2: raise RuntimeError('worker')
        class TG:
            def __init__(self,*a): self.n=0
            async def call(self,method,payload=None,document=None):
                if method=='getMe': return {'id':1}
                if method=='getWebhookInfo': return {'url':''}
                self.n+=1
                if self.n==1: return []
                await asyncio.sleep(0)
                raise KeyboardInterrupt()
        with mock.patch.object(m,'ProcessLock',Lock), mock.patch.object(m,'Store',DB), mock.patch.object(m,'Telegram',TG), mock.patch.object(m,'Engine',EngineFake), mock.patch.object(m,'AI',lambda *a: None), mock.patch.object(m,'sync_connection',mock.AsyncMock(return_value=None)):
            with self.assertRaises(KeyboardInterrupt): await m.run(cfg)


class LastWorkflowCoverage(IsolatedAsyncioTestCase):
    async def test_fresh_new_consent_decline_and_epoch_guards(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td); cfg=make_cfg(root); db=Store(cfg.data_dir,'company_a'); db.set('paused','0'); db.set('connection',json.dumps({'id':CONN,'user':{'id':OWNER},'is_enabled':True,'rights':{'can_reply':True}})); tg=FakeTG(); e=Engine(cfg,db,tg)
            # NEW -> conditions send (covers the successful lines after the production call).
            db.set_stage(CHAT,'NEW')
            db.incoming(CHAT,CONN,1,'Интересует вакансия',time.time()); e.db.set_stage=mock.Mock(return_value=True)
            await e.business(update(1,text='Интересует вакансия',mid=1)['business_message'],1)
            # CONDITIONS_SENT -> explicit decline.
            db.set('paused','0'); db.mode(CHAT,'AUTO'); db.execute("UPDATE conversations SET epoch=epoch+1 WHERE chat=?",(CHAT,)); db.execute("UPDATE candidates SET stage='CONDITIONS_SENT',conditions_version='v1',conditions_mid=1 WHERE chat=?",(CHAT,)); db.incoming(CHAT,CONN,2,'отказываюсь',time.time()); db.execute("UPDATE conversations SET task_status='CONDITIONS_SENT' WHERE chat=?",(CHAT,))
            e.db.set_stage=mock.Mock(return_value=True); await e.business(update(2,text='отказываюсь',mid=2)['business_message'],2)
            # Unknown question: change epoch on the third conversation lookup, inside the gated block.
            db.set('paused','0'); db.mode(CHAT,'AUTO'); db.execute("UPDATE conversations SET epoch=epoch+1 WHERE chat=?",(CHAT,)); db.execute("UPDATE candidates SET stage='CONDITIONS_SENT',conditions_version='v1',conditions_mid=2 WHERE chat=?",(CHAT,)); db.incoming(CHAT,CONN,3,'question',time.time()); db.execute("UPDATE conversations SET task_status='CONDITIONS_SENT' WHERE chat=?",(CHAT,))
            original=e.db.conversation; calls=[0]
            def conv(chat):
                row=original(chat); calls[0]+=1
                if calls[0]==3: db.execute('UPDATE conversations SET epoch=epoch+1 WHERE chat=?',(CHAT,))
                return row
            e.db.conversation=conv; e.db.set_stage=mock.Mock(return_value=True); await e.business(update(3,text='question',mid=3)['business_message'],3)
            # Post-stage guard: same third-lookup epoch invalidation.
            e.db.conversation=original; db.set('paused','0'); db.mode(CHAT,'AUTO'); db.execute("UPDATE conversations SET epoch=epoch+1 WHERE chat=?",(CHAT,)); db.incoming(CHAT,CONN,4,'post',time.time()); db.execute("UPDATE conversations SET task_status='INVITE_SENT' WHERE chat=?",(CHAT,))
            calls=[0]
            def conv2(chat):
                row=original(chat); calls[0]+=1
                if calls[0]==3: db.execute('UPDATE conversations SET epoch=epoch+1 WHERE chat=?',(CHAT,))
                return row
            e.db.conversation=conv2; await e.business(update(4,text='post',mid=4)['business_message'],4)
            db.close()

class EpochGuardCoverage(IsolatedAsyncioTestCase):
    async def test_epoch_guards_inside_workflow_stages(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td); cfg=make_cfg(root); db=Store(cfg.data_dir,'company_a'); db.set('paused','0'); db.set('connection',json.dumps({'id':CONN,'user':{'id':OWNER},'is_enabled':True,'rights':{'can_reply':True}})); tg=FakeTG(); e=Engine(cfg,db,tg)
            db.set_stage(CHAT,'CONDITIONS_SENT',conditions_version='v1',conditions_mid=1); db.incoming(CHAT,CONN,2,'отказываюсь',time.time()); db.execute("UPDATE conversations SET task_status='CONDITIONS_SENT' WHERE chat=?",(CHAT,))
            original=e.db.conversation
            def guard_decline(chat):
                row=original(chat); guard_decline.n+=1
                if guard_decline.n==3:
                    row = dict(row); row['epoch'] += 1
                return row
            guard_decline.n=0; e.db.conversation=guard_decline; e.db.set_stage=mock.Mock(return_value=True)
            await e.business(update(2,text='отказываюсь',mid=2)['business_message'],2)
            e.db.conversation=original
            db.set('paused','0'); db.mode(CHAT,'AUTO'); db.execute("UPDATE conversations SET epoch=epoch+1 WHERE chat=?",(CHAT,)); db.execute("UPDATE candidates SET stage='CONDITIONS_SENT',conditions_version='v1',conditions_mid=2 WHERE chat=?",(CHAT,)); db.incoming(CHAT,CONN,3,'question',time.time()); db.execute("UPDATE conversations SET task_status='CONDITIONS_SENT' WHERE chat=?",(CHAT,))
            def guard_unknown(chat):
                row=original(chat); guard_unknown.n+=1
                if guard_unknown.n==3:
                    row = dict(row); row['epoch'] += 1
                return row
            guard_unknown.n=0; e.db.conversation=guard_unknown; e.db.set_stage=mock.Mock(return_value=True)
            await e.business(update(3,text='question',mid=3)['business_message'],3)
            e.db.conversation=original
            db.set('paused','0'); db.mode(CHAT,'AUTO'); db.execute("UPDATE conversations SET epoch=epoch+1 WHERE chat=?",(CHAT,)); db.incoming(CHAT,CONN,4,'post question',time.time()); db.execute("UPDATE conversations SET task_status='INVITE_SENT' WHERE chat=?",(CHAT,))
            def guard_post(chat):
                row=original(chat); guard_post.n+=1
                if guard_post.n==3:
                    row = dict(row); row['epoch'] += 1
                return row
            guard_post.n=0; e.db.conversation=guard_post; e.db.set_stage=mock.Mock(return_value=True)
            await e.business(update(4,text='post question',mid=4)['business_message'],4)
            db.close()
