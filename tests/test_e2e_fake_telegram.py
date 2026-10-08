"""End-to-end: the real bot process (python -m assistant_core.main) against a local fake Bot API.

Nothing is mocked inside the bot: config loading, SQLite, polling, HTTP, multipart upload,
restart and STOP all run as in production. Only api.telegram.org is replaced by fake_bot_api.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from fake_bot_api import FakeBotAPI, TOKEN

ROOT = Path(__file__).resolve().parents[1]
OWNER = 5000            # owner and Business account are one person, as in the real install
CANDIDATE = 7000
OTHER_CANDIDATE = 7001
CONDITIONS = (ROOT / 'examples/materials/vacancy_conditions.txt').read_text(encoding='utf-8')
INVITE_URL = json.loads((ROOT / 'examples/recruitment.scenario.json').read_text(encoding='utf-8'))['group_invite_url']
CONSENT = 'Да, условия мне подходят. Я согласен перейти к следующему этапу'


class BotE2E(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        mats = self.root / 'materials'
        shutil.copytree(ROOT / 'examples/materials', mats)
        scenario = json.loads((ROOT / 'examples/recruitment.scenario.json').read_text(encoding='utf-8'))
        scenario['conditions_file'] = str(mats / 'vacancy_conditions.txt')
        scenario['presentation_file'] = str(mats / 'company_presentation.pdf')
        (self.root / 'scenario.json').write_text(json.dumps(scenario, ensure_ascii=False), encoding='utf-8')
        cfg = json.loads((ROOT / 'examples/config.example.json').read_text(encoding='utf-8'))
        cfg.update(client_id='e2e', owner_id=OWNER, business_owner_id=OWNER, contacts=[],
                   data_dir=str(self.root / 'data'), materials_dir=str(mats),
                   recruitment_scenario='scenario.json')
        self.config = self.root / 'config.json'
        self.config.write_text(json.dumps(cfg, ensure_ascii=False), encoding='utf-8')
        self.api = FakeBotAPI()
        self.proc = None

    def tearDown(self):
        self.stop_bot()
        self.api.close()
        self.tmp.cleanup()

    # ------------------------------------------------------------------ bot process

    def start_bot(self):
        env = dict(os.environ, BOT_TOKEN=TOKEN, TELEGRAM_API_URL=self.api.url, AI_MODE='faq',
                   PYTHONPATH=str(ROOT / 'src'))
        self.log = open(self.root / 'bot.log', 'a')
        self.proc = subprocess.Popen([sys.executable, '-m', 'assistant_core.main', '--config', str(self.config)],
                                     cwd=ROOT, env=env, stdout=self.log, stderr=subprocess.STDOUT)
        self.api.wait(lambda: any('Ядро запущено' in (m['text'] or '') for m in self.api.sent[self.started:]),
                      what='start notice')
        self.started = len(self.api.sent)

    started = 0

    def stop_bot(self):
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            self.proc.wait(10)
        self.proc = None
        if getattr(self, 'log', None):
            self.log.close()
            self.log = None
        self.started = len(self.api.sent)

    def restart_bot(self):
        self.api.settle()
        self.stop_bot()
        self.start_bot()

    def owner_says(self, text):
        self.api.owner_command(OWNER, text)
        self.api.settle()

    def say(self, text, chat=CANDIDATE, **kw):
        message = self.api.candidate_says(chat, text, **kw)
        self.api.settle()
        return message

    def ready(self):
        """Connected, started, /resume: the normal working state."""
        self.api.connect(OWNER)
        self.start_bot()
        self.owner_says('/resume')

    # ------------------------------------------------------------------ observations

    def texts(self, chat=CANDIDATE):
        return [m['text'] or m['caption'] or '' for m in self.api.to(chat)]

    def owner_texts(self):
        return self.texts(OWNER)

    def pdfs(self, chat=CANDIDATE):
        return [m for m in self.api.to(chat) if m['method'] == 'sendDocument']

    def invites(self, chat=CANDIDATE):
        return [t for t in self.texts(chat) if INVITE_URL in t]

    def conditions(self, chat=CANDIDATE):
        return [t for t in self.texts(chat) if t == CONDITIONS]

    # ------------------------------------------------------------------ scenarios

    def test_01_happy_path(self):
        self.ready()
        self.say('Здравствуйте, интересует вакансия')
        self.assertEqual(self.conditions(), [CONDITIONS], 'полный текст условий без изменений')
        self.say(CONSENT)
        pdf, = self.pdfs()
        self.assertEqual(pdf['filename'], 'company_presentation.pdf')
        self.assertEqual(pdf['content_type'], 'application/pdf')
        self.assertEqual(pdf['size'], (ROOT / 'examples/materials/company_presentation.pdf').stat().st_size)
        self.assertEqual(len(self.invites()), 1)
        self.assertTrue(all(m['business_connection_id'] == 'conn-1' for m in self.api.to(CANDIDATE)),
                        'кандидату — только через Business-подключение')

    def test_02_repeats_do_not_duplicate(self):
        self.ready()
        self.say('Интересует вакансия')
        self.say(CONSENT)
        self.say('Согласен')
        self.say('Интересует вакансия')
        self.assertEqual((len(self.conditions()), len(self.pdfs()), len(self.invites())), (1, 1, 1))

    def test_02b_repeated_inquiry_before_consent(self):
        """Повтор «интересует вакансия» / «спасибо» после условий не дублирует и не останавливает диалог."""
        self.ready()
        self.say('Интересует вакансия')
        self.say('Здравствуйте, интересует вакансия!')
        self.say('Спасибо')
        self.assertEqual(len(self.texts()), 1, 'повтор не вызывает новых сообщений')
        self.say(CONSENT)
        self.assertEqual((len(self.pdfs()), len(self.invites())), (1, 1))

    def test_02c_long_message_with_inquiry_still_goes_to_owner(self):
        """Вопрос без «?» внутри длинного сообщения не теряется."""
        self.ready()
        self.say('Интересует вакансия')
        self.say('Интересует вакансия, хотел бы ещё уточнить график работы и оплату в первый месяц')
        self.assertEqual(self.texts()[-1], 'Передам ваш вопрос Владимиру для ответа.')
        self.assertTrue(any('график' in t for t in self.owner_texts()))

    def test_03_decline(self):
        self.ready()
        self.say('Интересует вакансия')
        self.say('Нет, не подходит')
        self.assertEqual(self.pdfs(), [])
        self.assertEqual(self.invites(), [])
        self.assertIn('Спасибо за отклик', self.texts()[-1])

    def test_04_unknown_question_goes_to_owner(self):
        self.ready()
        self.say('Интересует вакансия')
        self.say('А можно работать удалённо из другой страны?')
        self.assertEqual(self.texts()[-1], 'Передам ваш вопрос Владимиру для ответа.')
        self.assertTrue(any('удалённо' in t for t in self.owner_texts()), 'вопрос пересылается владельцу')
        self.say(CONSENT)
        self.assertEqual(self.pdfs(), [], 'пока владелец не решил, бот молчит')

    def test_05_starts_in_stop(self):
        self.api.connect(OWNER)
        self.start_bot()
        self.say('Интересует вакансия')
        self.assertEqual(self.api.to(CANDIDATE), [], 'после запуска бот в STOP')
        self.assertTrue(any('не обработано' in t for t in self.owner_texts()))

    def test_06_stop_between_steps(self):
        self.ready()
        self.say('Интересует вакансия')
        self.owner_says('/stop')
        self.say(CONSENT)
        self.assertEqual(self.pdfs(), [])
        self.owner_says('/resume')
        self.say('Согласен')
        self.assertEqual((len(self.pdfs()), len(self.invites())), (1, 1), 'после /resume пакет выдаётся один раз')

    def test_07_forged_owner_command(self):
        self.api.connect(OWNER)
        self.start_bot()
        self.api.owner_command(OWNER, '/resume', sender=CANDIDATE)   # чужой /resume в чате владельца
        self.api.push(message={'message_id': 1, 'date': int(time.time()), 'from': {'id': CANDIDATE},
                               'chat': {'id': CANDIDATE, 'type': 'private'}, 'text': '/resume'})
        self.api.settle()
        self.say('Интересует вакансия')
        self.assertEqual(self.api.to(CANDIDATE), [], 'поддельная команда не сняла STOP')

    def test_08_owner_writes_manually(self):
        self.ready()
        self.say('Интересует вакансия')
        self.say('Добрый день, это Владимир, отвечу сам', sender=OWNER)
        self.say(CONSENT)
        self.assertEqual(self.pdfs(), [], 'владелец вмешался — диалог в ручном режиме')

    def test_09_not_candidates_ignored(self):
        self.ready()
        self.say('Интересует вакансия', chat=OWNER)                          # служебный чат владельца
        self.say('Интересует вакансия', chat=9100, is_bot=True)              # другой бот
        self.say('Интересует вакансия', chat=-100500, chat_type='group')     # группа
        self.say('Интересует вакансия', chat=9200, conn_id='foreign-conn')   # чужое подключение
        for chat in (9100, -100500, 9200):
            self.assertEqual(self.api.to(chat), [])
        self.assertFalse(any(t == CONDITIONS for t in self.owner_texts()))
        self.say('Привет, как дела?', chat=OTHER_CANDIDATE)
        self.assertEqual(self.api.to(OTHER_CANDIDATE), [], 'не про вакансию — без условий')

    def test_10_restart_after_each_stage(self):
        self.ready()
        self.say('Интересует вакансия')
        self.restart_bot()
        self.owner_says('/resume')
        self.say(CONSENT)
        self.restart_bot()
        self.owner_says('/resume')
        self.say('Спасибо')
        self.assertEqual((len(self.conditions()), len(self.pdfs()), len(self.invites())), (1, 1, 1))

    def test_11_rights_revoked_while_offline(self):
        self.ready()
        self.say('Интересует вакансия')
        self.stop_bot()
        self.api.connection = dict(self.api.connection, is_enabled=False)   # отключили, пока бот выключен
        self.start_bot()
        self.assertIn('Business-подключение отключено', self.owner_texts()[-1])
        self.owner_says('/resume')
        self.say(CONSENT)
        self.assertEqual(self.pdfs(), [])

    def test_12_rights_revoked_while_running(self):
        self.ready()
        self.say('Интересует вакансия')
        self.api.connect(OWNER, can_reply=False)
        self.api.settle()
        self.say(CONSENT)
        self.assertEqual(self.pdfs(), [], 'без can_reply бот не пишет')

    def test_13_lost_telegram_answer_not_retried(self):
        self.ready()
        self.say('Интересует вакансия')
        self.api.fail('sendDocument', 'drop')            # PDF ушёл, а ответ Telegram потерян
        self.say(CONSENT)
        self.assertEqual(self.invites(), [], 'без подтверждения PDF ссылку не шлём')
        self.assertTrue(any('UNKNOWN' in t for t in self.owner_texts()), 'владельцу — на сверку')
        self.restart_bot()
        self.owner_says('/resume')
        self.say('Согласен')
        self.assertEqual(self.api.calls.count('sendDocument'), 1, 'вслепую не повторяем')

    def test_14_candidate_blocked_bot(self):
        self.ready()
        self.say('Интересует вакансия')
        self.api.fail('sendDocument', 403, description='Forbidden: bot was blocked by the user')
        self.say(CONSENT)
        self.assertEqual(self.invites(), [])
        self.assertTrue(any('FAILED' in t for t in self.owner_texts()))
        self.assertIsNone(self.proc.poll(), 'бот не упал')

    def test_15_flood_control_on_polling(self):
        self.ready()
        self.api.fail('getUpdates', 429, retry_after=1, description='Too Many Requests')
        self.say('Интересует вакансия')
        self.assertEqual(len(self.conditions()), 1, 'после паузы retry_after бот продолжил')

    def test_16_pdf_missing(self):
        self.ready()
        self.say('Интересует вакансия')
        (self.root / 'materials/company_presentation.pdf').unlink()
        self.say(CONSENT)
        self.assertEqual((self.pdfs(), self.invites()), ([], []), 'без PDF и без ссылки')
        self.assertTrue(any('презентация' in t for t in self.owner_texts()))
        self.assertIsNone(self.proc.poll())

    def test_17_consent_edited(self):
        self.ready()
        self.say('Интересует вакансия')
        consent = self.say(CONSENT)
        self.api.owner_edits(consent, 'Нет, передумал')
        self.api.settle()
        self.assertTrue(any('согласия' in t for t in self.owner_texts()))
        self.say('Интересует вакансия')
        self.assertEqual(len(self.texts()), 3, 'после правки согласия автоответы остановлены')

    def test_18_two_candidates_in_parallel(self):
        self.ready()
        self.api.candidate_says(CANDIDATE, 'Интересует вакансия')
        self.api.candidate_says(OTHER_CANDIDATE, 'Пишу по поводу вакансии')
        self.api.settle()
        self.api.candidate_says(CANDIDATE, CONSENT)
        self.api.candidate_says(OTHER_CANDIDATE, 'Нет, не интересно')
        self.api.settle()
        self.assertEqual((len(self.pdfs()), len(self.invites())), (1, 1))
        self.assertEqual((self.pdfs(OTHER_CANDIDATE), self.invites(OTHER_CANDIDATE)), ([], []))


    # ------------------------------------------------------------------ editable scenario

    def edit_scenario(self, **changes):
        path = self.root / 'scenario.json'
        sc = json.loads(path.read_text(encoding='utf-8'))
        sc.update(changes)
        path.write_text(json.dumps(sc, ensure_ascii=False), encoding='utf-8')

    def test_19_scenario_custom_texts_phrases_and_order(self):
        self.edit_scenario(
            decline_message='Понял, удачи!',
            phrases={'vacancy': ['ищу подработку'], 'consent': ['беру'], 'decline': ['пас']},
            after_consent=[
                {'id': 'link', 'type': 'text', 'text': 'Вот группа: {url}', 'name': 'приглашение'},
                {'id': 'deck', 'type': 'document', 'caption': 'И презентация'},
                {'id': 'bye', 'type': 'text', 'text': 'До встречи!'},
            ])
        self.ready()
        self.say('Интересует вакансия')
        self.assertEqual(self.api.to(CANDIDATE), [], 'старая фраза заменена')
        self.say('Привет, ищу подработку')
        self.assertEqual(self.conditions(), [CONDITIONS])
        self.say('Беру')
        got = [(m['method'], m['text'] or m['caption']) for m in self.api.to(CANDIDATE)[1:]]
        self.assertEqual(got, [('sendMessage', 'Вот группа: https://t.me/+EvhipOH4o6IwMTFl'),
                               ('sendDocument', 'И презентация'),
                               ('sendMessage', 'До встречи!')])
        self.say('Беру')
        self.assertEqual(len(self.api.to(CANDIDATE)), 4, 'без дублей')
        self.say('Ищу подработку', chat=OTHER_CANDIDATE)
        self.say('Пас', chat=OTHER_CANDIDATE)
        self.assertEqual(self.texts(OTHER_CANDIDATE)[-1], 'Понял, удачи!')

    def test_20_scenario_edit_applies_after_restart(self):
        self.ready()
        self.say('Интересует вакансия')
        self.say('Да, согласен')
        self.assertEqual(len(self.invites()), 1)
        self.stop_bot()
        self.edit_scenario(after_consent=[
            {'id': 'pdf', 'type': 'document', 'caption': 'Новая подпись'},
            {'id': 'inv', 'type': 'text', 'text': 'Новый текст приглашения: {url}'}])
        self.start_bot()
        self.owner_says('/resume')
        self.say('Спасибо')
        self.assertEqual(len(self.api.to(CANDIDATE)), 3, 'первый кандидат ничего повторно не получил')
        self.say('Интересует вакансия', chat=OTHER_CANDIDATE)
        self.say('Согласен', chat=OTHER_CANDIDATE)
        self.assertEqual(self.texts(OTHER_CANDIDATE)[1:],
                         ['Новая подпись', 'Новый текст приглашения: https://t.me/+EvhipOH4o6IwMTFl'])

    def test_21_scenario_broken_stops_startup(self):
        self.edit_scenario(after_consent=[{'id': 'a', 'type': 'text', 'text': ''}])
        env = dict(os.environ, BOT_TOKEN=TOKEN, TELEGRAM_API_URL=self.api.url, AI_MODE='faq',
                   PYTHONPATH=str(ROOT / 'src'))
        out = subprocess.run([sys.executable, '-m', 'assistant_core.main', '--config', str(self.config)],
                             cwd=ROOT, env=env, capture_output=True, text=True, timeout=20)
        self.assertNotEqual(out.returncode, 0)
        self.assertIn('after_consent #1', out.stderr + out.stdout)
        self.assertEqual(self.api.calls, [], 'с ошибочным сценарием бот не стартует и в Telegram не ходит')



if __name__ == '__main__':
    unittest.main()
