"""Deterministic recruitment workflow. Fully rule-based, no external AI dependencies."""
import asyncio
import json
import re
import time
from .telegram import APIError, DeliveryUnknown
from .recruitment_parser import is_vacancy_inquiry, is_explicit_consent, is_explicit_decline

HELP = (
    'Команды: /status, /stop, /resume, /manual CHAT, /auto CHAT, '
    '/resume_candidate CHAT, /reply CHAT TEXT, /pending. '
    'После запуска всегда STOP. Для запуска автоответов: /resume.'
)

MAX_ATTEMPTS = 3
MAX_TEXT = 3900
CANDIDATE_STOP_WORDS = frozenset({'stop', 'стоп', 'не отвечай'})
PLEASANTRIES = frozenset({'спасибо', 'благодарю', 'понятно', 'понял', 'поняла', 'хорошо',
                          'ок', 'ok', 'отлично', 'ясно'})
INCOMPLETE = ('CONSENT_RECORDED', 'PRESENTATION_SENT')
DEFAULT_INVITE = ('Спасибо! Ваше согласие зафиксировано. Ознакомьтесь с презентацией компании '
                  'и присоединитесь к группе:')


def _plain(text):
    return ' '.join(re.sub(r'[^\w\s]', ' ', text.lower().replace('ё', 'е')).split())


def _is_pleasantry(text):
    words = _plain(text).split()
    return 0 < len(words) <= 3 and all(w in PLEASANTRIES for w in words)


MAX_REPEAT_WORDS = 8


def _is_repeat_before_consent(text, vacancy_phrases=()):
    if '?' in text or len(_plain(text).split()) > MAX_REPEAT_WORDS:
        return False
    return is_vacancy_inquiry(text, vacancy_phrases) or _is_pleasantry(text)


