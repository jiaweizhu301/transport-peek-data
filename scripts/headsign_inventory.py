#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""D4 · 全量 headsign 去重清单：上游到底写了哪些「开往哪里」。

  python headsign_inventory.py <gtfs_dir> [mode ...] [-o pipeline/reports/headsign-inventory.json]

为什么要有这个产物（2026-09-07 真机在 Central 看到「开往 Empty Train」之后加的）：

  1. **判「某个词要不要进 NON_REVENUE_HEADSIGNS」时有数可依，不用再拉一次上游。**
     每条 headsign 都带 trip 数、`pickup_type=0` 的行数与 trip 数 —— 一个词到底是
     「真的没人上车的调车」还是「标签含糊的真实班次」，看这两个数就能判，不用猜。
  2. **上游冒出新的非营运写法时，diff 这份清单就能发现**（`generated_at` 之外全是稳定排序）。

口径：
  * 逐 feed 统计，headsign 原样保留（不归一化），空 headsign 记成 `""`
  * `trips` = trips.txt 里的条数；`trips_with_stop_times` = 真有停站行的条数
    （上游有大量只在 trips.txt 里挂名、stop_times.txt 里一行都没有的 trip）
  * `pickup_rows` = 该 headsign 的全部停站行数；`pickup_allowed_rows` = 其中 `pickup_type=0` 的行数；
    `trips_with_pickup` = 至少有一站可上客的 trip 数 —— **这三个数是判非营运的关键**
  * `non_revenue` = 用 `build_db.is_non_revenue_headsign()` 现算的判定结果（管线的唯一真相）
  * **不做 agency 过滤、不做日历窗口裁剪**：这是「上游原样」的清单，不是「建库后」的清单
