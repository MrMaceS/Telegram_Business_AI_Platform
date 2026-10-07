"""Small deterministic workflow. AI can select approved FAQ; other drafts require owner."""
import asyncio
import hashlib
import json
import re
import time
import uuid
from pathlib import Path
from .telegram import APIError, DeliveryUnknown

CRITICAL = re.compile(r'оплат|цен[ауые]|стоим|деньг|кошел|перен[ое]с|срок|дедлайн|принят|принима|парол|токен|доступ|payment|price|deadline|password|оплат|вартіст|термін', re.I)
HELP = ('Команды: /status, /stop, /resume, /manual CHAT, /auto CHAT, '
        '/assign CHAT TASK, /issue CHAT, /reply CHAT TEXT, /accept CHAT, '
        '/files CHAT, /file ID, /pending. После запуска всегда STOP. '
        'Для AUTO: /resume, затем при необходимости /auto CHAT. '
        'Разрешённые контакты и задания задаются в config.json.')

class Engine:
    def __init__(self, cfg, store, telegram, ai):
        self.cfg, self.db, self.tg, self.ai = cfg, store, telegram, ai
        # One send/STOP gate per company. LLM never holds this lock.
        self.gate = asyncio.Lock()
        self.chats = {}
        self.ai_slots = asyncio.Semaphore(1)

    def connection(self):
        return json.loads(self.db.get('connection', '{}'))

    def connected(self, connection):
        c = self.connection()
        return c.get('id') == connection and c.get('is_enabled') is True and c.get('user',{}).get('id') == self.cfg.business_owner_id

    def can_send(self, chat, epoch, owner_action=False):
        c = self.db.conversation(chat)
        return bool(chat in self.cfg.contacts and c and c['epoch'] == epoch
            and self.db.get('paused') == '0' and (owner_action or c['mode']=='AUTO')
            and self.connected(c['connection']) and self.connection().get('rights',{}).get('can_reply')
            and 0 <= time.time()-c['last_in'] < 24*3600)

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
            payload = {'chat_id':chat}
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
                self.db.execute('INSERT OR IGNORE INTO messages(chat,mid,direction,body) VALUES(?,?,?,?)',
                    (chat, result['message_id'], 'assistant', text[:8000]))
            return True

    async def admit(self, update):
        """Persist/control events immediately, before slow business handlers. Returns needs_worker."""
        uid = update['update_id']
        if 'business_connection' in update:
            c = update['business_connection']
            if c.get('user',{}).get('id') != self.cfg.business_owner_id:
                self.db.log('CONNECTION_REJECTED'); self.db.complete(uid); return False
            async with self.gate:
                self.db.set('connection', json.dumps(c))
                self.db.execute('UPDATE conversations SET epoch=epoch+1')
            self.db.complete(uid); return False
        if 'message' in update:
            m = update['message']
            if m.get('from',{}).get('id') != self.cfg.owner_id or m.get('chat',{}).get('id') != self.cfg.owner_id or m['chat'].get('type')!='private':
                self.db.complete(uid); return False
            return True  # command work is scheduled independently from the polling loop
        if 'edited_business_message' in update or 'deleted_business_messages' in update:
            m = update.get('edited_business_message') or update['deleted_business_messages']
            chat = m['chat']['id']
            if chat in self.cfg.contacts and self.connected(m.get('business_connection_id')):
                async with self.gate:
                    if 'message_id' in m:
                        self.db.execute('UPDATE messages SET body=? WHERE chat=? AND mid=?',
                            ((m.get('text') or '[изменённое сообщение без текста]')[:8000],chat,m['message_id']))
                    else:
                        for mid in m.get('message_ids',[]):
                            self.db.execute('UPDATE messages SET deleted=1 WHERE chat=? AND mid=?',(chat,mid))
                    self.db.mode(chat, 'AWAITING_OWNER')
                await self.owner(f'Изменение/удаление сообщения в {chat}. Контекст требует сверки; автоответы приостановлены.',f'edit:{uid}')
            self.db.complete(uid); return False
        m = update.get('business_message')
        if not m:
            self.db.complete(uid); return False
        chat = m['chat']['id']
        if chat not in self.cfg.contacts or m['chat'].get('type')!='private' or not self.connected(m.get('business_connection_id')):
            self.db.complete(uid); return False
        sender = m.get('from',{}).get('id')
        if sender == self.cfg.business_owner_id and not m.get('sender_business_bot'):
            async with self.gate:
                self.db.mode(chat,'MANUAL')
                self.db.execute('INSERT OR IGNORE INTO messages(chat,mid,direction,body) VALUES(?,?,?,?)', (chat,m['message_id'],'assistant',(m.get('text') or m.get('caption') or '[ручное вложение владельца]')[:8000]))
            self.db.complete(uid); return False
        if sender != chat or m.get('sender_business_bot'):
            self.db.complete(uid); return False
        body = m.get('text') or m.get('caption') or '[вложение/неподдерживаемое сообщение]'
        # Incoming persistence advances epoch even while an earlier LLM call is running.
        self.db.incoming(chat,m['business_connection_id'],m['message_id'],body,m['date'])
        return True

    async def process(self, update):
        uid = update['update_id']
        self.db.complete(uid,'RUNNING')
        try:
            if 'message' in update:
                await self.command(update['message'].get('text',''), uid)
            else:
                m=update['business_message']; chat=m['chat']['id']
                lock=self.chats.setdefault(chat,asyncio.Lock())
                async with lock:
                    await self.business(m,uid)
            self.db.complete(uid)
        except Exception as exc:
            self.db.log('PROCESS_ERROR',detail=type(exc).__name__)
            self.db.complete(uid,'REVIEW')
            await self.owner(f'Событие {uid} требует проверки. /pending',f'error:{uid}')

    async def command(self, text, uid):
        bits=text.split(maxsplit=2); command=bits[0].lower() if bits else ''
        key=f'command:{uid}'
        if command in ('/start','/help'):
            await self.owner(HELP,key); return
        if command in ('/stop','/resume'):
            async with self.gate:
                self.db.set('paused','1' if command=='/stop' else '0')
                self.db.execute('UPDATE conversations SET epoch=epoch+1')
                self.db.log('GLOBAL_MODE',detail=command)
            await self.owner('STOP сохранён.' if command=='/stop' else 'Общая пауза снята. MANUAL/ожидание в отдельных диалогах сохранены.',key); return
        if command=='/status':
            rows=self.db.db.execute('SELECT chat,mode,task_id,task_status FROM conversations ORDER BY chat LIMIT 30').fetchall()
            await self.owner('STOP='+self.db.get('paused')+'\n'+'\n'.join(str(dict(x)) for x in rows),key); return
        if command=='/pending':
            rows=self.db.db.execute("SELECT id,chat,status FROM outgoing WHERE status IN ('UNKNOWN','FAILED') ORDER BY id DESC LIMIT 15").fetchall()
            events=self.db.db.execute("SELECT id FROM inbox WHERE status='REVIEW' LIMIT 15").fetchall()
            await self.owner('Отправки: '+str([dict(x) for x in rows])+'\nСобытия на сверку: '+str([x[0] for x in events]),key); return
        if command=='/file' and len(bits)>=2:
            row=self.db.db.execute('SELECT * FROM artifacts WHERE id=?',(int(bits[1]),)).fetchone()
            if row:
                await self.deliver(self.cfg.owner_id,f'Файл {row["id"]}; chat {row["chat"]}',key,
                                   document=Path(row['path']),owner_notice=True)
            else:
                await self.owner('Файл не найден.',key)
            return
        if len(bits)<2 or not bits[1].isdigit() or int(bits[1]) not in self.cfg.contacts:
            await self.owner(HELP,key); return
        chat=int(bits[1]); row=self.db.conversation(chat)
        if not row:
            await self.owner('Сначала нужно входящее сообщение разрешённого контакта.',key); return
        if command in ('/manual','/auto'):
            async with self.gate:
                self.db.mode(chat,'MANUAL' if command=='/manual' else 'AUTO')
            await self.owner(f'Режим {chat} обновлён. Старые ответы не отправляются.',key); return
        if command=='/assign' and len(bits)==3 and bits[2] in self.cfg.tasks:
            async with self.gate:
                if row['task_status'] not in (None,'ACCEPTED'):
                    awaitable_message='У контакта уже есть незакрытое задание; новая выдача запрещена.'
                else:
                    self.db.execute("UPDATE conversations SET task_id=?,task_status='ASSIGNED',epoch=epoch+1 WHERE chat=?",(bits[2],chat))
                    self.db.log('ASSIGNED',chat,bits[2]); awaitable_message='Назначено. Для отправки: /issue '+str(chat)
            await self.owner(awaitable_message,key); return
        if command=='/issue' and row['task_id']:
            if row['task_status']!='ASSIGNED':
                await self.owner('Пакет уже выдавался либо имеет другой статус. Повторная автоматическая выдача запрещена.',key); return
            task=self.cfg.tasks[row['task_id']];epoch=row['epoch']
            async with self.gate:
                changed=self.db.execute("UPDATE conversations SET task_status='ISSUING' WHERE chat=? AND task_status='ASSIGNED' AND epoch=?",(chat,epoch)).rowcount
            if not changed:
                await self.owner('Выдача уже началась или контекст изменился.',key);return
            ok=await self.deliver(chat,f'Задание {row["task_id"]}\n{task["brief"]}\nПодтвердите получение и начало работы.',key+':brief',epoch)
            for i,name in enumerate(task.get('files',[])):
                if not ok: break
                ok=await self.deliver(chat,f'Материал {i+1}',key+f':file:{i}',epoch,document=self.cfg.material(name))
            if ok:
                ok=bool(self.db.execute("UPDATE conversations SET task_status='ISSUED' WHERE chat=? AND epoch=? AND task_id=?",(chat,epoch,row['task_id'])).rowcount)
            if not ok:
                self.db.execute("UPDATE conversations SET task_status='ISSUE_REVIEW' WHERE chat=? AND task_status='ISSUING'",(chat,))
            await self.owner('Пакет отправлен.' if ok else 'Выдача не завершена: проверьте STOP, режим, окно Telegram и /pending. Повторную выдачу после частичной отправки сверить вручную.',key); return
        if command=='/reply' and len(bits)==3:
            ok=await self.deliver(chat,bits[2],key+':approved',row['epoch'],owner_action=True)
            await self.owner('Утверждённый текст отправлен; режим диалога сохранён.' if ok else 'Не отправлено. Проверьте STOP/права/окно и /pending.',key); return
        if command=='/accept' and row['task_id']:
            async with self.gate:
                self.db.execute("UPDATE conversations SET task_status='ACCEPTED',epoch=epoch+1 WHERE chat=?",(chat,))
                self.db.log('ACCEPTED_BY_OWNER',chat,row['task_id'])
            await self.owner('Приёмка владельцем записана.',key); return
        if command=='/files':
            rows=self.db.db.execute('SELECT id,task_id,size,sha256 FROM artifacts WHERE chat=? ORDER BY id DESC LIMIT 10',(chat,)).fetchall()
            await self.owner(str([dict(x) for x in rows]),key); return
        await self.owner(HELP,key)

    async def business(self,m,uid):
        chat=m['chat']['id'];key=f'event:{uid}';row=self.db.conversation(chat)
        doc=m.get('document'); text=m.get('text','')
        if doc:
            await self.receive_file(m,uid)
            return
        if text.strip().lower() in ('/stop','стоп','не отвечай','не відповідай'):
            async with self.gate: self.db.mode(chat,'MANUAL')
            await self.owner(f'Контакт {chat} попросил остановить автообщение.',key+':stop');return
        latest=self.db.db.execute("SELECT MAX(mid) FROM messages WHERE chat=? AND direction='user' AND deleted=0",(chat,)).fetchone()[0]
        if m['message_id'] != latest:
            self.db.log('SUPERSEDED_MESSAGE',chat,key)
            return  # A burst produces one answer using the newest conversation context.
        if not self.can_send(chat,row['epoch']):
            self.db.log('AUTO_SKIPPED',chat,key);return
        epoch=row['epoch']
        if not text:
            await self.deliver(chat,'Пришлите вопрос текстом или документом. Этот формат пока не обрабатывается.',key,epoch);return
        if row['task_id'] and text.strip().lower() in ('приступил','начал работу','почав роботу','приступаю') and row['task_status']=='ISSUED':
            self.db.execute("UPDATE conversations SET task_status='IN_PROGRESS' WHERE chat=?",(chat,))
            await self.deliver(chat,'Начало работы зафиксировано.',key,epoch);return
        if not row['disclosed']:
            intro=('По рабочим вопросам вам помогает AI-ассистент.' if getattr(self.ai,'mode','ollama')=='ollama' else 'По рабочим вопросам вам помогает автоматический помощник с готовыми ответами.')
            ok=await self.deliver(chat,intro+' Решения по условиям и приёмке принимает владелец.',key+':intro',epoch)
            if not ok:return
            self.db.execute('UPDATE conversations SET disclosed=1 WHERE chat=?',(chat,))
        task=self.cfg.tasks.get(row['task_id'],{})
        if CRITICAL.search(text) or not self.db.quota(self.cfg.daily_ai_calls):
            proposal={'faq_id':None,'draft':'Требуется решение владельца либо достигнут лимит AI.','escalate':True}
        else:
            async with self.ai_slots:
                proposal=await self.ai.propose(self.cfg,self.db.history(chat),task)
        # A new message, mode change, assignment or STOP invalidates the old generation.
        if not self.can_send(chat,epoch):return
        faq=next((x for x in self.cfg.faqs if x['id']==proposal.get('faq_id')),None)
        if faq and proposal.get('escalate') is False:
            await self.deliver(chat,faq['answer'],key+':faq',epoch);return
        # Send neutral acknowledgement before transition; it is never substantive advice.
        await self.deliver(chat,'Этот вопрос передан владельцу для решения.',key+':ack',epoch)
        async with self.gate:
            if self.db.conversation(chat)['epoch']!=epoch:return
            self.db.mode(chat,'AWAITING_OWNER')
        await self.owner(f'Контакт {chat}, задание {row["task_id"]}\nВопрос: {text[:1200]}\nЧерновик (не отправлен): {proposal.get("draft","")[:1800]}\nОтвет: /reply {chat} ТЕКСТ. Возврат AI: /auto {chat}',key+':escalate')

    async def receive_file(self,m,uid):
        chat=m['chat']['id'];doc=m['document'];row=self.db.conversation(chat);key=f'file:{uid}'
        if doc.get('file_size',0)>self.cfg.max_file_bytes:
            await self.owner(f'Файл {chat} превышает лимит; не сохранён.',key+':large');return
        if self.db.db.execute('SELECT 1 FROM artifacts WHERE chat=? AND mid=?',(chat,m['message_id'])).fetchone():return
        directory=self.cfg.data_dir/'files';directory.mkdir(exist_ok=True,mode=0o700)
        path=directory/(uuid.uuid4().hex+'.bin')
        try:
            await self.tg.download(doc['file_id'],path,self.cfg.max_file_bytes)
        except APIError:
            await self.owner(f'Файл {chat} не сохранён: ошибка скачивания.',key+':error');return
        digest=hashlib.sha256(path.read_bytes()).hexdigest()
        cur=self.db.execute('INSERT INTO artifacts(chat,mid,task_id,path,sha256,size,original_name) VALUES(?,?,?,?,?,?,?)',
            (chat,m['message_id'],row['task_id'],str(path.resolve()),digest,path.stat().st_size,doc.get('file_name','document')[:200]))
        if row['task_id'] and row['task_status']!='ACCEPTED':
            self.db.execute("UPDATE conversations SET task_status='RESULT_RECEIVED' WHERE chat=? AND task_id=? AND task_status!='ACCEPTED'",(chat,row['task_id']))
        self.db.log('FILE_STORED',chat,str(cur.lastrowid))
        await self.owner(f'Файл сохранён: ID {cur.lastrowid}; контакт {chat}; задание {row["task_id"]}; SHA256 {digest}. Получить: /file {cur.lastrowid}. Содержимое/комплектность не проверены.',key+':owner')
        await self.deliver(chat,'Файл сохранён и передан владельцу. Это подтверждение получения, не приёмка качества или полного комплекта.',key+':ack',self.db.conversation(chat)['epoch'])
