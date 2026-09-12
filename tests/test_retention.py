import copy
import sqlite3
import pytest
from app.domain import Store, DomainError
from conftest import request


def submit(store, deck, version, tenant='100001', cid='x'):
    store.register_client(tenant, cid)
    message = request('hand.submit', cid, tenant, version,
                      {'start_event_id': f'game-{version}-{cid}', 'cards': deck[:13]})
    return message, store.process(tenant, cid, message)


def test_five_closed_rounds_plus_active_and_all_related_rows(store, deck):
    store.create_tenant('100002')
    old, _ = submit(store, deck, 1)
    for version in range(1, 11):
        if version > 1:
            submit(store, deck, version)
        store.close_round('100001', version)
    current, ack = submit(store, deck, 11)
    assert [r['round_version'] for r in store.history('100001')['items']] == [10, 9, 8, 7, 6]
    assert store.detail('100001')['received_count'] == 1
    assert store.detail('100002')['round_version'] == 1
    with store.connect() as db:
        for table in ('rounds', 'hands', 'starts'):
            assert [r[0] for r in db.execute(f'SELECT DISTINCT version FROM {table} WHERE tenant_id=? ORDER BY version', ('100001',))] == [6, 7, 8, 9, 10, 11]
        assert db.execute('SELECT count(*) FROM requests WHERE tenant_id=?', ('100001',)).fetchone()[0] == 6
        assert db.execute('SELECT last_bound FROM clients WHERE tenant_id=? AND id=?', ('100001', 'x')).fetchone()[0] == 11
    assert store.process('100001', 'x', current) == ack
    with pytest.raises(DomainError) as e:
        store.process('100001', 'x', old)
    assert e.value.code == 'ROUND_CLOSED'
    assert store.detail('100001')['received_count'] == 1


def test_retention_uses_count_not_version_difference_and_retry_survives(store, deck):
    first, first_ack = submit(store, deck, 1)
    store.update_tenant('100001', {'round_version': 100})
    assert store.process('100001', 'x', first) == first_ack
    for version in (100, 200, 300, 400, 500):
        submit(store, deck, version)
        store.update_tenant('100001', {'round_version': version + 100})
    assert {r['round_version'] for r in store.history('100001')['items']} == {100, 200, 300, 400, 500}
    assert store.detail('100001')['round_version'] == 600


def test_daily_count_does_not_shrink_when_cards_are_deleted(store, deck):
    for version in range(1, 9):
        for i in range(7):
            submit(store, deck[i*13:(i+1)*13], version, cid=f'c{i}')
        store.close_round('100001', version)
    assert store.history()['total'] == 5
    assert store.summary()['completed_today'] == 8
    assert Store(store.path, store.clock).summary()['completed_today'] == 8


def test_hot_queries_use_version_and_deadline_indexes(store):
    with store.connect() as db:
        plan = lambda sql, args: ' '.join(r[3] for r in db.execute('EXPLAIN QUERY PLAN '+sql, args))
        assert 'starts_round' in plan('SELECT client_id,event_id FROM starts WHERE tenant_id=? AND version=?', ('100001', 1))
        assert 'requests_round' in plan("SELECT request_id FROM requests WHERE tenant_id=? AND json_extract(reply,'$.round_version')<?", ('100001', 10))
        assert 'rounds_expiry' in plan("SELECT tenant_id FROM rounds WHERE state!='closed' AND deadline_at<=?", (100,))


def test_upgrade_prunes_existing_database_and_backfills_daily_totals(store, deck, monkeypatch):
    prune = Store._prune_history
    monkeypatch.setattr(Store, '_prune_history', lambda *args: None)
    for version in range(1, 9):
        for i in range(7):
            submit(store, deck[i*13:(i+1)*13], version, cid=f'c{i}')
        store.close_round('100001', version)
    current, ack = submit(store, deck, 9, cid='current')
    with store.connect(True) as db:
        db.execute('DROP TABLE daily_stats')
        db.execute('DROP TABLE schema_migrations')
        db.execute('DROP INDEX starts_round')
    monkeypatch.setattr(Store, '_prune_history', prune)
    upgraded = Store(store.path, store.clock)
    assert upgraded.history()['total'] == 5
    assert upgraded.summary()['completed_today'] == 8
    assert upgraded.process('100001', 'current', current) == ack
    assert upgraded.detail('100001')['received_count'] == 1
    assert Store(store.path, store.clock).summary() == upgraded.summary()


def test_cleanup_failure_rolls_back_end_and_all_deletions(store, deck):
    for version in range(1, 6):
        submit(store, deck, version)
        store.close_round('100001', version)
    submit(store, deck, 6)
    with store.connect(True) as db:
        db.execute("CREATE TRIGGER fail_prune BEFORE DELETE ON rounds BEGIN SELECT RAISE(ABORT,'test cleanup failure'); END")
    with pytest.raises(sqlite3.IntegrityError):
        store.close_round('100001', 6)
    assert store.detail('100001')['round_version'] == 6
    assert store.detail('100001')['state'] == 'collecting'
    assert store.detail('100001', 1)['received_count'] == 1
    with store.connect(True) as db:
        assert db.execute('SELECT count(*) FROM starts').fetchone()[0] == 6
        assert db.execute('SELECT count(*) FROM requests').fetchone()[0] == 6
        db.execute('DROP TRIGGER fail_prune')
    store.close_round('100001', 6)
    assert store.history()['total'] == 5


def test_timeout_disable_and_returning_old_client_after_prune(store, deck):
    submit(store, deck, 1, cid='07')
    store.close_round('100001', 1)
    for version in range(2, 7):
        submit(store, deck, version, cid='12')
        store.close_round('100001', version)
    assert store.history()['total'] == 5
    submit(store, deck, 7, cid='07')
    store.update_tenant('100001', {'enabled': False})
    assert store.history()['total'] == 5
    assert store.detail('100001')['round_version'] == 8
    store.update_tenant('100001', {'enabled': True, 'timeout_seconds': 10})
    submit(store, deck, 8, cid='07')
    stamp = store.clock()
    store.clock = lambda: stamp + 11_000
    assert store.expire() == ['100001']
    assert store.history()['total'] == 5
    assert store.detail('100001')['round_version'] == 9