"""
import argparse
import collections
import csv
import datetime
import io
import json
import os
import sys
import zipfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import build_db
from gtfs_modes import MODES

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_OUT = os.path.join(os.path.dirname(HERE), 'reports', 'headsign-inventory.json')

# P 流手写词表里比管线多出来的 7 个词（2026-09-07 提出）。清单里单列一节，
# 让「要不要收进 NON_REVENUE_HEADSIGNS」这个问题有数可依，而不是靠猜。
CANDIDATE_WORDS = [
    'empty stock',
    'no passengers',
    'set down only',
    'to depot',
    'depot only',
    'staff only',
    'test train',
]


def rows(z, name):
    if name not in z.namelist():
        return iter(())
    return csv.DictReader(io.TextIOWrapper(z.open(name), encoding='utf-8-sig'))


def scan(zip_path, mode):
    z = zipfile.ZipFile(zip_path)
    routes = {r['route_id']: (r.get('agency_id') or '', r.get('route_short_name') or '',
                              r.get('route_long_name') or '')
              for r in rows(z, 'routes.txt')}

    hs_of = {}
    agg = collections.defaultdict(lambda: {
        'trips': 0, 'trips_with_stop_times': 0, 'stop_time_rows': 0,
        'pickup_allowed_rows': 0, 'trips_with_pickup': 0,
        'agencies': collections.Counter(), 'routes': collections.Counter()})
    for t in rows(z, 'trips.txt'):
        hs = (t.get('trip_headsign') or '').strip()
        hs_of[t['trip_id']] = hs
        a = agg[hs]
        a['trips'] += 1
        ag, short, long_ = routes.get(t['route_id'], ('?', '?', '?'))
        a['agencies'][ag] += 1
        a['routes']['%s|%s|%s' % (t['route_id'], short, long_)] += 1

    cur, n_rows, n_pu0 = None, 0, 0

    def flush():
        if cur is None or not n_rows:
            return
        a = agg[hs_of[cur]]
        a['trips_with_stop_times'] += 1
        a['stop_time_rows'] += n_rows
        a['pickup_allowed_rows'] += n_pu0
        if n_pu0:
            a['trips_with_pickup'] += 1

    for r in rows(z, 'stop_times.txt'):
        tid = r['trip_id']
        if tid not in hs_of:
            continue
        if tid != cur:
            flush()
            cur, n_rows, n_pu0 = tid, 0, 0
        n_rows += 1
        if int(r.get('pickup_type') or 0) == 0:
            n_pu0 += 1
    flush()

    out = []
    for hs, a in agg.items():
        top = a['routes'].most_common(5)
        out.append({
            'headsign': hs,
            'trips': a['trips'],
            'trips_with_stop_times': a['trips_with_stop_times'],
            'stop_time_rows': a['stop_time_rows'],
            'pickup_allowed_rows': a['pickup_allowed_rows'],
            'trips_with_pickup': a['trips_with_pickup'],
            'non_revenue': build_db.is_non_revenue_headsign(hs),
            'agencies': dict(sorted(a['agencies'].items())),
            'top_routes': [{'route': k.split('|')[0], 'short_name': k.split('|')[1],
                            'long_name': k.split('|')[2], 'trips': v} for k, v in top],
        })
    out.sort(key=lambda e: (-e['trips'], e['headsign']))
    return out


def candidate_report(feeds):
    """P 流那 7 个候选词在真实数据里各命中多少（子串口径，与管线判定一致）。"""
    out = []
    for w in CANDIDATE_WORDS:
        hits = []
        for mode, entries in feeds.items():
            for e in entries:
                if w in build_db.normalize_headsign(e['headsign']):
                    hits.append({'mode': mode, 'headsign': e['headsign'], 'trips': e['trips'],
                                 'trips_with_pickup': e['trips_with_pickup']})
        out.append({
            'word': w,
            'matched_headsigns': len(hits),
            'matched_trips': sum(h['trips'] for h in hits),
            'matched_trips_with_pickup': sum(h['trips_with_pickup'] for h in hits),
            'hits': hits,
        })
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('gtfs_dir', help='含 <mode>.zip 的目录（可含多个目录，用逗号分隔）')
    ap.add_argument('modes', nargs='*', default=None)
    ap.add_argument('-o', '--out', default=DEFAULT_OUT)
    a = ap.parse_args()

    dirs = [d for d in a.gtfs_dir.split(',') if d]
    modes = a.modes or list(MODES)
    feeds, missing = {}, []
    for mode in modes:
        zp = next((os.path.join(d, mode + '.zip') for d in dirs
                   if os.path.exists(os.path.join(d, mode + '.zip'))), None)
        if not zp:
            missing.append(mode)
            continue
        feeds[mode] = scan(zp, mode)
        print('%-13s %d 种 headsign / %d trips'
              % (mode, len(feeds[mode]), sum(e['trips'] for e in feeds[mode])), flush=True)
    if missing:
        print('（未扫描：%s —— 目录里没有对应 zip）' % '/'.join(missing), flush=True)

    doc = {
        '_source': 'pipeline/scripts/headsign_inventory.py',
        '_warning': '自动生成，不要手改。重跑：python pipeline/scripts/headsign_inventory.py <gtfs_dir>',
        'schema_version': 1,
        'generated_at': datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0)
                        .isoformat().replace('+00:00', 'Z'),
        'note': '上游 GTFS 原样的 headsign 去重清单（未做 agency 过滤、未裁日历窗口）。'
                '判「某个词要不要进 build_db.NON_REVENUE_HEADSIGNS」时看 trips_with_pickup：'
                '它 = 至少有一站可上客的 trip 数，接近 0 才是真调车。',
        'non_revenue_patterns': sorted(build_db.NON_REVENUE_HEADSIGNS),
        'scanned_modes': sorted(feeds),
        'not_scanned_modes': sorted(missing),
        'summary': {mode: {
            'distinct_headsigns': len(entries),
            'trips': sum(e['trips'] for e in entries),
            'non_revenue_headsigns': sum(1 for e in entries if e['non_revenue']),
            'non_revenue_trips': sum(e['trips'] for e in entries if e['non_revenue']),
        } for mode, entries in sorted(feeds.items())},
        'candidate_words': candidate_report(feeds),
        'feeds': {mode: entries for mode, entries in sorted(feeds.items())},
    }
    os.makedirs(os.path.dirname(os.path.abspath(a.out)), exist_ok=True)
    with open(a.out, 'w', encoding='utf-8') as f:
        json.dump(doc, f, ensure_ascii=False, indent=1)
    print('%s  %d 字节' % (a.out, os.path.getsize(a.out)), flush=True)


if __name__ == '__main__':
    main()
