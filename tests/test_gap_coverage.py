import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase, mock

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / 'src'
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import assistant_core.locking as locking
from assistant_core.config import Config
from assistant_core.locking import ProcessLock
from assistant_core.recruitment_parser import (
    is_explicit_consent,
    is_explicit_decline,
    is_vacancy_inquiry,
)
from assistant_core.storage import Store
from assistant_core.telegram import APIError, DeliveryUnknown
from assistant_core.workflow import Engine

OWNER = 1001
CHAT = 2002
CONN = 'conn'
VERSION = 'v1'
CONSENT = 'Да, условия мне подходят. Я согласен перейти к следующему этапу'
GROUP_URL = 'https://t.me/group'


def make_cfg(root: Path) -> Config:
    mats = root / 'materials'
    mats.mkdir(exist_ok=True)
    cond = mats / 'vacancy_conditions.txt'
    pdf = mats / 'company_presentation.pdf'
    cond.write_text('CONDITIONS', encoding='utf-8')
    pdf.write_bytes(b'%PDF-1.4 demo')
    return Config(
        client_id='company_a', owner_id=OWNER, business_owner_id=OWNER,
        contacts=(CHAT,), data_dir=root / 'data', materials_dir=mats,
        instructions='rules', faqs=(), tasks={},
        scenario_path=root / 'scenario.json',
        conditions_version=VERSION, conditions_file=cond,
        presentation_file=pdf, group_invite_url=GROUP_URL,
        unknown_question_text='Передам владельцу.',
        invite_message_text='invite',
    )


def business_message(mid, text):
    return {
        'message_id': mid, 'date': int(time.time()),
        'chat': {'id': CHAT, 'type': 'private'}, 'from': {'id': CHAT},
        'business_connection_id': CONN, 'text': text,
    }


class LockPosixBranchTests(unittest.TestCase):
    """locking.py: ветка POSIX (import fcntl; fcntl.flock)."""

    def _fake_fcntl(self, flock):
        return SimpleNamespace(LOCK_EX=2, LOCK_NB=4, flock=flock)

    def test_posix_branch_takes_exclusive_nonblocking_lock(self):
        flock = mock.Mock()
        fake = {'fcntl': self._fake_fcntl(flock)}
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, 'runtime.lock')
            with mock.patch.dict(sys.modules, fake):
                with mock.patch.object(locking.os, 'name', 'posix'):
                    lock = ProcessLock(path)
                    lock.close()
                    lock.close()
            flock.assert_called_once()
            self.assertEqual(flock.call_args[0][1], 2 | 4)

    def test_posix_branch_busy_lock_raises_and_closes_handle(self):
        flock = mock.Mock(side_effect=BlockingIOError())
        fake = {'fcntl': self._fake_fcntl(flock)}
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, 'runtime.lock')
            opened = []
            real_open = open

            def tracking_open(*args, **kwargs):
                handle = real_open(*args, **kwargs)
                opened.append(handle)
                return handle

            with mock.patch.dict(sys.modules, fake):
                with mock.patch.object(locking.os, 'name', 'posix'):
                    with mock.patch('builtins.open', tracking_open):
                        with self.assertRaises(RuntimeError):
                            ProcessLock(path)
            self.assertTrue(opened and all(h.closed for h in opened))


class FakeTG:
    """Telegram-заглушка: запоминает отправки.

    fail - очередь исключений для следующих вызовов.
    """

    def __init__(self):
        self.sent = []
        self.fail = []

    async def call(self, method, payload=None, document=None):
        if self.fail:
            raise self.fail.pop(0)
        self.sent.append((method, payload or {}, document))
        return {'message_id': 1000 + len(self.sent)}


