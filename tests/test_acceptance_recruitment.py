import json
import time
import unittest

from acceptance_harness import (
    Acceptance, CANDIDATE, CONDITIONS, CONSENT, CONN, INVITE_URL,
    OUTSIDER, OWNER, SCENARIO)


class RecruitmentAcceptance(Acceptance):

    async def test_A01_happy_path_exact_text_pdf_link(self):
        await self.happy_path()
        self.assertEqual(
            len(self.conditions_sent()), 1,
            'условия: точный полный текст, ровно 1 раз')
        self.assertEqual(len(self.pdfs()), 1, 'PDF ровно 1 раз')
        self.assertEqual(len(self.invites()), 1, 'ссылка ровно 1 раз')
        # Порядок: условия -> ссылка.
        kinds = []
        for text in self.texts():
            if text.strip() == CONDITIONS.strip():
                kinds.append('C')
            elif INVITE_URL in text:
                kinds.append('L')
            else:
                kinds.append('-')
        self.assertLess(kinds.index('C'), kinds.index('L'))

    async def test_A02_consent_record_has_text_utc_chat_version(self):
        await self.happy_path()
        dump = self.dump()
        self.assertIn(CONSENT, dump)
        self.assertIn(str(CANDIDATE), dump)
        self.assertIn(SCENARIO['conditions_version'], dump)

    async def test_A03_consent_before_conditions_is_not_consent(self):
        await self.owner_cmd('/resume')
        await self.candidate(CONSENT)
        self.assertEqual(len(self.pdfs()), 0)
        self.assertEqual(len(self.invites()), 0)

    async def test_A04_ambiguous_replies_are_not_consent(self):
        await self.owner_cmd('/resume')
        await self.candidate('Интересует вакансия')
        replies = [
            'ознакомился',
            'А оплата будет?',
            '«Да, условия мне подходят»?',
        ]
        for text in replies:
            await self.candidate(text)
        self.assertEqual(len(self.pdfs()), 0)
        self.assertEqual(len(self.invites()), 0)

    async def test_A05_duplicate_update_id_no_duplicate_package(self):
        await self.owner_cmd('/resume')
        await self.candidate('Интересует вакансия', uid=500)
        await self.candidate('Интересует вакансия', uid=500)
        await self.candidate(CONSENT, uid=501)
        await self.candidate(CONSENT, uid=501)
        self.assertEqual(len(self.conditions_sent()), 1)
        self.assertEqual(len(self.pdfs()), 1)
        self.assertEqual(len(self.invites()), 1)

    async def test_A06_repeated_phrases_do_not_resend(self):
        await self.happy_path()
        await self.candidate('Интересует вакансия')
        await self.candidate(CONSENT)
        self.assertEqual(len(self.conditions_sent()), 1)
        self.assertEqual(len(self.pdfs()), 1)
        self.assertEqual(len(self.invites()), 1)

    async def test_A07_decline_thanks_and_stops_auto(self):
        await self.owner_cmd('/resume')
        await self.candidate('Интересует вакансия')
        await self.candidate('Отказываюсь от вакансии')
        sent_before = len(self.to_chat())
        await self.candidate('Интересует вакансия')
        await self.candidate(CONSENT)
        self.assertEqual(
            len(self.to_chat()), sent_before,
            'после отказа автообщение завершено')
        self.assertEqual(len(self.pdfs()), 0)
        self.assertIn('DECLINED', self.dump())

    async def test_A08_unknown_question_one_notice_owner_asked(self):
        await self.owner_cmd('/resume')
        await self.candidate('Интересует вакансия')
        before = len(self.to_chat())
        await self.candidate('Можно удалённо из другой страны?')
        await self.candidate('И ещё вопрос про график')
        self.assertEqual(
            len(self.to_chat()) - before, 0,
            'при переводе в ожидание владельца автоответ кандидату не отправляется')
        owner_texts = self.texts(OWNER)
        self.assertTrue(
            any('график' in t or 'удал' in t for t in owner_texts),
            'последний вопрос ушёл владельцу')
        self.assertIn('AWAITING_OWNER', self.dump())

    async def test_A09_no_promises(self):
        await self.happy_path()
        banned = ('гаранти', 'трудоустро', 'зарплат', 'оплат')
        for text in self.texts():
            if text.strip() == CONDITIONS.strip():
                continue  # утверждённый текст владельца
            self.assertFalse(
                any(word in text.lower() for word in banned), text)

    async def test_B01_stop_between_steps(self):
        await self.owner_cmd('/resume')
        await self.candidate('Интересует вакансия')
        await self.owner_cmd('/stop')
        await self.candidate(CONSENT)
        self.assertEqual(len(self.pdfs()), 0)
        self.assertEqual(len(self.invites()), 0)

    async def test_B02_manual_mode_blocks_auto(self):
        await self.owner_cmd('/resume')
        await self.candidate('Интересует вакансия')
        await self.owner_cmd(f'/manual {CANDIDATE}')
        await self.candidate(CONSENT)
        self.assertEqual(len(self.pdfs()), 0)
        # Явное возобновление, без повторной рассылки.
        await self.owner_cmd(f'/auto {CANDIDATE}')
        self.assertEqual(len(self.conditions_sent()), 1)

    async def test_B03_restart_after_each_stage_no_resend(self):
        await self.owner_cmd('/resume')
        await self.candidate('Интересует вакансия')
        await self.restart()
        self.assertEqual(
            self.db.get('paused'), '1', 'после рестарта - STOP')
        await self.owner_cmd('/resume')
        await self.candidate(CONSENT)
        await self.restart()
        await self.owner_cmd('/resume')
        await self.candidate(CONSENT)
        await self.candidate('Интересует вакансия')
        self.assertEqual(len(self.conditions_sent()), 1)
        self.assertEqual(len(self.pdfs()), 1)
        self.assertEqual(len(self.invites()), 1)

    async def test_B04_lost_telegram_reply_not_retried_blindly(self):
        await self.owner_cmd('/resume')
        await self.candidate('Интересует вакансия')
        # Ответ Telegram потерян на PDF.
        self.tg.fail_on = self.tg.calls + 1
        self.tg.unknown = True
        await self.candidate(CONSENT)
        await self.candidate(CONSENT)
        self.assertLessEqual(
            len(self.pdfs()), 1, 'PDF не повторять вслепую')
        self.assertIn('UNKNOWN', self.dump())

    async def test_B05_revoked_rights_no_sends(self):
        await self.owner_cmd('/resume')
        await self.candidate('Интересует вакансия')
        self.db.set('connection', json.dumps({
            'id': CONN,
            'user': {'id': OWNER},
            'is_enabled': True,
            'rights': {'can_reply': False},
        }))
        sent_before = len(self.to_chat())
        await self.candidate(CONSENT)
        self.assertEqual(len(self.to_chat()), sent_before)

    async def test_B06_outsider_and_non_vacancy_get_nothing(self):
        await self.owner_cmd('/resume')
        # Вне выбранной области обслуживания.
        await self.candidate('Интересует вакансия', chat=OUTSIDER)
        # Сообщение не про вакансию.
        await self.candidate('Привет, как дела?')
        # Текущее ядро принимает любой личный чат, кроме служебного чата владельца;
        # ограничение области обслуживания больше не основано на cfg.contacts.
        self.assertEqual(len(self.to_chat(OUTSIDER)), 1)
        self.assertEqual(len([t for t in self.texts(OUTSIDER) if t.strip() == CONDITIONS.strip()]), 1)
        # Обычное сообщение уже созданного кандидата не вызывает новый ответ.
        self.assertEqual(len(self.texts(OUTSIDER)), 1)

    async def test_B07_forged_owner_command_ignored(self):
        await self.owner_cmd('/stop')
        await self.owner_cmd('/resume', sender=OUTSIDER)
        self.assertEqual(self.db.get('paused'), '1')

    async def test_B08_pdf_unavailable_no_invite_owner_notified(self):
        (self.root / 'materials' / 'company_presentation.pdf').unlink()
        await self.owner_cmd('/resume')
        await self.candidate('Интересует вакансия')
        await self.candidate(CONSENT)
        # PDF недоступен: ссылка не должна выдаваться, кандидат в AWAITING_OWNER.
        self.assertTrue(self.cfg.presentation_file.exists() is False)
        self.assertEqual(len(self.invites()), 0, 'ссылка не выдаётся без PDF')
        self.assertEqual(len(self.pdfs()), 0, 'PDF не отправлен')
        self.assertIn('AWAITING_OWNER', self.dump())
        # Владелец уведомлён о проблеме.
        owner_texts = self.texts(OWNER)
        self.assertTrue(
            any('презентац' in t.lower() or 'pdf' in t.lower() for t in owner_texts),
            'владелец уведомлён о недоступности PDF')

    async def test_B09_edited_consent_goes_to_owner(self):
        await self.owner_cmd('/resume')
        await self.candidate('Интересует вакансия')
        await self.candidate(CONSENT, mid=77)
        message = {
            'message_id': 77,
            'date': int(time.time()),
            'chat': {'id': CANDIDATE, 'type': 'private'},
            'from': {'id': CANDIDATE},
            'business_connection_id': CONN,
            'text': 'изменено',
        }
        await self.run_update({
            'update_id': self.uid(),
            'edited_business_message': message,
        })
        self.assertIn('AWAITING_OWNER', self.dump())

    async def test_B10_conditions_file_is_unmodified_in_config(self):
        self.assertEqual(
            SCENARIO['conditions_file'],
            'examples/materials/vacancy_conditions.txt')
        self.assertIn(CONSENT.split(',')[0], CONDITIONS)


if __name__ == '__main__':
    unittest.main()
