"""Local-only companion: ONE real own hand + SIX explicitly synthetic peers.
Never connects to the game, never invents the real client's hand, never runs on production.
Start after the protocol APK is configured to http://127.0.0.1:18080 via adb reverse.
"""
import argparse
import asyncio
import json
import random
import time
import uuid
from pathlib import Path
import httpx
from websockets.asyncio.client import connect

BASE = 'http://127.0.0.1:18080'
TENANT = '900991'
REAL = '01'

async def main(output, compare_delay):
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open('a', encoding='utf-8') as journal:
        def record(kind, **data):
            journal.write(json.dumps({'time_ms':time.time_ns()//1_000_000,'kind':kind,**data},ensure_ascii=False)+'\n');journal.flush()
        async with httpx.AsyncClient(base_url=BASE, timeout=5) as http:
            tenants=(await http.get('/api/tenants')).json()
            if not any(t['tenant_id']==TENANT for t in tenants):
                response=await http.post('/api/tenants',json={'tenant_id':TENANT,'note':'协议单账号测试：六份合成手牌，非真实对手结果','timeout_seconds':300});response.raise_for_status()
            sockets=[];readers=[];heartbeats=[]
            async def read(socket,cid):
                async for text in socket:
                    record('receive',client_id=cid,message=json.loads(text))
            async def heartbeat(socket,cid):
                while True:
                    await asyncio.sleep(10)
                    message={'protocol_version':1,'type':'ping','tenant_id':TENANT,'client_id':cid,'request_id':uuid.uuid4().hex,'round_version':None,'payload':{}}
                    await socket.send(json.dumps(message))
            try:
                for i in range(2,8):
                    cid=f'sim{i:02d}'
                    socket=await connect(BASE.replace('http:','ws:')+f'/ws/client?tenant_id={TENANT}&client_id={cid}',proxy=None)
                    sockets.append((cid,socket));readers.append(asyncio.create_task(read(socket,cid)));heartbeats.append(asyncio.create_task(heartbeat(socket,cid)))
                sent=set();pending_compare={}
                record('ready',tenant_id=TENANT,real_client=REAL,synthetic_peers=6)
                print('READY tenant=900991 real=01 synthetic=sim02..sim07; waiting for REAL upload',flush=True)
                while True:
                    if any(reader.done() for reader in readers+heartbeats):raise RuntimeError('Synthetic peer disconnected; stop rather than continue partially')
                    response=await http.get(f'/api/tenants/{TENANT}/rounds/current');response.raise_for_status();detail=response.json()
                    version=detail['round_version'];phase=detail['phase']
                    own=next((d for d in detail['devices'] if d['client_id']==REAL and d['cards']),None)
                    if own and version not in sent and phase=='playing':
                        if detail['received_count']!=1 or not detail['game_key']:raise RuntimeError('Tenant is not a fresh protocol-only test round')
                        cards=[{'suit':s,'rank':r} for s in 'shcd' for r in ['A',*map(str,range(2,11)),'J','Q','K'] for _ in range(2)]
                        for card in own['cards']:cards.remove({'suit':card['suit'],'rank':card['rank']})
                        random.Random(version).shuffle(cards)
                        record('real_hand_observed',version=version,cards=own['cards'],game_key=detail['game_key'])
                        for index,(cid,socket) in enumerate(sockets):
                            message={'protocol_version':1,'type':'hand.submit','tenant_id':TENANT,'client_id':cid,'request_id':uuid.uuid4().hex,'round_version':version,'payload':{'start_event_id':uuid.uuid4().hex,'game_key':detail['game_key'],'cards':cards[index*13:(index+1)*13]}}
                            await socket.send(json.dumps(message));record('synthetic_send',message=message)
                        sent.add(version)
                        if compare_delay is not None:
                            pending_compare[version]=time.monotonic()+compare_delay
                        print(f'round {version}: REAL hand received; 6 synthetic hands sent',flush=True)
                    if version in pending_compare and time.monotonic()>=pending_compare[version] and detail['received_count']==7 and phase=='playing':
                        cid,socket=sockets[0]
                        message={'protocol_version':1,'type':'round.compare','tenant_id':TENANT,'client_id':cid,'request_id':uuid.uuid4().hex,'round_version':version,'payload':{}}
                        await socket.send(json.dumps(message));record('synthetic_compare',message=message);pending_compare.pop(version)
                        print(f'round {version}: synthetic comparison notification sent; panel should hide',flush=True)
                    await asyncio.sleep(.05)
            finally:
                for _,socket in sockets:await socket.close()
                for reader in readers+heartbeats:reader.cancel()
                await asyncio.gather(*readers,*heartbeats,return_exceptions=True)
                record('stopped')

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--compare-delay',type=float,default=None,help='Opt-in synthetic comparison test only; default waits for real game comparison')
    args=parser.parse_args()
    if args.compare_delay is not None and args.compare_delay<1:parser.error('compare delay must be >= 1 second')
    try:asyncio.run(main(args.output,args.compare_delay))
    except KeyboardInterrupt:pass
