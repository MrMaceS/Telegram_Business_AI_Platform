import tempfile
import time
import unittest
from pathlib import Path
import sys
from pathlib import Path

# Тесты запускаются и без PYTHONPATH (например, из Git Bash): добавляем src сами.
_SRC = str(Path(__file__).resolve().parents[1] / 'src')
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from assistant_core.storage import Store

V1='2026-10-06-v1'

class CandidateStateTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.dir=Path(self.tmp.name)/'data'
        self.db=Store(self.dir,'a');self.db.set('paused','0')
        self.db.incoming(2,'c',1,'Интересует вакансия',time.time())
    def tearDown(self):self.db.close();self.tmp.cleanup()
    def reopen(self):
        self.db.close();self.db=Store(self.dir,'a');self.db.recover()
    def stage(self,chat=2):return self.db.candidate(chat)['stage']
    def consents(self):return self.db.db.execute('SELECT * FROM consents').fetchall()
    def conditions_sent(self):
        self.assertTrue(self.db.set_stage(2,'CONDITIONS_SENT',expected='NEW',conditions_version=V1,conditions_mid=100))

    def test_consents_table_columns(self):
        cols={r[1] for r in self.db.db.execute('PRAGMA table_info(consents)')}
        self.assertEqual(cols,{'id','chat','mid','text','conditions_version','at_utc'})

    def test_consent_recorded_after_conditions(self):
        self.conditions_sent()
        self.assertEqual(self.db.record_consent(2,101,'Да, условия мне подходят',V1),'RECORDED')
        self.assertEqual(self.stage(),'CONSENT_RECORDED')
        row=self.consents()[0]
        self.assertEqual((row['chat'],row['mid'],row['conditions_version']),(2,101,V1))
        self.assertTrue(row['at_utc'].endswith('+00:00'))

    def test_consent_before_conditions_rejected(self):
        self.assertEqual(self.db.record_consent(2,101,'Да',V1),'REJECTED')
        self.db.set_stage(2,'NEW')
        self.assertEqual(self.db.record_consent(2,101,'Да',V1),'REJECTED')
        self.assertFalse(self.consents())

    def test_consent_message_must_follow_conditions(self):
        self.conditions_sent()
        self.assertEqual(self.db.record_consent(2,99,'Да',V1),'REJECTED')
        self.assertEqual(self.stage(),'CONDITIONS_SENT')

    def test_repeated_consent_is_idempotent(self):
        self.conditions_sent();self.db.record_consent(2,101,'Да',V1)
        self.assertEqual(self.db.record_consent(2,101,'Да',V1),'DUPLICATE')
        self.assertEqual(self.db.record_consent(2,102,'Согласен',V1),'DUPLICATE')
        self.db.set_stage(2,'PRESENTATION_SENT')
        self.assertEqual(self.db.record_consent(2,103,'Согласен',V1),'DUPLICATE')
        self.assertEqual(len(self.consents()),1);self.assertEqual(self.stage(),'PRESENTATION_SENT')

    def test_consent_to_other_version_not_carried(self):
        self.conditions_sent()
        self.assertEqual(self.db.record_consent(2,101,'Да','2026-01-01-v0'),'REJECTED')
        self.assertFalse(self.consents())

    def test_stage_transitions_guarded(self):
        with self.assertRaises(ValueError):self.db.set_stage(2,'CONSENT_RECORDED')
        with self.assertRaises(ValueError):self.db.set_stage(2,'INVITE_SENT')
        with self.assertRaises(ValueError):self.db.set_stage(2,'CONDITIONS_SENT')
        with self.assertRaises(ValueError):self.db.set_stage(2,'BOGUS')
        self.conditions_sent()
        self.assertFalse(self.db.set_stage(2,'CONDITIONS_SENT',conditions_version=V1))
        self.assertFalse(self.db.set_stage(2,'DECLINED',expected='NEW'))
        self.assertEqual(self.stage(),'CONDITIONS_SENT')
        with self.assertRaises(ValueError):self.db.set_stage(2,'NEW')

    def test_awaiting_owner_keeps_previous_stage_and_resumes(self):
        self.conditions_sent()
        self.assertTrue(self.db.set_stage(2,'AWAITING_OWNER',reason='question'))
        self.assertFalse(self.db.set_stage(2,'AWAITING_OWNER'))
        self.assertEqual(self.db.candidate(2)['prev_stage'],'CONDITIONS_SENT')
        with self.assertRaises(ValueError):self.db.set_stage(2,'DECLINED')
        self.db.set('paused','1');self.db.mode(2,'MANUAL');epoch=self.db.conversation(2)['epoch']
        self.assertEqual(self.db.resume_candidate(2),'CONDITIONS_SENT')
        self.assertIsNone(self.db.candidate(2)['prev_stage'])
        self.assertEqual(self.db.get('paused'),'1')
        self.assertEqual(self.db.conversation(2)['mode'],'MANUAL')
        self.assertEqual(self.db.conversation(2)['epoch'],epoch+1)
        self.assertIsNone(self.db.resume_candidate(2))
        self.assertEqual(self.db.record_consent(2,101,'Да',V1),'RECORDED')

    def test_resume_to_declined_or_rejects_other_targets(self):
        self.conditions_sent();self.db.set_stage(2,'AWAITING_OWNER')
        with self.assertRaises(ValueError):self.db.resume_candidate(2,'INVITE_SENT')
        self.assertEqual(self.db.resume_candidate(2,'DECLINED'),'DECLINED')
        self.assertIsNone(self.db.resume_candidate(3))

    def test_restart_preserves_stages_and_consent(self):
        self.conditions_sent();self.db.record_consent(2,101,'Да',V1)
        self.db.set_stage(2,'PRESENTATION_SENT');self.db.set_stage(2,'AWAITING_OWNER')
        self.reopen()
        self.assertEqual(self.db.get('paused'),'1')
        row=self.db.candidate(2)
        self.assertEqual((row['stage'],row['prev_stage'],row['conditions_version']),('AWAITING_OWNER','PRESENTATION_SENT',V1))
        self.assertEqual(len(self.consents()),1)
        self.assertEqual(self.db.resume_candidate(2),'PRESENTATION_SENT')
        self.assertEqual(self.db.get('paused'),'1')

    def test_restart_after_completed_send_does_not_resend(self):
        self.conditions_sent();self.db.record_consent(2,101,'Да',V1)
        self.assertTrue(self.db.reserve('presentation:2',2,'sendDocument'))
        self.db.outgoing_result('presentation:2','SENT',{'message_id':5})
        self.db.set_stage(2,'PRESENTATION_SENT')
        self.reopen()
        self.assertEqual(self.stage(),'PRESENTATION_SENT')
        self.assertFalse(self.db.reserve('presentation:2',2,'sendDocument'))

    def test_restart_with_unclear_send_goes_to_owner_without_retry(self):
        self.conditions_sent();self.db.record_consent(2,101,'Да',V1)
        self.assertTrue(self.db.reserve('presentation:2',2,'sendDocument'))  # crash while SENDING
        self.reopen()
        self.assertEqual(self.db.get('paused'),'1')
        self.assertEqual(self.db.db.execute("SELECT status FROM outgoing WHERE dedupe='presentation:2'").fetchone()[0],'UNKNOWN')
        row=self.db.candidate(2)
        self.assertEqual((row['stage'],row['prev_stage']),('AWAITING_OWNER','CONSENT_RECORDED'))
        self.assertEqual(self.db.resume_candidate(2),'CONSENT_RECORDED')
        self.assertFalse(self.db.reserve('presentation:2',2,'sendDocument'))

    def test_recover_is_strict_and_idempotent(self):
        self.db.ingest({'update_id':50,'message':{'from':{'id':1},'chat':{'id':1},'text':'/resume'}})
        self.db.ingest({'update_id':51,'business_message':{'message_id':2,'chat':{'id':2},'text':'x'}})
        self.db.complete(51,'RUNNING')
        self.reopen();self.db.recover()
        self.assertEqual(self.db.get('paused'),'1')
        status=dict(self.db.db.execute('SELECT id,status FROM inbox').fetchall())
        self.assertEqual(status,{50:'REVIEW',51:'REVIEW'})
        self.assertFalse(self.db.received());self.assertFalse(self.db.pending())

    def test_paused_survives_reopen_even_if_running(self):
        self.assertEqual(self.db.get('paused'),'0')
        self.reopen();self.assertEqual(self.db.get('paused'),'1')

if __name__=='__main__':
    unittest.main()