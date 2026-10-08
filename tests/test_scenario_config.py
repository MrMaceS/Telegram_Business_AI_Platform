"""Сценарий без правки кода: тексты, фразы и порядок сообщений из recruitment.scenario.json."""
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from acceptance_harness import (
    Acceptance, CANDIDATE, CONSENT, CONN, FakeAI, OWNER, ROOT,
)
from assistant_core.config import Config
from assistant_core.storage import Store
from assistant_core.workflow import Engine


def write_config(root, patch):
    """Пример сценария из examples/ + изменения теста; возвращает путь к config.json."""
    mats = root / 'materials'
    if not mats.exists():
        shutil.copytree(ROOT / 'examples/materials', mats)
    scenario = json.loads((ROOT / 'examples/recruitment.scenario.json').read_text(encoding='utf-8'))
    scenario['conditions_file'] = str(mats / 'vacancy_conditions.txt')
    scenario['presentation_file'] = str(mats / 'company_presentation.pdf')
    for key, value in patch.items():
        if value is None:
            scenario.pop(key, None)
        else:
            scenario[key] = value
    (root / 'scenario.json').write_text(json.dumps(scenario, ensure_ascii=False), encoding='utf-8')
    cfg = json.loads((ROOT / 'examples/config.example.json').read_text(encoding='utf-8'))
    cfg.update(owner_id=OWNER, business_owner_id=OWNER, contacts=[],
               data_dir=str(root / 'data'), materials_dir=str(mats),
               recruitment_scenario='scenario.json')
    path = root / 'config.json'
    path.write_text(json.dumps(cfg, ensure_ascii=False), encoding='utf-8')
    return path


class ScenarioCase(Acceptance):
    patch = {}

    def start_engine(self, fresh=False):
        self.cfg = Config.load(str(write_config(self.root, self.patch)))
        self.db = Store(self.cfg.data_dir, self.cfg.client_id)
        self.e = Engine(self.cfg, self.db, self.tg, FakeAI())
        if fresh:
            self.db.set('paused', '0')
            self.db.set('connection', json.dumps({
                'id': CONN, 'user': {'id': OWNER}, 'is_enabled': True,
                'rights': {'can_reply': True}}))
        else:
            self.db.recover()

    def stage(self):
        return self.db.candidate(CANDIDATE)['stage']


class CustomTextsAndPhrases(ScenarioCase):
    patch = {
        'decline_message': 'Хорошо, удачи вам!',
        'phrases': {'vacancy': ['ищу подработку'], 'consent': ['беру'], 'decline': ['пас']},
    }

    async def test_custom_vacancy_phrase_replaces_builtin(self):
        await self.candidate('Интересует вакансия')
        self.assertEqual(self.conditions_sent(), [], 'встроенная фраза заменена списком из сценария')
        await self.candidate('Добрый день, ищу подработку')
        self.assertEqual(len(self.conditions_sent()), 1)

    async def test_custom_consent_phrase(self):
        await self.candidate('Ищу подработку')
        await self.candidate('Беру')
        self.assertEqual(len(self.pdfs()), 1)
        self.assertEqual(self.stage(), 'INVITE_SENT')

    async def test_custom_decline_phrase_and_text(self):
        await self.candidate('Ищу подработку')
        await self.candidate('Пас')
        self.assertEqual(self.texts()[-1], 'Хорошо, удачи вам!')
        self.assertEqual(self.stage(), 'DECLINED')


