"""Installer-only read of IDs. Run with the assistant and other pollers stopped."""
import asyncio
import json
import os
from assistant_core.telegram import Telegram

async def inspect():
    tg=Telegram(os.environ.get('BOT_TOKEN',''))
    me=await tg.call('getMe')
    print(json.dumps({'bot_id':me['id'],'username':me.get('username'),'can_connect_to_business':me.get('can_connect_to_business')},ensure_ascii=False))
    hook=await tg.call('getWebhookInfo')
    if hook.get('url'):
        print('Existing webhook detected; not changed. Installer must reconcile ownership.')
        return
    updates=await tg.call('getUpdates',{'timeout':0,'limit':100,'allowed_updates':['message','business_connection','business_message','edited_business_message','deleted_business_messages']})
    for update in updates:
        c=update.get('business_connection')
        if c:
            print(json.dumps({'connection_id':c.get('id'),'business_owner_id':c.get('user',{}).get('id'),'enabled':c.get('is_enabled'),'can_reply':c.get('rights',{}).get('can_reply')},ensure_ascii=False))
        m=update.get('message') or update.get('business_message')
        if m:
            print(json.dumps({'type':'business' if 'business_message' in update else 'ordinary','from_id':m.get('from',{}).get('id'),'chat_id':m.get('chat',{}).get('id'),'chat_type':m.get('chat',{}).get('type'),'connection_id':m.get('business_connection_id')},ensure_ascii=False))
    if not updates:
        print('No pending updates. Send /start to this bot as owner, then connect Business and send a test message.')

if __name__=='__main__':
    try: asyncio.run(inspect())
    except Exception as exc:
        print('Inspection failed:',type(exc).__name__)
        raise SystemExit(1) from None
