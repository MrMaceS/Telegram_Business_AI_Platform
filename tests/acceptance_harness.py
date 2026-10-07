"""Общая обвязка приёмочных тестов ТЗ 5.0 (роль 4).

ВАЖНО: всё, что зависит от того, как коллеги реализуют Config/Engine, собрано
в ОДНОЙ функции build() ниже. Если интерфейс поменяется - правим только её.
Тесты работают "чёрным ящиком": подаём Telegram-updates, смотрим, что ушло в Telegram
(FakeTelegram) и что лежит в SQLite.
"""
import asyncio, json, shutil, sqlite3, tempfile, time, unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OWNER, CANDIDATE, OUTSIDER, CONN = 100000001, 100000002, 999999, 'conn-1'
CONDITIONS = (ROOT / 'examples/materials/vacancy_conditions.txt').read_text(encoding='utf-8')
SCENARIO = json.loads((ROOT / 'examples/recruitment.scenario.json').read_text(encoding='utf-8'))
INVITE_URL = SCENARIO['group_invite_url']
CONSENT = 'Да, условия мне подходят. Я согласен перейти к следующему этапу'

from assistant_core.config import Config
from assistant_core.storage import Store
from assistant_core.workflow import Engine
from assistant_core.telegram import APIError, DeliveryUnknown


class FakeTelegram:
    def __init__(self):
        self.sent, self.calls, self.fail_on, self.unknown = [], 0, None, False
    async def call(self, method, payload=None, document=None):
        self.calls += 1
        if self.calls == self.fail_on:
            raise DeliveryUnknown() if self.unknown else APIError()
        self.sent.append((method, payload, document))
        return {'message_id': 1000 + self.calls}
    async def download(self, file_id, destination, limit):
        destination.write_bytes(b'x')


class FakeAI:  # LLM не нужна; если ядро её вызовет - вернём эскалацию
    mode = 'faq'
    async def propose(self, *a):
        return {'faq_id': None, 'draft': '', 'escalate': True}


def build(root: Path, tg: FakeTelegram):
    """ЕДИНСТВЕННОЕ место, зависящее от интерфейса коллег."""
    mats = root / 'materials'
    if not mats.exists():
        shutil.copytree(ROOT / 'examples/materials', mats)
    cfg_json = json.loads((ROOT / 'examples/config.example.json').read_text(encoding='utf-8'))
    cfg_json.update(owner_id=OWNER, business_owner_id=OWNER, contacts=[CANDIDATE],
                    data_dir=str(root / 'data'), materials_dir=str(mats),
                    scenario_file=str(ROOT / 'examples/recruitment.scenario.json'))
    p = root / 'config.json'
    p.write_text(json.dumps(cfg_json, ensure_ascii=False), encoding='utf-8')
    cfg = Config.load(str(p))
    db = Store(cfg.data_dir, cfg.client_id)
    return cfg, db, Engine(cfg, db, tg, FakeAI())


class Acceptance(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.root = Path(self.tmp.name)
        self.tg = FakeTelegram(); self._uid = 0
        self.start_engine(fresh=True)
    def tearDown(self):
        self.db.close(); self.tmp.cleanup()

    def start_engine(self, fresh=False):
        cfg, self.db, self.e = build(self.root, self.tg); self.cfg = cfg
        if fresh:
            self.db.set('paused', '0')
            self.db.set('connection', json.dumps({'id': CONN, 'user': {'id': OWNER},
                         'is_enabled': True, 'rights': {'can_reply': True}}))
        else:
            self.db.recover()          # как при реальном запуске: всегда STOP

    async def restart(self):
        self.db.close(); self.start_engine(fresh=False)

    def uid(self):
        self._uid += 1; return self._uid

    async def run_update(self, u):
        if not self.db.ingest(u):
            return                      # дубликат update_id отброшен
        if await self.e.admit(u):
            self.db.complete(u['update_id'], 'PENDING'); await self.e.process(u)

    async def candidate(self, text, chat=CANDIDATE, uid=None, sender=None, mid=None):
        uid = uid or self.uid()
        m = {'message_id': mid or uid, 'date': int(time.time()), 'chat': {'id': chat, 'type': 'private'},
             'from': {'id': sender or chat}, 'business_connection_id': CONN, 'text': text}
        await self.run_update({'update_id': uid, 'business_message': m}); return uid

    async def owner_cmd(self, text, sender=OWNER):
        uid = self.uid()
        await self.run_update({'update_id': uid, 'message': {'message_id': uid, 'from': {'id': sender},
                               'chat': {'id': sender, 'type': 'private'}, 'text': text}})

    # --- наблюдение ---
    def to_chat(self, chat=CANDIDATE):
        return [x for x in self.tg.sent if x[1].get('chat_id') == chat]
    def texts(self, chat=CANDIDATE):
        return [x[1].get('text') or x[1].get('caption') or '' for x in self.to_chat(chat)]
    def pdfs(self):
        return [x for x in self.to_chat() if x[0] == 'sendDocument' and x[2] and x[2].name == 'company_presentation.pdf']
    def invites(self):
        return [t for t in self.texts() if INVITE_URL in t]
    def conditions_sent(self):
        return [t for t in self.texts() if t.strip() == CONDITIONS.strip()]
    def dump(self):
        """Весь текст всех таблиц SQLite (для поиска записи согласия и этапа)."""
        out = []
        for (t,) in self.db.db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall():
            for r in self.db.db.execute(f'SELECT * FROM {t}').fetchall():
                out.append(f'{t}: ' + ' | '.join(str(x) for x in tuple(r)))
        return '\n'.join(out)
    async def happy_path(self):
        await self.owner_cmd('/resume')
        await self.candidate('Здравствуйте, интересует вакансия')
        await self.candidate(CONSENT)
