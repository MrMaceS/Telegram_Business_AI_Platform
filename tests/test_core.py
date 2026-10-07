import hashlib
import json
import tempfile
import time
import unittest
from pathlib import Path

from assistant_core.config import Config
from assistant_core.locking import ProcessLock
from assistant_core.storage import Store
from assistant_core.telegram import APIError, DeliveryUnknown
from assistant_core.workflow import Engine


class FakeTelegram:
    def __init__(self):
        self.sent = []
        self.calls = 0
        self.fail_on = None
        self.unknown = False

    async def call(self, method, payload=None, document=None):
        self.calls += 1
        if self.calls == self.fail_on:
            raise DeliveryUnknown() if self.unknown else APIError()
        self.sent.append((method, payload or {}, document))
        return {'message_id': 1000 + self.calls}

    async def download(self, file_id, destination, limit):
        destination.write_bytes(b'file')


class CoreTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        materials = self.root / 'materials'
        materials.mkdir()
        (materials / 'task.txt').write_text('task', encoding='utf-8')
        (materials / 'vacancy_conditions.txt').write_text('CONDITIONS', encoding='utf-8')
        (materials / 'company_presentation.pdf').write_bytes(b'%PDF-1.4 test')
        self.cfg = Config(
            client_id='a', owner_id=1, business_owner_id=1, contacts=(2,),
            data_dir=self.root / 'data', materials_dir=materials,
            instructions='rules', faqs=(), tasks={},
            scenario_path=self.root / 'scenario.json',
            conditions_version='v1',
            conditions_file=materials / 'vacancy_conditions.txt',
            presentation_file=materials / 'company_presentation.pdf',
            group_invite_url='https://t.me/test',
            unknown_question_text='Передам владельцу.',
            invite_message_text='invite',
        )
        self.db = Store(self.cfg.data_dir, 'a')
        self.tg = FakeTelegram()
        self.e = Engine(self.cfg, self.db, self.tg)
        self.db.set('paused', '0')
        self.db.set('connection', json.dumps({
            'id': 'c', 'user': {'id': 1}, 'is_enabled': True,
            'rights': {'can_reply': True},
        }))

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def update(self, uid=1, text='Интересует вакансия', mid=None, chat=2, sender=None):
        return {'update_id': uid, 'business_message': {
            'message_id': mid or (2000 + uid),
            'date': int(time.time()),
            'chat': {'id': chat, 'type': 'private'},
            'from': {'id': chat if sender is None else sender},
            'business_connection_id': 'c',
            'text': text,
        }}

    async def admitted(self, update):
        self.assertTrue(self.db.ingest(update))
        self.assertTrue(await self.e.admit(update))
        return update

    def chat_sends(self, chat=2):
        return [x for x in self.tg.sent if x[1].get('chat_id') == chat]

    async def test_ingest_deduplicates_and_persists_offset(self):
        update = self.update(7)
        self.assertTrue(self.db.ingest(update))
        self.assertFalse(self.db.ingest(update))
        self.assertEqual(self.db.get('offset'), '8')

    async def test_admission_rejects_unknown_sender_and_connection(self):
        update = self.update(sender=3)
        self.assertFalse(await self.e.admit(update))
        self.assertIsNone(self.db.conversation(2))
        update = self.update(uid=2)
        update['business_message']['business_connection_id'] = 'other'
        self.assertFalse(await self.e.admit(update))

    async def test_owner_command_requires_owner_identity(self):
        update = {'update_id': 8, 'message': {
            'from': {'id': 2}, 'chat': {'id': 2, 'type': 'private'}, 'text': '/resume'}}
        self.db.set('paused', '1')
        self.assertFalse(await self.e.admit(update))
        self.assertEqual(self.db.get('paused'), '1')

    async def test_vacancy_inquiry_sends_conditions_once(self):
        update = await self.admitted(self.update())
        await self.e.process(update)
        conditions = [x for x in self.chat_sends() if x[1].get('text') == 'CONDITIONS']
        self.assertEqual(len(conditions), 1)
        self.assertEqual(self.db.candidate(2)['stage'], 'CONDITIONS_SENT')

    async def test_consent_sends_pdf_and_invite_once(self):
        first = await self.admitted(self.update(1))
        await self.e.process(first)
        consent = await self.admitted(self.update(2, text='Да, условия мне подходят. Я согласен перейти к следующему этапу'))
        await self.e.process(consent)
        self.assertEqual(sum(x[0] == 'sendDocument' for x in self.chat_sends()), 1)
        self.assertEqual(sum('https://t.me/test' in x[1].get('text', '') for x in self.chat_sends()), 1)
        self.assertEqual(self.db.candidate(2)['stage'], 'INVITE_SENT')
        self.assertEqual(self.db.db.execute('SELECT COUNT(*) FROM consents').fetchone()[0], 1)

        repeated = await self.admitted(self.update(3, text='Да, условия мне подходят. Я согласен перейти к следующему этапу'))
        await self.e.process(repeated)
        self.assertEqual(sum(x[0] == 'sendDocument' for x in self.chat_sends()), 1)
        self.assertEqual(sum('https://t.me/test' in x[1].get('text', '') for x in self.chat_sends()), 1)

    async def test_ambiguous_question_escalates_without_candidate_auto_reply(self):
        first = await self.admitted(self.update(1))
        await self.e.process(first)
        question = await self.admitted(self.update(2, text='А оплата будет?'))
        await self.e.process(question)
        self.assertEqual(self.db.candidate(2)['stage'], 'AWAITING_OWNER')
        self.assertEqual(self.db.conversation(2)['mode'], 'AWAITING_OWNER')
        self.assertTrue(any(x[1].get('chat_id') == 1 for x in self.tg.sent))
        self.assertEqual(len(self.chat_sends()), 1)

    async def test_decline_closes_auto_conversation(self):
        first = await self.admitted(self.update(1))
        await self.e.process(first)
        decline = await self.admitted(self.update(2, text='Отказываюсь от вакансии'))
        await self.e.process(decline)
        self.assertEqual(self.db.candidate(2)['stage'], 'DECLINED')
        before = len(self.chat_sends())
        repeated = await self.admitted(self.update(3, text='Интересует вакансия'))
        await self.e.process(repeated)
        self.assertEqual(len(self.chat_sends()), before)

    async def test_stop_and_resume_control_global_sending_gate(self):
        await self.e.command('/stop', 10)
        update = await self.admitted(self.update(1))
        await self.e.process(update)
        self.assertEqual(self.chat_sends(), [])
        await self.e.command('/resume', 11)
        update = await self.admitted(self.update(2))
        await self.e.process(update)
        self.assertEqual(len(self.chat_sends()), 1)

    async def test_delivery_unknown_is_not_retried(self):
        update = await self.admitted(self.update())
        epoch = self.db.conversation(2)['epoch']
        self.tg.fail_on = 1
        self.tg.unknown = True
        self.assertFalse(await self.e.deliver(2, 'x', 'once', epoch))
        self.assertFalse(await self.e.deliver(2, 'x', 'once', epoch))
        self.assertEqual(self.tg.calls, 1)
        self.assertEqual(self.db.db.execute('SELECT status FROM outgoing').fetchone()[0], 'UNKNOWN')

    async def test_new_message_invalidates_old_epoch(self):
        first = await self.admitted(self.update(1))
        old_epoch = self.db.conversation(2)['epoch']
        await self.admitted(self.update(2, text='Вопрос'))
        self.assertFalse(await self.e.deliver(2, 'stale', 'stale', old_epoch))

    async def test_edit_pauses_candidate_and_rebuilds_history(self):
        first = await self.admitted(self.update())
        edited = dict(self.update(2)['business_message'], message_id=2001, text='edited')
        update = {'update_id': 2, 'edited_business_message': edited}
        self.assertFalse(await self.e.admit(update))
        self.assertEqual(self.db.conversation(2)['mode'], 'AWAITING_OWNER')
        self.assertEqual(self.db.history(2)[0]['content'], 'edited')

    async def test_material_path_escape_is_rejected(self):
        with self.assertRaises(ValueError):
            self.cfg.material('../outside.txt')

    async def test_process_lock_rejects_second_instance(self):
        first = ProcessLock(self.root / 'runtime.lock')
        try:
            with self.assertRaises(RuntimeError):
                ProcessLock(self.root / 'runtime.lock')
        finally:
            first.close()
        second = ProcessLock(self.root / 'runtime.lock')
        second.close()

    async def test_manual_mode_blocks_automatic_conditions(self):
        update = await self.admitted(self.update())
        await self.e.command('/manual 2', 10)
        await self.e.process(update)
        self.assertEqual(self.chat_sends(), [])
        self.assertEqual(self.db.conversation(2)['mode'], 'MANUAL')

    async def test_restart_recovery_sets_global_stop_and_unknown_outgoing(self):
        await self.admitted(self.update())
        self.db.reserve('maybe', 2, 'sendMessage')
        self.db.recover()
        self.assertEqual(self.db.get('paused'), '1')
        self.assertEqual(self.db.db.execute('SELECT status FROM outgoing').fetchone()[0], 'UNKNOWN')


if __name__ == '__main__':
    unittest.main()
