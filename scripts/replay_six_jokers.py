"""30 isolated real-WebSocket rounds from saved, de-identified card fixtures."""
import json
import sys
import time
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tests'))
from wire_support import RealServer, ok, shuffled_deck


def decode(values):
    return [({'suit': 'j', 'rank': 'small' if v == 14 else 'big'} if v in (14, 15)
             else {'suit': 'shcd'[(v >> 4)-1], 'rank': {14:'A',13:'K',12:'Q',11:'J'}.get(v&15,str(v&15))}) for v in values]


def run(out):
    out.mkdir(parents=True, exist_ok=False)
    fixtures=json.loads((ROOT/'tests/fixtures/six-joker-replay.json').read_text())
    two=json.loads((ROOT/'tests/fixtures/two-joker-replay.json').read_text())
    server=RealServer(out)
    rounds=[]
    try:
        tenant=server.tenant()
        clients=[server.client(tenant,str(i)) for i in range(7)]
        for version in range(1,31):
            mode=['standard','two_jokers','six_jokers'][(version-1)%3]
            if mode!='standard':
                source=two[0] if mode=='two_jokers' else fixtures[(version//3)%3]
                hands=[decode(h) for h in source]
                omitted=(version-1)%8
                expected=Counter((c['suit'],c['rank']) for c in hands[omitted])
                submitted=[h for i,h in enumerate(hands) if i!=omitted]
            else:
                deck=shuffled_deck(version)
                submitted=[deck[i*13:i*13+13] for i in range(7)]
                expected=Counter((c['suit'],c['rank']) for c in deck[91:])
            if version>1:
                clients[-1].close()
                clients[-1]=server.client(tenant,'switch-A' if version%2 else 'switch-B')
            for c in clients:c.wait('round.state',lambda m:m['round_version']==version)
            began=time.perf_counter()
            requests=[]
            for i,c in enumerate(clients):
                payload={'cards':submitted[i],'start_event_id':f'round-{version}-peer-{i}'}
                if mode!='standard':payload['mode']=mode
                msg=c.message('hand.submit',version,payload)
                ok(c.call_message(msg));requests.append(msg)
            # Acknowledged retries must not create another contribution or advance the round.
            ok(clients[0].call_message(requests[0]))
            results=[]
            for c in clients:
                reply=c.wait('round.result',lambda m:m['round_version']==version)
                data=reply['payload']
                assert data['mode']==mode
                got=Counter({(x['suit'],x['rank']):x['count'] for x in data['cards']})
                assert got==expected and sum(got.values())==13
                results.append(data)
            elapsed=(time.perf_counter()-began)*1000
            ok(clients[version%7].call('round.compare',version))
            for c in clients:c.wait('round.phase',lambda m:m['round_version']==version and m['payload']['phase']=='comparing')
            if version%3==0:
                cid=clients[0].cid;clients[0].close();clients[0]=server.client(tenant,cid)
                state=clients[0].wait('round.state',lambda m:m['round_version']==version)
                assert state['payload']['phase']=='comparing'
            ok(clients[-1].call('round.end',version))
            for c in clients:
                c.wait('round.phase',lambda m:m['round_version']==version and m['payload']['phase']=='ended')
                state=c.wait('round.state',lambda m:m['round_version']==version+1)
                assert state['payload']['mode'] is None
            row={'round':version,'mode':mode,'received':7,'remaining':13,'correct':True,'local_submit_to_all_results_ms':round(elapsed,2),'hands':submitted,'results':results}
            rounds.append(row)
            (out/'rounds.json').write_text(json.dumps(rounds,ensure_ascii=False,indent=2))
            print(f'round {version}: {mode} 7/7, result correct, phases correct ({elapsed:.1f}ms)',flush=True)
        server.integrity()
        (out/'summary.json').write_text(json.dumps({'rounds':30,'six_jokers':10,'two_jokers':10,'standard':10,'hands':210,'result_deliveries':210,'retries':30,'client_switches':29,'compare_reconnects':10,'passed':True},indent=2))
    finally:
        for i,c in enumerate(server.clients):
            (out/f'wire-{i}.json').write_text(json.dumps({'sent':c.sent,'received':c.messages},ensure_ascii=False))
        server.cleanup()


if __name__=='__main__':run(Path(sys.argv[1]))