class WorkflowGapBase(IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.cfg = make_cfg(self.root)
        self.db = Store(self.cfg.data_dir, 'company_a')
        self.db.set('paused', '0')
        self.db.set('connection', json.dumps({
            'id': CONN, 'user': {'id': OWNER}, 'is_enabled': True,
            'rights': {'can_reply': True}}))
        self.tg = FakeTG()
        self.engine = Engine(self.cfg, self.db, self.tg)

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def stage(self):
        row = self.db.candidate(CHAT)
        return row['stage'] if row else None

    def incoming(self, mid, text, chat=CHAT):
        self.db.incoming(chat, CONN, mid, text, time.time())

    def epoch(self):
        return self.db.conversation(CHAT)['epoch']

    def set_candidate(self, stage, conditions_mid=10, version=VERSION):
        """Кандидат на нужном этапе (через candidates.stage, как в базе)."""
        self.db.set_stage(CHAT, 'CONDITIONS_SENT',
                          conditions_version=version,
                          conditions_mid=conditions_mid)
        if stage != 'CONDITIONS_SENT':
            self.db.execute('UPDATE candidates SET stage=? WHERE chat=?',
                            (stage, CHAT))
        self.db.incoming(CHAT, CONN, 1, 'start', time.time())

    async def say(self, mid, text):
        self.incoming(mid, text)
        await self.engine.business(business_message(mid, text), mid)

    def bump_epoch(self):
        """Имитация /stop, /manual или нового сообщения при отправке."""
        self.db.execute(
            'UPDATE conversations SET epoch=epoch+1 WHERE chat=?', (CHAT,))

    def bump_epoch_after_deliver(self):
        real = self.engine.deliver

        async def wrapper(*args, **kwargs):
            result = await real(*args, **kwargs)
            self.bump_epoch()
            return result

        self.engine.deliver = wrapper

    def methods(self):
        return [m for m, _, _ in self.tg.sent]

    def to(self, chat):
        return [p for _, p, _ in self.tg.sent if p.get('chat_id') == chat]

    def invites(self):
        return [p for p in self.to(CHAT) if GROUP_URL in p.get('text', '')]


class ParserBranchTests(unittest.TestCase):
    def test_consent_edge_cases(self):
        too_long = ' '.join(['согласен'] * 16)
        for text in ('', '   ', '...', too_long):
            with self.subTest(text=text):
                self.assertFalse(is_explicit_consent(text))
        for text in ('да', 'Да!', 'ок', 'Хорошо', 'Согласен',
                     'Условия устраивают'):
            with self.subTest(text=text):
                self.assertTrue(is_explicit_consent(text))

    def test_negations_and_hedges_are_not_consent(self):
        for text in ('Я не согласен', 'Не согласна', 'Мне не подходит',
                     'Нет, не согласен с условиями',
                     'Не принимаю условия', 'согласен, но есть вопрос',
                     'ознакомился', 'Согласен?'):
            with self.subTest(text=text):
                self.assertFalse(is_explicit_consent(text))

    def test_decline_edge_cases(self):
        for text in ('Я не согласен', 'Не подходит', 'нет',
                     'Нет, спасибо', 'Отказываюсь'):
            with self.subTest(text=text):
                self.assertTrue(is_explicit_decline(text))
        for text in ('нет, но я подумаю', 'нет проблем',
                     'Нет, а когда старт?', 'согласен'):
            with self.subTest(text=text):
                self.assertFalse(is_explicit_decline(text))

    def test_vacancy_inquiry(self):
        self.assertTrue(
            is_vacancy_inquiry('Здравствуйте, хочу узнать о вакансии'))
        self.assertFalse(is_vacancy_inquiry('Привет, как дела?'))


class StepAndGuardTests(WorkflowGapBase):
    async def test_block_reasons_for_missing_and_service_chat(self):
        self.assertEqual(self.engine._block_reason(9999, 0),
                         'NO_CONVERSATION')
        self.incoming(1, 'x', chat=OWNER)
        epoch = self.db.conversation(OWNER)['epoch']
        self.assertEqual(self.engine._block_reason(OWNER, epoch),
                         'SERVICE_CHAT')

    async def test_step_is_idempotent_after_sent(self):
        self.incoming(1, 'x')
        first = await self.engine._step(CHAT, 'p:1', 'text', self.epoch())
        second = await self.engine._step(CHAT, 'p:1', 'text', self.epoch())
        self.assertEqual(first[0], 'SENT')
        self.assertEqual(second, first)
        self.assertEqual(len(self.tg.sent), 1)

    async def test_step_with_unknown_delivery_is_not_retried_blindly(self):
        self.incoming(1, 'x')
        self.tg.fail = [DeliveryUnknown()]
        for _ in range(2):
            result = await self.engine._step(CHAT, 'p:2', 't', self.epoch())
            self.assertEqual(result[0], 'UNKNOWN')
        self.assertEqual(self.tg.sent, [])

    async def test_step_gives_up_after_max_attempts(self):
        self.incoming(1, 'x')
        self.tg.fail = [APIError(), APIError(), APIError()]
        for _ in range(3):
            result = await self.engine._step(CHAT, 'p:3', 't', self.epoch())
            self.assertEqual(result[0], 'FAILED')
        self.tg.fail = []
        result = await self.engine._step(CHAT, 'p:3', 't', self.epoch())
        self.assertEqual(result[0], 'FAILED')
        self.assertEqual(self.tg.sent, [],
                         'четвёртой отправки быть не должно')

    async def test_stage_conflict_is_logged_not_raised(self):
        self.db.set_stage(CHAT, 'NEW')
        self.assertFalse(self.engine._set_stage_safe(CHAT, 'INVITE_SENT'))
        kinds = [r[0] for r in self.db.db.execute('SELECT kind FROM audit')]
        self.assertIn('STAGE_CONFLICT', kinds)

    async def test_blocked_step_notifies_owner_only_if_recoverable(self):
        self.incoming(1, 'x')
        epoch = self.epoch()
        self.db.set('paused', '1')
        await self.engine._step_problem(
            CHAT, 'k1', 'условия', 'BLOCKED', epoch)
        self.assertEqual(len(self.to(OWNER)), 1)
        self.assertIn('STOP', self.to(OWNER)[0]['text'])
        self.db.set('paused', '0')
        await self.engine._step_problem(
            CHAT, 'k2', 'условия', 'BLOCKED', epoch + 5)
        self.assertEqual(len(self.to(OWNER)), 1)

    async def test_conditions_longer_than_one_message_are_refused(self):
        self.cfg.conditions_file.write_text('x' * 4000, encoding='utf-8')
        self.incoming(1, 'Интересует вакансия')
        with self.assertRaises(ValueError):
            await self.engine.business(
                business_message(1, 'Интересует вакансия'), 1)
        self.assertEqual(self.tg.sent, [])  # обрезанные условия не уходят

    async def test_admit_errors_go_to_review_and_never_raise(self):
        update = {'update_id': 77}
        self.db.ingest(update)

        async def boom(_):
            raise RuntimeError('bad update')

        self.engine._admit = boom
        self.assertFalse(await self.engine.admit(update))
        status = self.db.db.execute(
            'SELECT status FROM inbox WHERE id=77').fetchone()[0]
        self.assertEqual(status, 'REVIEW')
        self.assertEqual(len(self.to(OWNER)), 1)
        self.engine.owner = mock.AsyncMock(
            side_effect=RuntimeError('owner down'))
        self.assertFalse(await self.engine.admit(update))


class ConditionsAndConsentFlowTests(WorkflowGapBase):
    async def test_sent_conditions_saved_even_if_epoch_changed(self):
        self.bump_epoch_after_deliver()
        await self.say(1, 'Интересует вакансия')
        self.assertEqual(self.methods(), ['sendMessage'])
        self.assertEqual(self.stage(), 'CONDITIONS_SENT')

    async def test_consent_not_after_conditions_goes_to_owner(self):
        self.set_candidate('CONDITIONS_SENT', conditions_mid=50)
        await self.say(5, CONSENT)
        self.assertNotIn('sendDocument', self.methods())
        self.assertEqual(self.stage(), 'AWAITING_OWNER')
        self.assertEqual(self.db.candidate(CHAT)['prev_stage'],
                         'CONDITIONS_SENT')
        self.assertIsNone(
            self.db.db.execute('SELECT 1 FROM consents').fetchone())
        self.assertEqual(len(self.to(CHAT)), 1)
        self.assertEqual(len(self.to(OWNER)), 1)

    async def test_consent_to_other_version_is_not_carried_over(self):
        self.set_candidate('CONDITIONS_SENT', conditions_mid=10,
                           version='old-version')
        await self.say(20, CONSENT)
        self.assertNotIn('sendDocument', self.methods())
        self.assertEqual(self.stage(), 'AWAITING_OWNER')
        self.assertIsNone(
            self.db.db.execute('SELECT 1 FROM consents').fetchone())

    async def test_duplicate_consent_message_is_ignored(self):
        self.set_candidate('CONDITIONS_SENT', conditions_mid=10)
        self.db.execute(
            'INSERT INTO consents(chat,mid,text,conditions_version,at_utc)'
            ' VALUES(?,?,?,?,?)', (CHAT, 20, CONSENT, VERSION, 'now'))
        await self.say(20, CONSENT)
        self.assertEqual(self.tg.sent, [])
        self.assertEqual(self.stage(), 'CONDITIONS_SENT')

    async def test_decline_is_saved_even_if_epoch_changed(self):
        self.set_candidate('CONDITIONS_SENT')
        self.bump_epoch_after_deliver()
        await self.say(30, 'нет')
        self.assertEqual(self.methods(), ['sendMessage'])
        self.assertEqual(self.stage(), 'DECLINED')

    async def test_refusal_with_negation_does_not_release_package(self):
        self.set_candidate('CONDITIONS_SENT')
        await self.say(70, 'Я не согласен')
        self.assertNotIn('sendDocument', self.methods())
        self.assertEqual(self.invites(), [])
        self.assertEqual(self.stage(), 'DECLINED')

    async def test_unknown_question_candidate_gets_exactly_one_notice(self):
        self.set_candidate('CONDITIONS_SENT')
        await self.say(60, 'Можно удалённо?')
        self.assertEqual(len(self.to(CHAT)), 1)
        self.assertEqual(self.to(CHAT)[0]['text'],
                         self.cfg.unknown_question_text)
        self.assertEqual(self.stage(), 'AWAITING_OWNER')
        await self.say(61, 'И ещё вопрос')
        self.assertEqual(len(self.to(CHAT)), 1)


class PackageRecoveryTests(WorkflowGapBase):
    async def test_invite_failure_then_owner_resume_sends_once(self):
        self.set_candidate('PRESENTATION_SENT')
        self.tg.fail = [APIError()]
        await self.say(10, 'Здравствуйте')
        self.assertEqual(self.stage(), 'AWAITING_OWNER')
        self.assertEqual(self.db.candidate(CHAT)['prev_stage'],
                         'PRESENTATION_SENT')
        self.assertIn('приглашение', self.to(OWNER)[0]['text'])
        await self.engine.command(f'/resume_candidate {CHAT}', 500)
        self.assertEqual(self.stage(), 'INVITE_SENT')
        self.assertEqual(len(self.invites()), 1)
        # повтор: ничего не шлём
        await self.engine.command(f'/resume_candidate {CHAT}', 501)
        self.assertEqual(len(self.invites()), 1)

    async def test_incomplete_package_is_finished_on_next_message(self):
        self.set_candidate('CONSENT_RECORDED')
        await self.say(10, 'спасибо')
        self.assertEqual(self.stage(), 'INVITE_SENT')
        self.assertEqual(self.methods().count('sendDocument'), 1)
        before = len(self.tg.sent)
        await self.say(11, 'спасибо')
        self.assertEqual(len(self.tg.sent), before)
        await self.say(12, 'Какая будет зарплата?')
        self.assertEqual(self.stage(), 'AWAITING_OWNER')
        self.assertEqual(self.db.candidate(CHAT)['prev_stage'],
                         'INVITE_SENT')

    async def test_incomplete_package_without_pdf_goes_to_owner(self):
        self.cfg.presentation_file.unlink()
        self.set_candidate('CONSENT_RECORDED')
        await self.say(10, 'Здравствуйте')
        self.assertEqual(self.stage(), 'AWAITING_OWNER')
        self.assertNotIn('sendDocument', self.methods())
        self.assertIn('презентация', self.to(OWNER)[0]['text'])


class PostConsentEscalationTests(WorkflowGapBase):
    QUESTION = 'Какая будет зарплата?'

    async def test_post_consent_question_escalates_keeps_prev_stage(self):
        self.set_candidate('INVITE_SENT')
        await self.say(40, self.QUESTION)
        self.assertEqual(self.stage(), 'AWAITING_OWNER')
        self.assertEqual(self.db.candidate(CHAT)['prev_stage'],
                         'INVITE_SENT')
        self.assertEqual(self.db.conversation(CHAT)['mode'],
                         'AWAITING_OWNER')
        self.assertEqual(len(self.to(CHAT)), 1)
        self.assertEqual(len(self.to(OWNER)), 1)
        self.assertIn(self.QUESTION, self.to(OWNER)[0]['text'])

    async def test_repeated_consent_or_inquiry_after_invite_ignored(self):
        self.set_candidate('INVITE_SENT')
        await self.say(41, CONSENT)
        await self.say(42, 'Интересует вакансия')
        self.assertEqual(self.tg.sent, [])
        self.assertEqual(self.stage(), 'INVITE_SENT')


class StorageConsentGuardTests(WorkflowGapBase):
    async def test_consent_rejected_when_conditions_mid_unknown(self):
        """CONDITIONS_SENT без сохранённого conditions_mid.

        Нельзя убедиться, что сообщение кандидата пришло ПОСЛЕ условий,
        поэтому согласие не записывается.
        """
        # conditions_mid=None
        self.db.set_stage(CHAT, 'CONDITIONS_SENT',
                          conditions_version=VERSION)
        self.assertIsNone(self.db.candidate(CHAT)['conditions_mid'])
        self.assertEqual(
            self.db.record_consent(CHAT, 99, CONSENT, VERSION), 'REJECTED')
        self.assertEqual(self.stage(), 'CONDITIONS_SENT')
        self.assertIsNone(
            self.db.db.execute('SELECT 1 FROM consents').fetchone())

    async def test_consent_message_at_that_state_goes_to_owner(self):
        self.db.set_stage(CHAT, 'CONDITIONS_SENT',
                          conditions_version=VERSION)
        await self.say(99, CONSENT)
        self.assertNotIn('sendDocument', self.methods())
        self.assertEqual(self.stage(), 'AWAITING_OWNER')
        self.assertEqual(len(self.to(OWNER)), 1)


if __name__ == '__main__':
    unittest.main()