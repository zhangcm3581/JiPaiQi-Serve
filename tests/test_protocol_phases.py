import uuid
import pytest
from app.domain import DomainError
from conftest import request
from wire_support import RealServer, shuffled_deck, ok


def submit(store, cid, cards, key='room:1'):
    store.register_client('100001',cid)
    return store.process('100001',cid,request('hand.submit',cid,payload={'cards':cards,'start_event_id':uuid.uuid4().hex,'game_key':key}))


def test_compare_persists_and_does_not_advance(store,deck):
    submit(store,'a',deck[:13])
    message=request('round.compare','a')
    store.process('100001','a',message)
    assert store.process('100001','a',message)['payload']['phase']=='comparing'
    assert store.detail('100001')['round_version']==1
    assert store.detail('100001')['phase']=='comparing'
    store.process('100001','a',request('round.end','a'))
    assert store.detail('100001')['round_version']==2
    assert store.detail('100001')['phase']=='playing'


def test_wrong_game_key_rollback(store,deck):
    submit(store,'a',deck[:13])
    with pytest.raises(DomainError,match='不同房间'):
        submit(store,'b',deck[13:26],'another:1')
    assert store.detail('100001')['received_count']==1
    assert next(c for c in store.detail('100001')['devices'] if c['client_id']=='b')['bound_round_version'] is None


def test_nonparticipant_cannot_hide_or_close(store):
    store.register_client('100001','idle')
    for kind in ['round.compare','round.end']:
        with pytest.raises(DomainError) as error:store.process('100001','idle',request(kind,'idle'))
        assert error.value.code=='NOT_JOINED'


@pytest.mark.parametrize('ready',[False,True])
def test_real_websocket_phase_broadcast_and_reconnect(tmp_path,ready):
    server=RealServer(tmp_path)
    try:
        tenant=server.tenant(); other=server.tenant('900002');deck=shuffled_deck()
        clients=[server.client(tenant,str(i)) for i in range(7)]
        outsider=server.client(other,'outsider')
        for i in range(7 if ready else 1):
            ok(clients[i].call('hand.submit',1,{'cards':deck[i*13:(i+1)*13],'start_event_id':f'start-{i}','game_key':'room:1'}))
        marks=[len(c.messages) for c in clients]
        ok(clients[0].call('round.compare'))
        for c,mark in zip(clients,marks):
            c.wait('round.phase',lambda x:x['payload']['phase']=='comparing',after=mark)
            c.wait('round.state',lambda x:x['payload']['phase']=='comparing',after=mark)
        assert not any(m['type']=='round.phase' for m in outsider.messages)
        clients[0].close();rejoined=server.client(tenant,'0')
        assert rejoined.wait('round.state')['payload']['phase']=='comparing'
        assert not any(m['type']=='round.result' for m in rejoined.messages)
        # Even finishing collection after compare must not republish a visible result.
        if not ready:
            for i in range(1,7):ok(clients[i].call('hand.submit',1,{'cards':deck[i*13:(i+1)*13],'start_event_id':f'start-{i}','game_key':'room:1'}))
            assert not any(m['type']=='round.result' for c in clients[1:] for m in c.messages)
        ok(rejoined.call('round.end'))
        for c in clients[1:]:
            c.wait('round.phase',lambda x:x['payload']['phase']=='ended')
            c.wait('round.state',lambda x:x['round_version']==2)
        server.integrity()
    finally:server.cleanup()
