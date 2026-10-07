"""Приёмка ТЗ 5.0, раздел 5 (офлайн-часть). Каждый тест = пункт протокола docs/TEST_PROTOCOL.md.
Пока коллеги не реализовали workflow - КРАСНЫЕ тесты это нормально (это и есть "NOT RUN -> FAIL").
Моки НЕ заменяют проверку на Windows и настоящем аккаунте.
"""
import unittest
from acceptance_harness import *


class RecruitmentAcceptance(Acceptance):

    async def test_A01_happy_path_exact_text_pdf_link(self):
        await self.happy_path()
        self.assertEqual(len(self.conditions_sent()), 1, 'условия: точный полный текст, ровно 1 раз')
        self.assertEqual(len(self.pdfs()), 1, 'PDF ровно 1 раз')
        self.assertEqual(len(self.invites()), 1, 'ссылка ровно 1 раз')
        # порядок: условия -> PDF -> ссылка
        kinds = ['C' if t.strip() == CONDITIONS.strip() else 'L' if INVITE_URL in t else '-' for t in self.texts()]
        self.assertLess(kinds.index('C'), kinds.index('L'))

    async def test_A02_consent_record_has_text_utc_chat_version(self):
        await self.happy_path(); d = self.dump()
        self.assertIn(CONSENT, d); self.assertIn(str(CANDIDATE), d)
        self.assertIn(SCENARIO['conditions_version'], d)

    async def test_A03_consent_before_conditions_is_not_consent(self):
        await self.owner_cmd('/resume'); await self.candidate(CONSENT)
        self.assertEqual(len(self.pdfs()), 0); self.assertEqual(len(self.invites()), 0)

    async def test_A04_ambiguous_replies_are_not_consent(self):
        await self.owner_cmd('/resume'); await self.candidate('Интересует вакансия')
        for t in ['да', 'Нет', 'ознакомился', 'А оплата будет?', '«Да, условия мне подходят»?']:
            await self.candidate(t)
        self.assertEqual(len(self.pdfs()), 0); self.assertEqual(len(self.invites()), 0)

    async def test_A05_duplicate_update_id_no_duplicate_package(self):
        await self.owner_cmd('/resume')
        await self.candidate('Интересует вакансия', uid=500); await self.candidate('Интересует вакансия', uid=500)
        await self.candidate(CONSENT, uid=501); await self.candidate(CONSENT, uid=501)
        self.assertEqual(len(self.conditions_sent()), 1); self.assertEqual(len(self.pdfs()), 1)
        self.assertEqual(len(self.invites()), 1)

    async def test_A06_repeated_phrases_do_not_resend(self):
        await self.happy_path()
        await self.candidate('Интересует вакансия'); await self.candidate(CONSENT)
        self.assertEqual(len(self.conditions_sent()), 1); self.assertEqual(len(self.pdfs()), 1)
        self.assertEqual(len(self.invites()), 1)

    async def test_A07_decline_thanks_and_stops_auto(self):
        await self.owner_cmd('/resume'); await self.candidate('Интересует вакансия')
        await self.candidate('Нет, не подходит'); n = len(self.to_chat())
        await self.candidate('Интересует вакансия'); await self.candidate(CONSENT)
        self.assertEqual(len(self.to_chat()), n, 'после отказа автообщение завершено')
        self.assertEqual(len(self.pdfs()), 0)
        self.assertIn('DECLINED', self.dump())

    async def test_A08_unknown_question_one_notice_owner_asked_pause(self):
        await self.owner_cmd('/resume'); await self.candidate('Интересует вакансия')
        before = len(self.to_chat()); await self.candidate('Можно удалённо из другой страны?')
        await self.candidate('И ещё вопрос про график')
        self.assertEqual(len(self.to_chat()) - before, 1, 'кандидату одно уведомление')
        self.assertTrue(any('график' in t or 'удал' in t for t in self.texts(OWNER)), 'вопрос ушёл владельцу')
        self.assertIn('AWAITING_OWNER', self.dump())

    async def test_A09_no_promises(self):
        await self.happy_path()
        bad = ('гаранти', 'трудоустро', 'зарплат', 'оплат')
        for t in self.texts():
            if t.strip() == CONDITIONS.strip(): continue   # утверждённый текст владельца
            self.assertFalse(any(w in t.lower() for w in bad), t)

    async def test_B01_stop_between_steps(self):
        await self.owner_cmd('/resume'); await self.candidate('Интересует вакансия')
        await self.owner_cmd('/stop'); await self.candidate(CONSENT)
        self.assertEqual(len(self.pdfs()), 0); self.assertEqual(len(self.invites()), 0)

    async def test_B02_manual_mode_blocks_auto(self):
        await self.owner_cmd('/resume'); await self.candidate('Интересует вакансия')
        await self.owner_cmd(f'/manual {CANDIDATE}'); await self.candidate(CONSENT)
        self.assertEqual(len(self.pdfs()), 0)
        await self.owner_cmd(f'/auto {CANDIDATE}')          # явное возобновление, без повторной рассылки
        self.assertEqual(len(self.conditions_sent()), 1)

    async def test_B03_restart_after_each_stage_no_resend(self):
        await self.owner_cmd('/resume'); await self.candidate('Интересует вакансия')
        await self.restart(); self.assertEqual(self.db.get('paused'), '1', 'после рестарта - STOP')
        await self.owner_cmd('/resume'); await self.candidate(CONSENT)
        await self.restart(); await self.owner_cmd('/resume')
        await self.candidate(CONSENT); await self.candidate('Интересует вакансия')
        self.assertEqual(len(self.conditions_sent()), 1); self.assertEqual(len(self.pdfs()), 1)
        self.assertEqual(len(self.invites()), 1)

    async def test_B04_lost_telegram_reply_not_retried_blindly(self):
        await self.owner_cmd('/resume'); await self.candidate('Интересует вакансия')
        self.tg.fail_on = self.tg.calls + 1; self.tg.unknown = True       # ответ Telegram потерян на PDF
        await self.candidate(CONSENT); await self.candidate(CONSENT)
        self.assertLessEqual(len(self.pdfs()), 1, 'PDF не повторять вслепую')
        self.assertIn('UNKNOWN', self.dump())

    async def test_B05_revoked_rights_no_sends(self):
        await self.owner_cmd('/resume'); await self.candidate('Интересует вакансия')
        self.db.set('connection', json.dumps({'id': CONN, 'user': {'id': OWNER}, 'is_enabled': True,
                                              'rights': {'can_reply': False}}))
        n = len(self.to_chat()); await self.candidate(CONSENT)
        self.assertEqual(len(self.to_chat()), n)

    async def test_B06_outsider_and_non_vacancy_get_nothing(self):
        await self.owner_cmd('/resume')
        await self.candidate('Интересует вакансия', chat=OUTSIDER)   # вне выбранной области
        await self.candidate('Привет, как дела?')                    # не про вакансию
        self.assertEqual(self.to_chat(OUTSIDER), [])
        self.assertEqual(self.conditions_sent(), [], 'условия не на любое сообщение')

    async def test_B07_forged_owner_command_ignored(self):
        await self.owner_cmd('/stop'); await self.owner_cmd('/resume', sender=OUTSIDER)
        self.assertEqual(self.db.get('paused'), '1')

    async def test_B08_pdf_unavailable_no_invite_owner_notified(self):
        (self.root / 'materials' / 'company_presentation.pdf').unlink()
        await self.owner_cmd('/resume'); await self.candidate('Интересует вакансия'); await self.candidate(CONSENT)
        self.assertEqual(len(self.invites()), 0, 'без PDF ссылку не выдаём')
        self.assertTrue(self.texts(OWNER), 'владелец уведомлён')

    async def test_B09_edited_consent_goes_to_owner(self):
        await self.owner_cmd('/resume'); await self.candidate('Интересует вакансия')
        uid = await self.candidate(CONSENT, mid=77)
        m = {'message_id': 77, 'date': int(time.time()), 'chat': {'id': CANDIDATE, 'type': 'private'},
             'from': {'id': CANDIDATE}, 'business_connection_id': CONN, 'text': 'изменено'}
        await self.run_update({'update_id': self.uid(), 'edited_business_message': m})
        self.assertIn('AWAITING_OWNER', self.dump())

    async def test_B10_conditions_file_is_unmodified_in_config(self):
        self.assertEqual(SCENARIO['conditions_file'], 'examples/materials/vacancy_conditions.txt')
        self.assertTrue(CONSENT.split(',')[0] in CONDITIONS)

if __name__ == '__main__':
    unittest.main()
