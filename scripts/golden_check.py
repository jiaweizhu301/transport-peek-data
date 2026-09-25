#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""D4 · 黄金样本校验：冻结输入 -> 期望输出，逐表比对。

  python golden_check.py [--update]

输入 = `pipeline/golden/golden.<mode>.gtfs.zip`（冻结，不随上游漂移）。
期望 = `pipeline/golden/expected.json`（行数 + 每表内容 sha256 + 抽样行）。
`--today` / `static_version` 钉死、窗口取 `gtfs_modes` 的每模式缺省，因此输出是可复现的。
`--update` 重出期望值 —— 只有在**有意**改动 D2 口径时才可以跑，且必须在 STATUS-D 记一笔。
任一不符 -> 退出码 1 -> 不发 Release（D4 口径）。
"""
import argparse
import hashlib
import json
import os
import sqlite3
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
GOLDEN = os.path.join(os.path.dirname(HERE), 'golden')
sys.path.insert(0, HERE)
import build_db
import tripoffsets    # noqa: E402  —— trips.offsets 的唯一 Python 编解码实现
from gtfs_modes import days_for

MODES = ['sydneytrains', 'metro']
PINNED_TODAY = __import__('datetime').date(2026, 9, 7)
PINNED_VERSION = '20260907-0000'
# 日历窗口不再钉死成一个数：直接吃 gtfs_modes 里每模式的缺省值（sydneytrains=16 / metro=120），
# 这样「谁改了某个模式的缺省窗口」也会被黄金样本抓住，而不是只抓建库逻辑。

# schema 2：stop_times 是视图（而且没有时刻列），换成它底下的两张真表。
# trips 的哈希里包含 offsets blob，所以时刻的任何变化都会在这里体现。
TABLES = ['meta', 'stops', 'routes', 'direction_groups', 'trips', 'pattern_stops',
          'stop_patterns', 'calendar', 'calendar_dates', 'transfers']
# meta.generated_at 是生成时间，必然变；比对时剔除
META_COLS = 'id, schema_version, mode, static_version, calendar_start, calendar_end'


def snapshot(db_path):
    db = sqlite3.connect('file:%s?immutable=1' % db_path.replace('\\', '/'), uri=True)
    out = {'tables': {}}
    blob_cache = {}
    seq_cache = {}
    for t in TABLES:
        cols = META_COLS if t == 'meta' else '*'
        rows = db.execute('SELECT %s FROM %s' % (cols, t)).fetchall()
        rows.sort(key=lambda r: tuple('' if v is None else str(v) for v in r))
        h = hashlib.sha256()
        for r in rows:
            h.update(repr(r).encode('utf-8'))
        out['tables'][t] = {'rows': len(rows), 'sha256': h.hexdigest()}
    out['platform_code_rows'] = db.execute(
        'SELECT count(*) FROM stops WHERE platform_code IS NOT NULL').fetchone()[0]
    # schema 2：departure 要解 blob 才有。取样按 (stop_id, departure, trip_id) 排序，
    # 口径与 schema 1 那版一致，这样黄金样本里这几行的语义没变、只是算法变了。
    sample = []
    for tid, sid, seq, pid, start, blob, nst, hs, dk in db.execute(
            'SELECT t.trip_id, ps.stop_id, ps.stop_sequence, t.pattern_id, t.start_secs, '
            '       t.offsets, sp.n_stops, t.headsign, g.direction_key '
            'FROM pattern_stops ps JOIN trips t ON t.pattern_id = ps.pattern_id '
            'JOIN stop_patterns sp ON sp.pattern_id = t.pattern_id '
            'JOIN direction_groups g ON g.id = t.direction_group_id'):
        seqs = seq_cache.get(pid)
        if seqs is None:
            seqs = [r[0] for r in db.execute(
                'SELECT stop_sequence FROM pattern_stops WHERE pattern_id = ? '
                'ORDER BY stop_sequence', (pid,))]
            seq_cache[pid] = seqs
        times = blob_cache.get(tid)
        if times is None:
            times = tripoffsets.decode(start, blob, n_stops=nst)
            blob_cache[tid] = times
        sample.append((tid, sid, times[seqs.index(seq)][1], hs, dk))
    sample.sort(key=lambda r: (r[1], r[2], r[0]))
    out['sample_departures'] = sample[:5]
    out['sample_platform'] = db.execute(
        'SELECT stop_id, name, platform_code FROM stops '
        'WHERE platform_code IS NOT NULL ORDER BY stop_id LIMIT 5').fetchall()
    db.close()
    return out


def run():
    got = {}
    tmp = tempfile.mkdtemp(prefix='tp-golden-')
    for mode in MODES:
        zp = os.path.join(GOLDEN, 'golden.%s.gtfs.zip' % mode)
        out = os.path.join(tmp, mode + '.sqlite')
        stats = build_db.build(zp, mode, PINNED_VERSION, out, days_for(mode), PINNED_TODAY)
        snap = snapshot(out)
        snap['build_stats'] = {k: v for k, v in stats.items() if k != 'sqlite_bytes'}
        got[mode] = snap
    return got


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--update', action='store_true')
    a = ap.parse_args()
    exp_path = os.path.join(GOLDEN, 'expected.json')
    got = json.loads(json.dumps(run()))          # tuple -> list，与 JSON 往返一致
    if a.update or not os.path.exists(exp_path):
        with open(exp_path, 'w', encoding='utf-8') as f:
            json.dump(got, f, ensure_ascii=False, indent=1, sort_keys=True)
        print('expected.json 已重写（%d 模式）' % len(got))
        return 0
    exp = json.load(open(exp_path, encoding='utf-8'))
    bad = []
    for mode in MODES:
        for t in TABLES:
            e, g = exp[mode]['tables'][t], got[mode]['tables'][t]
            if e != g:
                bad.append('%s.%s 期望 rows=%d sha=%s，实得 rows=%d sha=%s'
                           % (mode, t, e['rows'], e['sha256'][:12], g['rows'], g['sha256'][:12]))
        for k in ('platform_code_rows', 'sample_departures', 'sample_platform'):
            if exp[mode][k] != got[mode][k]:
                bad.append('%s.%s 不符' % (mode, k))
    for b in bad:
        print('FAIL ' + b)
    if bad:
        print('黄金样本校验失败：%d 处' % len(bad))
        return 1
    print('黄金样本校验通过：%s，共 %d 表逐表 sha256 一致'
          % ('/'.join(MODES), len(TABLES)))
    return 0


if __name__ == '__main__':
    sys.exit(main())