class Engine:
    def __init__(self, cfg, store, telegram, ai=None):
        self.cfg, self.db, self.tg, self.ai = cfg, store, telegram, ai
        # Один send/STOP замок на весь процесс
        self.gate = asyncio.Lock()
        self.chats = {}

    # ------------------------------------------------------------------ допуск и отправка

    def connection(self):
        return json.loads(self.db.get('connection', '{}'))

    def connected(self, connection):
        c = self.connection()
        return (
            c.get('id') == connection
            and c.get('is_enabled') is True
            and c.get('user', {}).get('id') == self.cfg.business_owner_id
        )

    def _block_reason(self, chat, epoch, owner_action=False):
        """Почему отправка запрещена (None, если разрешена). Те же условия, что раньше в can_send."""
        c = self.db.conversation(chat)
        if not c:
            return 'NO_CONVERSATION'
        if c['epoch'] != epoch:
            return 'EPOCH'
        # config запрещает владельцам быть в contacts, поэтому проверка сводится к исключению служебных чатов
        if chat in (self.cfg.owner_id, self.cfg.business_owner_id):
            return 'SERVICE_CHAT'
        if self.db.get('paused') != '0':
            return 'STOP'
        if not owner_action and c['mode'] != 'AUTO':
            return 'MODE'
        if not self.connected(c['connection']) or not self.connection().get('rights', {}).get('can_reply'):
            return 'CONNECTION'
        if not 0 <= time.time() - c['last_in'] < 24 * 3600:
            return 'WINDOW'
        return None

    def can_send(self, chat, epoch, owner_action=False):
        return self._block_reason(chat, epoch, owner_action) is None

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
                return None
            if not self.db.reserve(key, chat, method):
                return None
            payload = {'chat_id': chat}
            if not owner_notice:
                payload['business_connection_id'] = self.db.conversation(chat)['connection']
            payload['caption' if document else 'text'] = text[:1000 if document else MAX_TEXT]
            try:
                result = await self.tg.call(method, payload, document=document)
            except DeliveryUnknown:
                self.db.outgoing_result(key, 'UNKNOWN')
                self.db.log('DELIVERY_UNKNOWN', chat, key)
                return None
            except APIError:
                self.db.outgoing_result(key, 'FAILED')
                self.db.log('DELIVERY_FAILED', chat, key)
                return None
            self.db.outgoing_result(key, 'SENT', result)
            if not owner_notice:
                self.db.execute(
                    'INSERT OR IGNORE INTO messages(chat,mid,direction,body) VALUES(?,?,?,?)',
                    (chat, result['message_id'], 'assistant', text[:8000])
                )
            return result

    # ------------------------------------------------------------------ шаги сценария

    def _attempts(self, prefix):
        """Попытки отправки шага: [(status, telegram_id), ...]. Ключи prefix:1, prefix:2, ..."""
        found, n = [], 1
        while True:
            row = self.db.db.execute('SELECT status,telegram_id FROM outgoing WHERE dedupe=?',
                                     (f'{prefix}:{n}',)).fetchone()
            if not row:
                return found
            found.append((row[0], row[1]))
            n += 1

    async def _step(self, chat, prefix, text, epoch, document=None):
        """Идемпотентная отправка одного шага сценария.
        Ключ зависит от (чат, версия условий, шаг), а не от update_id: повторное событие
        не дублирует рассылку. Возвращает (статус, telegram_id):
        SENT | BLOCKED (не отправляли) | FAILED (точно не доставлено) | UNKNOWN (на сверку)."""
        attempts = self._attempts(prefix)
        for status, tid in attempts:
            if status == 'SENT':
                return 'SENT', tid
        if any(s in ('UNKNOWN', 'SENDING') for s, _ in attempts):
            return 'UNKNOWN', None          # вслепую не повторяем
        if len(attempts) >= MAX_ATTEMPTS:
            return 'FAILED', None
        key = f'{prefix}:{len(attempts) + 1}'
        res = await self.deliver(chat, text, key, epoch, document=document)
        if res:
            return 'SENT', res.get('message_id')
        row = self.db.db.execute('SELECT status FROM outgoing WHERE dedupe=?', (key,)).fetchone()
        return (row[0] if row else 'BLOCKED'), None

    def _set_stage_safe(self, chat, stage, **kw):
        """Отправка уже состоялась: факт надо сохранить. Параллельная смена этапа
        (правка сообщения, эскалация) не должна ронять обработку."""
        try:
            return self.db.set_stage(chat, stage, **kw)
        except ValueError:
            self.db.log('STAGE_CONFLICT', chat, stage)
            return False

    async def _step_problem(self, chat, key, what, status, epoch):
        if status == 'BLOCKED':
            reason = self._block_reason(chat, epoch)
            self.db.log('STEP_BLOCKED', chat, f'{what} {reason}')
            if reason in ('STOP', 'CONNECTION', 'WINDOW'):
                await self.owner(
                    f'Диалог {chat}: шаг «{what}» не выполнен ({reason}). Этап сохранён, '
                    f'продолжение — при следующем сообщении кандидата после снятия причины.',
                    f'{key}:problem')

            return

        async with self.gate:
            self._set_stage_safe(chat, 'AWAITING_OWNER', reason=f'{what} {status}')
            self.db.mode(chat, 'AWAITING_OWNER')
        hint = ('Возможно, сообщение доставлено: проверьте переписку, при необходимости /manual.'
                if status == 'UNKNOWN' else 'Доставка не удалась (см. /pending).')
        await self.owner(
            f'Диалог {chat}: шаг «{what}» — {status}. {hint} Автоответчик в этом диалоге приостановлен. '
            f'После решения: /resume_candidate {chat}',
            f'{key}:problem')

    async def _escalate(self, chat, text, reason, key, epoch):
        """Один ответ кандидату, затем пауза диалога и вопрос владельцу.
        Подтверждение отправляется ДО смены режима: mode() увеличивает epoch и меняет режим,
        после этого can_send() заблокировал бы собственное подтверждение."""
        ack = await self.deliver(chat, self.cfg.unknown_question_text, f'{key}:ack', epoch)
        async with self.gate:
            self._set_stage_safe(chat, 'AWAITING_OWNER', reason=reason)
            self.db.mode(chat, 'AWAITING_OWNER')
        await self.owner(
            f'Кандидат {chat}: {reason}.\n"{text[:500]}"\n'
            + ('' if ack else '⚠️ Подтверждение кандидату НЕ отправлено.\n')
            + f'Ответить: /reply {chat} ТЕКСТ\n'
            f'Возобновить автоответчик: /resume_candidate {chat}',
            f'{key}:esc')

    def _invite_text(self):
        base = (self.cfg.invite_message_text or DEFAULT_INVITE).strip()
        url = self.cfg.group_invite_url
        return base if url in base else f'{base} {url}'

    async def _send_conditions(self, chat, key, epoch):
        version = self.cfg.conditions_version
        cond_text = self.cfg.conditions_file.read_text(encoding='utf-8')
        if len(cond_text) > MAX_TEXT:
            # deliver() молча обрезал бы условия; ТЗ требует полный текст без изменений
            raise ValueError('Conditions text is longer than one Telegram message')
        status, mid = await self._step(chat, f'cond:{chat}:{version}', cond_text, epoch)
        if status == 'SENT':
            self._set_stage_safe(chat, 'CONDITIONS_SENT', conditions_version=version, conditions_mid=mid)
        else:
            await self._step_problem(chat, key, 'условия', status, epoch)

    def _package(self):
        """Сообщения после согласия по порядку (after_consent в сценарии).
        Без него — прежний пакет: PDF, затем ссылка; id 'pdf'/'inv' совпадают со старыми ключами
        отправки, поэтому кандидаты «в пути» не получат повтор после обновления."""
        if self.cfg.after_consent:
            return self.cfg.after_consent
        return (
            {'id': 'pdf', 'type': 'document', 'file': self.cfg.presentation_file,
             'caption': self.cfg.presentation_caption, 'name': 'презентация'},
            {'id': 'inv', 'type': 'text', 'text': self._invite_text(), 'name': 'приглашение'},
        )

    async def _finish_package(self, chat, key, epoch):
        """Досылает недостающие сообщения пакета. Состояние-ориентированно и идемпотентно:
        вызывается и после согласия, и при следующем сообщении, и по команде владельца.
        Этапы прежние: PRESENTATION_SENT — выдано первое сообщение, INVITE_SENT — весь пакет.
        Возвращает True, если пакет выдан полностью."""
        version = self.cfg.conditions_version
        cand = self.db.candidate(chat)
        stage = cand['stage'] if cand else None
        if stage not in INCOMPLETE:
            return stage == 'INVITE_SENT'
        items = self._package()
        # PRESENTATION_SENT: первое сообщение уже выдано, даже если ключа отправки нет
        for n, item in enumerate(items[1:] if stage == 'PRESENTATION_SENT' else items):
            document = item['file'] if item['type'] == 'document' else None
            # Доступность файла проверяется ДО отправки (test_B08)
            if document is not None and not document.is_file():
                await self._step_problem(chat, key, item['name'], 'FAILED', epoch)
                return False
            text = item['caption'] if document is not None else item['text']
            text = text.replace('{url}', self.cfg.group_invite_url)
            status, _ = await self._step(chat, f"{item['id']}:{chat}:{version}", text, epoch, document=document)
            if status != 'SENT':
                await self._step_problem(chat, key, item['name'], status, epoch)
                return False
            if stage == 'CONSENT_RECORDED' and n == 0:
                self._set_stage_safe(chat, 'PRESENTATION_SENT')
        if self.db.candidate(chat)['stage'] == 'CONSENT_RECORDED':
            self._set_stage_safe(chat, 'PRESENTATION_SENT')   # пакет из одного сообщения
        self._set_stage_safe(chat, 'INVITE_SENT')
        return self.db.candidate(chat)['stage'] == 'INVITE_SENT'

    async def _complete_if_needed(self, chat, key):
        cand = self.db.candidate(chat)
        conv = self.db.conversation(chat)
        if cand and conv and cand['stage'] in INCOMPLETE:
            done = await self._finish_package(chat, key, conv['epoch'])
            if done:
                await self.owner(f'Диалог {chat}: пакет (презентация и ссылка) выдан полностью.', f'{key}:done')

    # ------------------------------------------------------------------ приём событий

    async def admit(self, update):
        """Обёртка: необработанное исключение не должно ронять главный цикл.
        Иначе «ядовитое» событие остаётся RECEIVED и валит процесс при каждом рестарте."""
        try:
            return await self._admit(update)
        except Exception as exc:
            uid = update.get('update_id')
            self.db.log('ADMIT_ERROR', detail=type(exc).__name__)
            self.db.complete(uid, 'REVIEW')
            try:
                await self.owner(f'Событие {uid} не удалось принять. /pending', f'admit_error:{uid}')
            except Exception:
                pass
            return False

    async def _admit(self, update):
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
                    if self.db.candidate(chat):
                        self._set_stage_safe(chat, 'AWAITING_OWNER', reason='message edited/deleted')
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

        if m['chat'].get('type') != 'private' or not self.connected(m.get('business_connection_id')):
            self.db.complete(uid)
            return False

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

    # ------------------------------------------------------------------ команды владельца

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
                'SELECT c.chat, c.mode, cd.stage, cd.prev_stage '
                'FROM conversations c LEFT JOIN candidates cd ON c.chat = cd.chat '
                'ORDER BY c.chat LIMIT 30'
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
            if command == '/auto':
                await self._complete_if_needed(chat, key)
            return

        if command == '/resume_candidate':
            async with self.gate:
                resumed_stage = self.db.resume_candidate(chat)
                if resumed_stage:
                    self.db.mode(chat, 'AUTO')
            if resumed_stage:
                await self.owner(f'Диалог с {chat} возобновлен с этапа {resumed_stage}.', key)
                await self._complete_if_needed(chat, key)
            else:
                await self.owner(f'Не удалось возобновить диалог {chat}.', key)
            return

        if command == '/reply' and len(bits) == 3:
            res = await self.deliver(chat, bits[2], key + ':approved', row['epoch'], owner_action=True)
            await self.owner('Утверждённый текст отправлен.' if res else 'Не отправлено. Проверьте STOP/права.', key)
            return

        await self.owner(HELP, key)

    # ------------------------------------------------------------------ сообщения кандидатов

    async def business(self, m, uid):
        chat = m['chat']['id']
        key = f'event:{uid}'
        row = self.db.conversation(chat)
        text = (m.get('text') or m.get('caption') or '').strip()
        epoch = row['epoch']

        cand = self.db.candidate(chat)
        stage = cand['stage'] if cand else 'NEW'

        # Остановка диалога по запросу кандидата
        if _plain(text) in CANDIDATE_STOP_WORDS:
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

        reason = self._block_reason(chat, epoch)
        if reason:
            self.db.log('AUTO_SKIPPED', chat, f'{key} {reason}')
            # Сообщение, пришедшее при STOP/без прав/вне окна, иначе потеряется молча
            if reason in ('STOP', 'CONNECTION', 'WINDOW') and stage not in ('DECLINED', 'AWAITING_OWNER'):
                await self.owner(
                    f'Сообщение от {chat} не обработано автоматически ({reason}):\n"{text[:300]}"\n'
                    f'Ответьте вручную или после снятия причины попросите кандидата написать снова.',
                    key + ':skipped')
            return

        if stage == 'NEW':
            if is_vacancy_inquiry(text, self.cfg.vacancy_phrases):
                await self._send_conditions(chat, key, epoch)
            return

        if stage == 'CONDITIONS_SENT':
            consent = is_explicit_consent(text, self.cfg.consent_phrases)
            decline = is_explicit_decline(text, self.cfg.decline_phrases)

            if consent and not decline:
                async with self.gate:
                    if self.db.conversation(chat)['epoch'] != epoch:
                        return
                    rec_res = self.db.record_consent(chat, m['message_id'], text, self.cfg.conditions_version)
                if rec_res == 'DUPLICATE':
                    return
                if rec_res != 'RECORDED':
                    # другая версия условий / сообщение раньше условий: решает владелец
                    await self._escalate(chat, text, 'согласие не принято (' + rec_res + ')', key, epoch)
                    return
                await self._finish_package(chat, key, epoch)
                return

            if decline and not consent:
                status, _ = await self._step(chat, f'dec:{chat}:{self.cfg.conditions_version}',
                                        self.cfg.decline_message_text, epoch)
                if status == 'SENT':
                    self._set_stage_safe(chat, 'DECLINED')
                else:
                    await self._step_problem(chat, key, 'отказ', status, epoch)
                return

            # Повторное «интересует вакансия» или «спасибо» до согласия: условия уже у кандидата,
            # ничего не дублируем и не останавливаем диалог. Длинное сообщение может содержать
            # вопрос без «?», поэтому молча пропускаем только короткие.
            if _is_repeat_before_consent(text, self.cfg.vacancy_phrases):
                self.db.log('REPEAT_IGNORED', chat, key)
                return

            # Неизвестный вопрос, сомнение или одновременно согласие и отказ -> владелец
            why ='неоднозначный ответ' if (consent and decline) else 'вопрос на этапе условий'
            await self._escalate(chat, text, why, key, epoch)
            return

        if stage in INCOMPLETE or stage == 'INVITE_SENT':
            if stage in INCOMPLETE:
                # Пакет не доведён до конца (STOP, смена epoch, сбой): сначала досылаем его
                await self._finish_package(chat, key, epoch)
                cand = self.db.candidate(chat)
                if not cand or cand['stage'] != 'INVITE_SENT':
                    return
            if '?' not in text and (is_explicit_consent(text, self.cfg.consent_phrases)
                                    or is_vacancy_inquiry(text, self.cfg.vacancy_phrases)
                                    or _is_pleasantry(text)):
                return
            await self._escalate(chat, text, f'сообщение после согласия (этап {stage})', key, epoch)