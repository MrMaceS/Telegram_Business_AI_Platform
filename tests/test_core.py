import asyncio
import hashlib
import json
import tempfile
import time
import unittest
from pathlib import Path
from assistant_core.config import Config
from assistant_core.storage import Store
from assistant_core.workflow import Engine
from assistant_core.telegram import APIError, DeliveryUnknown
from assistant_core.ops import backup, restore

class FakeTelegram:
    def __init__(self):
        self.sent=[];self.calls=0;self.fail_on=None;self.unknown=False
    async def call(self,method,payload=None,document=None):
        self.calls+=1
        if self.calls==self.fail_on:
            raise DeliveryUnknown() if self.unknown else APIError()
        self.sent.append((method,payload,document))
        return {'message_id':1000+self.calls}
    async def download(self,file_id,destination,limit):
        destination.write_bytes(b'opaque archive, never execute')

class FakeAI:
    async def propose(self,*args):
        return {'faq_id':'faq','draft':'DO NOT SEND MODEL TEXT','escalate':False}

class CoreTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        mat=self.root/'materials';mat.mkdir();(mat/'task.txt').write_text('task')
        self.cfg=Config('a',1,1,(2,),self.root/'data',mat,'rules',
            ({'id':'faq','answer':'Approved answer'},),{'T':{'brief':'Brief','files':['task.txt']}})
        self.db=Store(self.cfg.data_dir,'a');self.tg=FakeTelegram()
        self.e=Engine(self.cfg,self.db,self.tg,FakeAI());self.db.set('paused','0')
        self.db.set('connection',json.dumps({'id':'c','user':{'id':1},'is_enabled':True,'rights':{'can_reply':True}}))
    def tearDown(self):self.db.close();self.tmp.cleanup()
    def update(self,uid=1,text='Question?',document=None):
        m={'message_id':uid,'date':int(time.time()),'chat':{'id':2,'type':'private'},'from':{'id':2},'business_connection_id':'c','text':text}
        if document:m['document']=document
        return {'update_id':uid,'business_message':m}
    async def admitted(self,u):
        self.assertTrue(self.db.ingest(u));self.assertTrue(await self.e.admit(u));return u
    def business_sends(self):return [x for x in self.tg.sent if x[1]['chat_id']==2]

    async def test_ingest_deduplicates_and_persists_offset(self):
        u=self.update(7);self.assertTrue(self.db.ingest(u));self.assertFalse(self.db.ingest(u));self.assertEqual(self.db.get('offset'),'8')
    async def test_unknown_contact_and_connection_denied(self):
        u=self.update();u['business_message']['from']['id']=3
        self.assertFalse(await self.e.admit(u));self.assertIsNone(self.db.conversation(2))
        u=self.update();u['business_message']['business_connection_id']='other'
        self.assertFalse(await self.e.admit(u))
    async def test_unauthorized_owner_commands_rejected(self):
        u={'update_id':8,'message':{'from':{'id':2},'chat':{'id':2,'type':'private'},'text':'/resume'}}
        self.db.set('paused','1');self.assertFalse(await self.e.admit(u));self.assertEqual(self.db.get('paused'),'1')
    async def test_only_approved_faq_is_sent(self):
        u=await self.admitted(self.update());await self.e.process(u)
        texts=[x[1].get('text','') for x in self.business_sends()]
        self.assertIn('Approved answer',texts);self.assertNotIn('DO NOT SEND MODEL TEXT',texts)
    async def test_critical_request_escalates_without_model_commitment(self):
        u=await self.admitted(self.update(text='Перенос срока и цена?'));await self.e.process(u)
        self.assertEqual(self.db.conversation(2)['mode'],'AWAITING_OWNER')
        self.assertNotIn('Approved answer',[x[1].get('text') for x in self.business_sends()])
    async def test_stop_during_ai_and_resume_invalidates_generation(self):
        started=asyncio.Event();release=asyncio.Event()
        class SlowAI:
            async def propose(inner,*args):
                started.set();await release.wait();return {'faq_id':'faq','draft':'bad','escalate':False}
        self.e.ai=SlowAI();u=await self.admitted(self.update());job=asyncio.create_task(self.e.process(u))
        await started.wait();await self.e.command('/stop',10);await self.e.command('/resume',11)
        release.set();await job
        self.assertNotIn('Approved answer',[x[1].get('text') for x in self.business_sends()])
    async def test_stop_send_gate(self):
        await self.admitted(self.update());epoch=self.db.conversation(2)['epoch']
        await self.e.command('/stop',10)
        self.assertFalse(await self.e.deliver(2,'late','late',epoch))
        self.assertEqual(len(self.business_sends()),0)
    async def test_window_and_rights_enforced(self):
        await self.admitted(self.update());epoch=self.db.conversation(2)['epoch']
        self.db.execute('UPDATE conversations SET last_in=?',(time.time()-86401,))
        self.assertFalse(await self.e.deliver(2,'late','late',epoch))
    async def test_uncertain_send_never_retries(self):
        await self.admitted(self.update());epoch=self.db.conversation(2)['epoch']
        self.tg.fail_on=1;self.tg.unknown=True
        self.assertFalse(await self.e.deliver(2,'x','once',epoch))
        self.assertFalse(await self.e.deliver(2,'x','once',epoch))
        self.assertEqual(self.tg.calls,1)
        self.assertEqual(self.db.db.execute('SELECT status FROM outgoing').fetchone()[0],'UNKNOWN')
    async def test_partial_issue_blocks_reissue(self):
        await self.admitted(self.update());await self.e.command('/assign 2 T',10)
        self.tg.fail_on=self.tg.calls+2;self.tg.unknown=True
        await self.e.command('/issue 2',11)
        self.assertEqual(self.db.conversation(2)['task_status'],'ISSUE_REVIEW')
        before=len(self.business_sends());await self.e.command('/issue 2',12)
        self.assertEqual(len(self.business_sends()),before)
    async def test_manual_file_receipt_persists_without_auto_reply(self):
        u=await self.admitted(self.update(document={'file_id':'f','file_name':'../../evil.zip','file_size':10}))
        self.db.mode(2,'MANUAL');await self.e.process(u)
        row=self.db.db.execute('SELECT * FROM artifacts').fetchone()
        self.assertTrue(Path(row['path']).is_relative_to(self.cfg.data_dir))
        self.assertEqual(row['sha256'],hashlib.sha256(Path(row['path']).read_bytes()).hexdigest())
        self.assertFalse(self.business_sends())
    async def test_new_message_invalidates_old_epoch(self):
        await self.admitted(self.update(1));old=self.db.conversation(2)['epoch'];await self.admitted(self.update(2))
        self.assertFalse(await self.e.deliver(2,'stale','stale',old))
    async def test_edit_pauses_and_rebuilds_history(self):
        await self.admitted(self.update());u={'update_id':2,'edited_business_message':dict(self.update()['business_message'],text='edited')}
        self.assertFalse(await self.e.admit(u));self.assertEqual(self.db.conversation(2)['mode'],'AWAITING_OWNER')
        self.assertEqual(self.db.history(2)[0]['content'],'edited')
    async def test_instance_isolation_and_database_binding(self):
        b=Store(self.root/'b','b')
        try:
            await self.admitted(self.update());self.assertIsNone(b.conversation(2))
            b.set('paused','0');await self.e.command('/stop',10);self.assertEqual(b.get('paused'),'0')
        finally:b.close()
        with self.assertRaises(ValueError):Store(self.cfg.data_dir,'b')
    async def test_path_escape_rejected(self):
        with self.assertRaises(ValueError):self.cfg.material('../outside.txt')
    async def test_daily_quota_persists(self):
        self.assertTrue(self.db.quota(1));self.assertFalse(self.db.quota(1))
    async def test_burst_answers_latest_context_once(self):
        first=await self.admitted(self.update(1,text='Question A'))
        last=await self.admitted(self.update(2,text='Question B'))
        await self.e.process(first);await self.e.process(last)
        self.assertEqual(sum(x[1].get('text')=='Approved answer' for x in self.business_sends()),1)
    async def test_restart_between_receipt_and_admission(self):
        u=self.update(9);self.db.ingest(u);self.db.recover()
        row=self.db.received()[0];self.assertEqual(row['id'],9)
        self.assertTrue(await self.e.admit(json.loads(row['payload'])))
        self.db.complete(9,'PENDING')
        epoch=self.db.conversation(2)['epoch']
        self.assertTrue(await self.e.admit(u))
        self.assertEqual(self.db.conversation(2)['epoch'],epoch)
        self.assertEqual(len(self.db.history(2)),1)
        self.assertEqual(self.db.pending()[0]['id'],9)
    async def test_download_cannot_reopen_accepted_task(self):
        u=await self.admitted(self.update(document={'file_id':'f','file_size':10}))
        await self.e.command('/assign 2 T',10)
        started=asyncio.Event();release=asyncio.Event()
        async def slow_download(file_id,destination,limit):
            started.set();await release.wait();destination.write_bytes(b'file')
        self.tg.download=slow_download
        job=asyncio.create_task(self.e.process(u));await started.wait()
        await self.e.command('/accept 2',11);release.set();await job
        self.assertEqual(self.db.conversation(2)['task_status'],'ACCEPTED')
    async def test_second_process_lock_is_rejected(self):
        from assistant_core.locking import ProcessLock
        first=ProcessLock(self.root/'runtime.lock')
        try:
            with self.assertRaises(RuntimeError):ProcessLock(self.root/'runtime.lock')
        finally:first.close()
        second=ProcessLock(self.root/'runtime.lock');second.close()
    async def test_restart_and_restore_are_paused(self):
        await self.admitted(self.update());self.db.mode(2,'MANUAL');self.db.reserve('maybe',2,'sendMessage')
        self.db.recover();self.assertEqual(self.db.get('paused'),'1');self.assertEqual(self.db.conversation(2)['mode'],'MANUAL')
        destination=self.root/'backup';backup(self.cfg.data_dir,destination)
        restored=self.root/'restored';restore(destination,restored);b=Store(restored,'a')
        try:
            self.assertEqual(b.get('paused'),'1')
            self.assertEqual(b.db.execute('SELECT status FROM outgoing').fetchone()[0],'UNKNOWN')
        finally:b.close()

class LocalAITests(unittest.IsolatedAsyncioTestCase):
    async def test_cloud_endpoint_and_model_rejected(self):
        from assistant_core.ai import AI
        with self.assertRaises(ValueError):AI('x',base='https://api.openai.com')
        with self.assertRaises(ValueError):AI('x-cloud')
        with self.assertRaises(ValueError):AI('x',mode='paid')
    async def test_faq_mode_has_no_ai_network_call(self):
        from assistant_core.ai import AI
        from types import SimpleNamespace
        cfg=SimpleNamespace(faqs=[{'id':'a','question':'Как сдать?','answer':'Документом'}])
        result=await AI(mode='faq').propose(cfg,[{'role':'user','content':'Как сдать?'}],{})
        self.assertEqual(result['faq_id'],'a');self.assertFalse(result['escalate'])
