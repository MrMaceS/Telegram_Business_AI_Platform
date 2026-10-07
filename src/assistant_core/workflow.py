"""Deterministic recruitment workflow. Fully rule-based, no external AI dependencies."""
import asyncio
import json
import logging
import time
from .telegram import APIError, DeliveryUnknown
from .recruitment_parser import is_vacancy_inquiry, is_explicit_consent, is_explicit_decline

logger = logging.getLogger(__name__)

HELP = (
    'Команды: /status, /stop, /resume, /manual CHAT, /auto CHAT, '
    '/resume_candidate CHAT, /reply CHAT TEXT, /pending. '
    'После запуска всегда STOP. Для запуска автоответов: /resume.'
)


class Engine:
    def __init__(self, cfg, store, telegram, ai=None):
        self.cfg, self.db, self.tg, self.ai = cfg, store, telegram, ai
        # Один send/STOP замок на весь процесс
        self.gate = asyncio.Lock()
        self.chats = {}

    def connection(self):
        return json.loads(self.db.get('connection', '{}'))

    def connected(self, connection):
        c = self.connection()
        return (
            c.get('id') == connection
            and c.get('is_enabled') is True
            and c.get('user', {}).get('id') == self.cfg.business_owner_id
        )

    def can_send(self, chat, epoch, owner_action=False):
        c = self.db.conversation(chat)
        if not c or c['epoch'] != epoch:
            return False
        # Разрешаем отправку в диалоги кандидатов (исключая служебные ID владельцев)
        is_permitted = (chat in self.cfg.contacts) or (chat != self.cfg.owner_id and chat != self.cfg.business_owner_id)
        return bool(
            is_permitted
            and self.db.get('paused') == '0'
            and (owner_action or c['mode'] == 'AUTO')
            and self.connected(c['connection'])
            and self.connection().get('rights', {}).get('can_reply')
            and 0 <= time.time() - c['last_in'] < 24 * 3600
        )

    async def owner(self, text, key):
        await self.deliver(self.cfg.owner_id, text[:3900], key, owner_notice=True)

    async def deliver(self, chat, text, key, epoch=None, document=None, owner_notice=False, owner_action=False):
        method = 'sendDocument' if document else 'sendMessage'
        async with self.gate:
            if owner_notice:
                if chat != self.cfg.owner_id:
                    raise ValueError('Invalid owner target')
            elif not self.can_send(chat, epoch, owner_action):
                self.db.log('SEND_BLOCKED', chat, key)
                return False
            if not self.db.reserve(key, chat, method):
                return False
            payload = {'chat_id': chat}
            if not owner_notice:
                payload['business_connection_id'] = self.db.conversation(chat)['connection']
            payload['caption' if document else 'text'] = text[:1000 if document else 3900]
            try:
                result = await self.tg.call(method, payload, document=document)
            except DeliveryUnknown:
                self.db.outgoing_result(key, 'UNKNOWN')
                self.db.log('DELIVERY_UNKNOWN', chat, key)
                return False
            except APIError:
                self.db.outgoing_result(key, 'FAILED')
                self.db.log('DELIVERY_FAILED', chat, key)
                return False
            self.db.outgoing_result(key, 'SENT', result)
            if not owner_notice:
                self.db.execute(
                    'INSERT OR IGNORE INTO messages(chat,mid,direction,body) VALUES(?,?,?,?)',
                    (chat, result['message_id'], 'assistant', text[:8000])
                )
            return True

    async def admit(self, update):
        """Фиксация и валидация событий до передачи в обработчики."""
        uid = update['update_id']
        if 'business_connection' in update:
            c = update['business_connection']
            if c.get('user', {}).get('id') != self.cfg.business_owner_id:
                self.db.log('CONNECTION_REJECTED')
                self.db.complete(uid)
                return False
            async with self.gate:
                self.db.set('connection', json.dumps(c))
                self.db.execute('UPDATE conversations SET epoch=epoch+1')
            self.db.complete(uid)
            return False

        if 'message' in update:
            m = update['message']
            if (
                m.get('from', {}).get('id') != self.cfg.owner_id
                or m.get('chat', {}).get('id') != self.cfg.owner_id
                or m['chat'].get('type') != 'private'
            ):
                self.db.complete(uid)
                return False
            return True

        if 'edited_business_message' in update or 'deleted_business_messages' in update:
            m = update.get('edited_business_message') or update['deleted_business_messages']
            chat = m['chat']['id']
            if self.connected(m.get('business_connection_id')):
                async with self.gate:
                    is_consent = False
                    if 'message_id' in m:
                        self.db.execute(
                            'UPDATE messages SET body=? WHERE chat=? AND mid=?',
                            ((m.get('text') or '[изменённое сообщение без текста]')[:8000], chat, m['message_id'])
                        )
                        is_consent = bool(self.db.db.execute(
                            'SELECT 1 FROM consents WHERE chat=? AND mid=?', (chat, m['message_id'])
                        ).fetchone())
                    else:
                        for mid in m.get('message_ids', []):
                            self.db.execute('UPDATE messages SET deleted=1 WHERE chat=? AND mid=?', (chat, mid))
                    self.db.mode(chat, 'AWAITING_OWNER')
                notice = (
                    f'Изменение/удаление сообщения согласия в {chat}!'
                    if is_consent
                    else f'Изменение/удаление сообщения в {chat}.'
                )
                await self.owner(f'{notice} Автоответчик приостановлен.', f'edit:{uid}')
            self.db.complete(uid)
            return False

        m = update.get('business_message')
        if not m:
            self.db.complete(uid)
            return False

        chat = m['chat']['id']
        # Допуск: только личные чаты кандидатов через авторизованную Business связь
        if m['chat'].get('type') != 'private' or not self.connected(m.get('business_connection_id')):
            self.db.complete(uid)
            return False

        # Служебные чаты владельца и другие боты не являются кандидатами
        if chat in (self.cfg.owner_id, self.cfg.business_owner_id) or m.get('from', {}).get('is_bot'):
            self.db.log('NOT_CANDIDATE', chat)
            self.db.complete(uid)
            return False

        sender = m.get('from', {}).get('id')
        if sender == self.cfg.business_owner_id and not m.get('sender_business_bot'):
            async with self.gate:
                self.db.mode(chat, 'MANUAL')
                self.db.execute(
                    'INSERT OR IGNORE INTO messages(chat,mid,direction,body) VALUES(?,?,?,?)',
                    (chat, m['message_id'], 'assistant', (m.get('text') or m.get('caption') or '[ручное сообщение владельца]')[:8000])
                )
            self.db.complete(uid)
            return False

        if sender != chat or m.get('sender_business_bot'):
            self.db.complete(uid)
            return False

        body = m.get('text') or m.get('caption') or '[вложение/неподдерживаемое сообщение]'
        self.db.incoming(chat, m['business_connection_id'], m['message_id'], body, m['date'])
        return True

    async def process(self, update):
        uid = update['update_id']
        self.db.complete(uid, 'RUNNING')
        try:
            if 'message' in update:
                await self.command(update['message'].get('text', ''), uid)
            else:
                m = update['business_message']
                chat = m['chat']['id']
                lock = self.chats.setdefault(chat, asyncio.Lock())
                async with lock:
                    await self.business(m, uid)
            self.db.complete(uid)
        except Exception as exc:
            self.db.log('PROCESS_ERROR', detail=type(exc).__name__)
            self.db.complete(uid, 'REVIEW')
            await self.owner(f'Событие {uid} требует проверки. /pending', f'error:{uid}')

    async def command(self, text, uid):
        bits = text.split(maxsplit=2)
        command = bits[0].lower() if bits else ''
        key = f'command:{uid}'

        if command in ('/start', '/help'):
            await self.owner(HELP, key)
            return

        if command in ('/stop', '/resume'):
            async with self.gate:
                self.db.set('paused', '1' if command == '/stop' else '0')
                self.db.execute('UPDATE conversations SET epoch=epoch+1')
                self.db.log('GLOBAL_MODE', detail=command)
            await self.owner(
                'STOP сохранён.' if command == '/stop' else 'Общая пауза снята. MANUAL/ожидание в отдельных диалогах сохранены.',
                key
            )
            return

        if command == '/status':
            rows = self.db.db.execute(
                'SELECT chat,mode,task_id,task_status FROM conversations ORDER BY chat LIMIT 30'
            ).fetchall()
            await self.owner('STOP=' + str(self.db.get('paused')) + '\n' + '\n'.join(str(dict(x)) for x in rows), key)
            return

        if command == '/pending':
            rows = self.db.db.execute(
                "SELECT id,chat,status FROM outgoing WHERE status IN ('UNKNOWN','FAILED') ORDER BY id DESC LIMIT 15"
            ).fetchall()
            events = self.db.db.execute("SELECT id FROM inbox WHERE status='REVIEW' LIMIT 15").fetchall()
            await self.owner(
                'Отправки: ' + str([dict(x) for x in rows]) + '\nСобытия на сверку: ' + str([x[0] for x in events]),
                key
            )
            return

        if len(bits) < 2 or not bits[1].isdigit():
            await self.owner(HELP, key)
            return

        chat = int(bits[1])
        row = self.db.conversation(chat)
        if not row:
            await self.owner(f'Диалог {chat} не найден в базе.', key)
            return

        if command in ('/manual', '/auto'):
            async with self.gate:
                self.db.mode(chat, 'MANUAL' if command == '/manual' else 'AUTO')
            await self.owner(f'Режим {chat} обновлён.', key)
            return

        if command == '/resume_candidate':
            resumed_stage = self.db.resume_candidate(chat)
            if resumed_stage:
                await self.owner(f'Диалог с {chat} возобновлен с этапа {resumed_stage}.', key)
            else:
                await self.owner(f'Не удалось возобновить диалог {chat}.', key)
            return

        if command == '/reply' and len(bits) == 3:
            ok = await self.deliver(chat, bits[2], key + ':approved', row['epoch'], owner_action=True)
            await self.owner('Утверждённый текст отправлен.' if ok else 'Не отправлено. Проверьте STOP/права.', key)
            return

        await self.owner(HELP, key)

    async def business(self, m, uid):
        chat = m['chat']['id']
        key = f'event:{uid}'
        row = self.db.conversation(chat)
        text = (m.get('text') or m.get('caption') or '').strip()
        epoch = row['epoch']
        stage = row['task_status'] or 'NEW'

        # Остановка диалога по запросу кандидата
        if text.lower() in ('/stop', 'стоп', 'не отвечай'):
            async with self.gate:
                self.db.mode(chat, 'MANUAL')
            await self.owner(f'Кандидат {chat} запросил остановку автообщения.', key + ':stop')
            return

        # Игнорирование устаревших сообщений
        latest = self.db.db.execute(
            "SELECT MAX(mid) FROM messages WHERE chat=? AND direction='user' AND deleted=0",
            (chat,)
        ).fetchone()[0]
        if m['message_id'] != latest:
            self.db.log('SUPERSEDED_MESSAGE', chat, key)
            return

        if not self.can_send(chat, epoch):
            self.db.log('AUTO_SKIPPED', chat, key)
            return

        if stage == 'NEW':
            if is_vacancy_inquiry(text):
                cond_text = self.cfg.conditions_file.read_text(encoding='utf-8')
                async with self.gate:
                    if self.db.conversation(chat)['epoch'] != epoch:
                        return
                    self.db.set_stage(chat, 'CONDITIONS_SENT')
                await self.deliver(chat, cond_text, key + ':cond', epoch)
            return

        if stage == 'CONDITIONS_SENT':
            # Явное согласие
            if is_explicit_consent(text):
                async with self.gate:
                    if self.db.conversation(chat)['epoch'] != epoch:
                        return
                    self.db.record_consent(chat, m['message_id'], text, self.cfg.conditions_version)
                    self.db.set_stage(chat, 'CONSENT_RECORDED')

                # Отправка презентации компании в виде файла PDF
                pres_path = self.cfg.presentation_file
                ok_doc = await self.deliver(
                    chat,
                    'Условия согласованы. Направляю презентацию компании:',
                    key + ':pdf',
                    epoch,
                    document=pres_path
                )
                if not ok_doc:
                    return

                async with self.gate:
                    self.db.set_stage(chat, 'PRESENTATION_SENT')

                # Отправка приглашения ссылкой
                invite_msg = (
                    f"Спасибо! Ваше согласие зафиксировано. Ознакомьтесь с презентацией компании "
                    f"и присоединитесь к группе: {self.cfg.group_invite_url}"
                )
                ok_inv = await self.deliver(chat, invite_msg, key + ':inv', epoch)
                if ok_inv:
                    async with self.gate:
                        self.db.set_stage(chat, 'INVITE_SENT')
                return

            # Явный отказ
            if is_explicit_decline(text):
                async with self.gate:
                    if self.db.conversation(chat)['epoch'] != epoch:
                        return
                    self.db.set_stage(chat, 'DECLINED')
                await self.deliver(chat, 'Спасибо за отклик! Желаем успехов.', key + ':dec', epoch)
                return

            # В. Неизвестный вопрос / сомнения -> перевод на владельца
            async with self.gate:
                if self.db.conversation(chat)['epoch'] != epoch:
                    return
                self.db.set_stage(chat, 'AWAITING_OWNER', previous_stage=stage)
                self.db.mode(chat, 'AWAITING_OWNER')

            await self.deliver(chat, self.cfg.unknown_question_text, key + ':ack', epoch)
            await self.owner(
                f'Кандидат {chat} задал вопрос на этапе условий:\n"{text}"\n'
                f'Ответить: /reply {chat} ТЕКСТ\n'
                f'Возобновить автоответчик: /resume_candidate {chat}',
                key + ':esc'
            )
            return

        if stage in ('CONSENT_RECORDED', 'PRESENTATION_SENT', 'INVITE_SENT'):
            if is_explicit_consent(text) or is_vacancy_inquiry(text):
                return
            async with self.gate:
                if self.db.conversation(chat)['epoch'] != epoch:
                    return
                self.db.set_stage(chat, 'AWAITING_OWNER', previous_stage=stage)
                self.db.mode(chat, 'AWAITING_OWNER')
            await self.deliver(chat, self.cfg.unknown_question_text, key + ':ack_post', epoch)
            await self.owner(f'Кандидат {chat} (этап {stage}) прислал сообщение:\n"{text}"', key + ':esc_post')