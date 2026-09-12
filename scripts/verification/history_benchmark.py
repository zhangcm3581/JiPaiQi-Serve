"""Isolated synthetic history benchmark; never opens the deployed database."""
import argparse
import importlib.util
import json
import math
import random
import shutil
import statistics
import sys
import tempfile
import time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from app.domain import Store, RANKS, SUITS, encode

p = argparse.ArgumentParser()
p.add_argument('--baseline-domain', type=Path, required=True)
p.add_argument('--rounds', type=int, default=10000)
p.add_argument('--output', type=Path, required=True)
a = p.parse_args()
spec = importlib.util.spec_from_file_location('baseline_domain', a.baseline_domain)
old = importlib.util.module_from_spec(spec)
spec.loader.exec_module(old)
deck = [{'rank': r, 'suit': s} for _ in range(2) for s in SUITS for r in RANKS]
random.Random(42).shuffle(deck)
stamp = int(time.time()*1000)

def stats(values):
    values = sorted(values)
    return {'mean_ms': statistics.mean(values), 'p50_ms': statistics.median(values), 'p95_ms': values[math.ceil(len(values)*.95)-1], 'max_ms': max(values), 'samples': len(values)}

def measure(store):
    timings = []
    for _ in range(50):
        begin = time.perf_counter(); store.detail('100001'); timings.append((time.perf_counter()-begin)*1000)
    return stats(timings)

with tempfile.TemporaryDirectory(prefix='jpq-history-benchmark-') as directory:
    directory = Path(directory)
    source = directory/'baseline.sqlite3'
    baseline = old.Store(source, lambda: stamp)
    baseline.create_tenant('100001', round_version=a.rounds+1)
    cards = encode(deck[:13])
    with baseline.connect(True) as db:
        db.executemany('INSERT INTO clients VALUES(?,?,?,?)', [('100001', f'c{i}', a.rounds, stamp) for i in range(7)])
        db.executemany("INSERT INTO rounds(tenant_id,version,state,created_at,ready_at,closed_at,result,calculation_ms) VALUES(?,?,'closed',?,?,?,?,?)", [('100001',v,stamp,stamp,stamp,'[]',1.0) for v in range(1,a.rounds+1)])
        db.executemany('INSERT INTO starts VALUES(?,?,?,?,?)', [('100001',f'c{i}',f'e-{v}-{i}',v,cards) for v in range(1,a.rounds+1) for i in range(7)])
        db.executemany('INSERT INTO hands VALUES(?,?,?,?,?)', [('100001',v,f'c{i}',cards,stamp) for v in range(1,a.rounds+1) for i in range(7)])
        db.executemany('INSERT INTO requests VALUES(?,?,?,?,?)', [('100001',f'c{i}',f'r-{v}-{i}','fingerprint',encode({'round_version':v,'type':'ack'})) for v in range(1,a.rounds+1) for i in range(7)])
    result = {'closed_rounds_before': a.rounds, 'history_hands_before': a.rounds*7, 'baseline_detail': measure(baseline)}
    with baseline.connect(True) as db:
        db.execute('CREATE INDEX starts_round ON starts(tenant_id,version,client_id,event_id)')
    result['indexed_only_detail'] = measure(baseline)
    begin = time.perf_counter(); upgraded = Store(source, lambda: stamp); result['upgrade_ms'] = (time.perf_counter()-begin)*1000
    result['retained_detail'] = measure(upgraded)
    with upgraded.connect() as db:
        result['rows_after'] = {t: db.execute(f'SELECT count(*) FROM {t}').fetchone()[0] for t in ('rounds','hands','starts','requests')}
        result['starts_query_plan'] = [r[3] for r in db.execute('EXPLAIN QUERY PLAN SELECT client_id,event_id FROM starts WHERE tenant_id=? AND version=?', ('100001',a.rounds+1))]
    result['completed_today_after'] = upgraded.summary()['completed_today']
    a.output.parent.mkdir(parents=True, exist_ok=True)
    a.output.write_text(json.dumps(result, ensure_ascii=False, indent=2)+'\n')
    print(json.dumps(result, ensure_ascii=False, indent=2))
