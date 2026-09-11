import copy
import pytest
from conftest import request
from app.domain import DomainError
from app.protocol import parse_message

def hand(cid, cards, version=1, event=None):
    return parse_message(request('hand.submit',cid,version=version,payload={'start_event_id':event or 'event-'+str(version)+'-'+cid,'cards':cards}))

def test_atomic_seven_hands_without_join(store,deck):
    for i in range(7):
        cid=f'client_{i}';store.register_client('100001',cid)
        reply=store.process('100001',cid,hand(cid,deck[i*13:(i+1)*13]))
        assert reply['payload']['bound_round_version']==1
    d=store.detail('100001');assert d['state']=='ready'
    assert sum(c['count'] for c in d['result'])==13

def test_invalid_hand_rolls_back_participation(store,deck):
    store.register_client('100001','x')
    msg=request('hand.submit','x',payload={'start_event_id':'bad','cards':deck[:12]})
    with pytest.raises(DomainError):store.process('100001','x',msg)
    d=store.detail('100001');assert d['state']=='waiting';assert d['devices'][0]['bound_round_version'] is None

def test_retry_after_closed_returns_original_ack(store,deck):
    store.register_client('100001','x');m=hand('x',deck[:13]);a=store.process('100001','x',m)
    store.close_round('100001',1)
    assert store.process('100001','x',m)==a
    changed=copy.deepcopy(m);changed['request_id']='new';changed['round_version']=2
    with pytest.raises(DomainError) as e:store.process('100001','x',changed)
    assert e.value.code=='START_EVENT_CONFLICT'
    assert store.detail('100001')['received_count']==0

def test_new_id_request_same_event_is_idempotent_but_changed_event_is_not(store,deck):
    store.register_client('100001','x');m=hand('x',deck[:13]);store.process('100001','x',m)
    retry=copy.deepcopy(m);retry['request_id']='retry';assert store.process('100001','x',retry)['payload']['duplicate']
    bad=hand('x',deck[:13],event='another')
    with pytest.raises(DomainError) as e:store.process('100001','x',bad)
    assert e.value.code=='START_EVENT_CONFLICT'

def test_existing_id_returns_after_many_rounds_without_previous_binding_exchange(store,deck):
    store.register_client('100001','07');store.process('100001','07',hand('07',deck[:13]))
    store.update_tenant('100001',{'round_version':8})
    store.process('100001','07',hand('07',deck[:13],8))
    assert store.detail('100001')['received_count']==1
