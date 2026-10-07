"""Entrypoint. No Telegram/AI calls until explicitly started with real credentials."""
import argparse
import asyncio
import json
import logging
import os
import time
from .config import Config
from .storage import Store
from .telegram import Telegram, APIError
from .ai import AI
from .workflow import Engine
from .locking import ProcessLock

async def run(cfg):
    cfg.data_dir.mkdir(parents=True,exist_ok=True,mode=0o700)
    lock=ProcessLock(cfg.data_dir/'runtime.lock')
    db=Store(cfg.data_dir,cfg.client_id)
    tg=Telegram(os.environ.get('BOT_TOKEN',''))
    me=await tg.call('getMe')
    if db.get('bot_id') not in (None,str(me['id'])):
        raise ValueError('Different bot bound to this database; migrate explicitly')
    db.set('bot_id',me['id'])
    webhook=await tg.call('getWebhookInfo')
    if webhook.get('url'):
        raise ValueError('Webhook already configured; installer must reconcile explicitly')
    db.recover()
    engine=Engine(cfg,db,tg,AI(cfg.model,os.environ.get('AI_MODE','ollama'),os.environ.get('OLLAMA_URL','http://127.0.0.1:11434')))
    await engine.owner('Ядро запущено в STOP. Проверьте /pending и /status; /resume включает разрешённые автоответы.',f'start:{time.time_ns()}')
    running={}
    async def execute(update):
        await engine.process(update)
    try:
        while True:
            db.set('heartbeat',int(time.time()))
            for uid,job in list(running.items()):
                if job.done():
                    try:job.result()
                    except Exception:db.log('WORKER_ERROR')
                    del running[uid]
            try:
                updates=await tg.call('getUpdates',{'offset':int(db.get('offset','0')),'timeout':2,'limit':20,
                    'allowed_updates':['message','business_connection','business_message','edited_business_message','deleted_business_messages']})
                for update in updates:
                    db.ingest(update)
                # Admission is separate from durable receipt; restart resumes this stage.
                for row in db.received():
                    update=json.loads(row['payload'])
                    needs=await engine.admit(update)
                    if needs:
                        db.complete(update['update_id'],'PENDING')
                        # Commands bypass AI saturation; no automatic retries after errors.
                        if 'message' in update:
                            running[update['update_id']]=asyncio.create_task(execute(update))
                for row in db.pending():
                    if row['id'] in running:continue
                    update=json.loads(row['payload'])
                    if 'message' in update:continue
                    if len(running)>=10:break
                    running[row['id']]=asyncio.create_task(execute(update))
            except APIError:
                db.log('POLL_ERROR')
                await asyncio.sleep(2)
            await asyncio.sleep(.05)
    finally:
        for job in running.values():job.cancel()
        await asyncio.gather(*running.values(),return_exceptions=True)
        db.close();lock.close()

def main():
    os.umask(0o077)
    parser=argparse.ArgumentParser()
    parser.add_argument('--config',default=os.environ.get('CONFIG_PATH','config.json'))
    parser.add_argument('--check',action='store_true')
    args=parser.parse_args();cfg=Config.load(args.config)
    if args.check:
        print('Configuration valid; credentials/network not tested');return
    try:asyncio.run(run(cfg))
    except KeyboardInterrupt:pass
    except Exception as exc:
        # Exception strings from HTTP clients may expose token URLs; print type only.
        print('Startup/runtime failure:',type(exc).__name__,'See installation checklist.')
        raise SystemExit(1) from None

if __name__=='__main__':main()