class CustomSequence(ScenarioCase):
    patch = {'after_consent': [
        {'id': 'thanks', 'type': 'text', 'text': 'Спасибо, согласие записано.'},
        {'id': 'link', 'type': 'text', 'text': 'Группа: {url}', 'name': 'приглашение'},
        {'id': 'deck', 'type': 'document', 'caption': 'Презентация после ссылки'},
    ]}

    async def test_messages_follow_configured_order_once(self):
        await self.candidate('Интересует вакансия')
        await self.candidate(CONSENT)
        package = self.to_chat()[1:]
        self.assertEqual([m for m, _, _ in package], ['sendMessage', 'sendMessage', 'sendDocument'])
        self.assertEqual(self.texts()[1], 'Спасибо, согласие записано.')
        self.assertEqual(self.texts()[2], f'Группа: {self.cfg.group_invite_url}')
        self.assertEqual(self.texts()[3], 'Презентация после ссылки')
        self.assertEqual(self.stage(), 'INVITE_SENT')
        await self.candidate(CONSENT)
        self.assertEqual(len(self.to_chat()), 4, 'повторное согласие не дублирует пакет')

    async def test_failed_middle_step_resumes_without_repeats(self):
        await self.owner_cmd('/resume')
        await self.candidate('Интересует вакансия')
        self.tg.fail_on = self.tg.calls + 2       # 1-е сообщение пакета уходит, 2-е падает
        await self.candidate(CONSENT)
        self.assertEqual(self.stage(), 'AWAITING_OWNER')
        self.assertIn('приглашение', self.texts(OWNER)[-1])
        await self.owner_cmd(f'/resume_candidate {CANDIDATE}')
        self.assertEqual(self.stage(), 'INVITE_SENT')
        texts = self.texts()
        self.assertEqual(texts.count('Спасибо, согласие записано.'), 1, 'выданный шаг не повторяется')
        self.assertEqual(len(self.pdfs()), 1)

    async def test_restart_keeps_progress(self):
        await self.candidate('Интересует вакансия')
        await self.candidate(CONSENT)
        await self.restart()
        await self.owner_cmd('/resume')
        await self.candidate(CONSENT)
        self.assertEqual(len(self.to_chat()), 4)


class SingleMessagePackage(ScenarioCase):
    patch = {'after_consent': [{'id': 'only', 'type': 'text', 'text': 'Ссылка: {url}'}]}

    async def test_one_message_completes_package(self):
        await self.candidate('Интересует вакансия')
        await self.candidate(CONSENT)
        self.assertEqual(self.pdfs(), [])
        self.assertEqual(len(self.invites()), 1)
        self.assertEqual(self.stage(), 'INVITE_SENT')


class LegacyScenarioWithoutNewKeys(ScenarioCase):
    """Старый файл сценария без новых полей работает как раньше."""
    patch = {'after_consent': None, 'phrases': None, 'decline_message': None,
             'invite_message': 'Спасибо! Ссылка: https://t.me/+EvhipOH4o6IwMTFl'}

    async def test_old_scenario_keeps_pdf_then_link(self):
        await self.candidate('Интересует вакансия')
        await self.candidate(CONSENT)
        self.assertEqual([m for m, _, _ in self.to_chat()], ['sendMessage', 'sendDocument', 'sendMessage'])
        self.assertEqual(self.texts()[2], 'Спасибо! Ссылка: https://t.me/+EvhipOH4o6IwMTFl')
        self.assertEqual(self.stage(), 'INVITE_SENT')


class ScenarioValidation(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def rejects(self, patch):
        with self.assertRaises(ValueError):
            Config.load(str(write_config(self.root, patch)))

    def test_invalid_scenarios_stop_startup(self):
        self.rejects({'after_consent': []})
        self.rejects({'after_consent': [{'id': 'a', 'type': 'video'}]})
        self.rejects({'after_consent': [{'id': 'a', 'type': 'text', 'text': ''}]})
        self.rejects({'after_consent': [{'id': 'a', 'type': 'text', 'text': 'x' * 4000}]})
        self.rejects({'after_consent': [{'id': 'a', 'type': 'document', 'file': 'нет.pdf'}]})
        self.rejects({'after_consent': [{'id': 'a', 'type': 'text', 'text': 'x'},
                                        {'id': 'a', 'type': 'text', 'text': 'y'}]})
        self.rejects({'after_consent': [{'id': 'не латиница', 'type': 'text', 'text': 'x'}]})
        self.rejects({'after_consent': ['text']})
        self.rejects({'phrases': {'vacancy': []}})
        self.rejects({'decline_message': ''})

    def test_example_scenario_loads(self):
        cfg = Config.load(str(write_config(self.root, {})))
        self.assertEqual([i['id'] for i in cfg.after_consent], ['pdf', 'inv'])
        self.assertEqual(cfg.after_consent[0]['file'], cfg.presentation_file)


if __name__ == '__main__':
    unittest.main()
