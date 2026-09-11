#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""从真实 GTFS zip 裁出**冻结的**黄金样本输入 `golden.<mode>.gtfs.zip`（一次性，产物入库）。

  python make_golden_input.py <gtfs_dir> <out_dir> [mode ...]

黄金样本口径 = 「冻结的输入 -> 期望输出」，不去比对会漂移的官网（M1-D4）。
挑选的父站刻意覆盖 D2 的四个分支：
  * agency 混入（sydneytrains 里的 NSWTrains 车次要被裁掉）
  * platform_code 从站名解析
  * 「通过不停靠」行（pickup=1 且 dropoff=1）
  * 非营运班次（RTTA 空车）
产物冻结后不要重跑；真要重跑就同时用 gen_golden_expected.py 重出期望值，并在 STATUS-D 记一笔。
"""
import csv
import io
import os
import sys
import zipfile

SELECT = {
    # 父站 stop_id（Central / Strathfield / Hornsby / Chatswood）
    'sydneytrains': ['200060', '202020', '207720', '206710'],
    'metro': ['206710', '212110', '200060'],
}
FILES = ['agency.txt', 'routes.txt', 'trips.txt', 'stops.txt', 'stop_times.txt',
         'calendar.txt', 'calendar_dates.txt', 'transfers.txt']
MAX_TRIPS = 400


def rows(z, name):
    if name not in z.namelist():
        return None
    with z.open(name) as fh:
        return list(csv.DictReader(io.TextIOWrapper(fh, encoding='utf-8-sig')))


def write(zo, name, fields, data):
    buf = io.StringIO(newline='')
    w = csv.DictWriter(buf, fieldnames=fields, lineterminator='\n')
    w.writeheader()
    for r in data:
        w.writerow({k: r.get(k, '') for k in fields})
    zo.writestr(name, buf.getvalue())


def build(src, out, mode):
    z = zipfile.ZipFile(src)
    stops = rows(z, 'stops.txt')
    parents = set(SELECT[mode])
    probe = parents | {s['stop_id'] for s in stops if s.get('parent_station') in parents}

    st = rows(z, 'stop_times.txt')
    hit_trips = {r['trip_id'] for r in st if r['stop_id'] in probe}
    trips = [t for t in rows(z, 'trips.txt') if t['trip_id'] in hit_trips]
    trips.sort(key=lambda t: t['trip_id'])
    trips = trips[::max(1, len(trips) // MAX_TRIPS)][:MAX_TRIPS]
    keep_trips = {t['trip_id'] for t in trips}

    st = [r for r in st if r['trip_id'] in keep_trips]
    keep_stops = {r['stop_id'] for r in st} | probe
    # 带上父站
    by_id = {s['stop_id']: s for s in stops}
    for sid in list(keep_stops):
        p = by_id.get(sid, {}).get('parent_station')
        if p:
            keep_stops.add(p)
    for s in stops:                       # 保留被选父站的全部子站
        if s.get('parent_station') in keep_stops:
            keep_stops.add(s['stop_id'])
    stops = [s for s in stops if s['stop_id'] in keep_stops]

    keep_routes = {t['route_id'] for t in trips}
    keep_svc = {t['service_id'] for t in trips}
    routes = [r for r in rows(z, 'routes.txt') if r['route_id'] in keep_routes]
    cal = [c for c in rows(z, 'calendar.txt') if c['service_id'] in keep_svc]
    cd = rows(z, 'calendar_dates.txt')
    cd = [c for c in cd if c['service_id'] in keep_svc] if cd else []
    tf = rows(z, 'transfers.txt')
    tf = [t for t in tf if t['from_stop_id'] in keep_stops and t['to_stop_id'] in keep_stops] \
        if tf else []

    dst = os.path.join(out, 'golden.%s.gtfs.zip' % mode)
    with zipfile.ZipFile(dst, 'w', zipfile.ZIP_DEFLATED, compresslevel=9) as zo:
        write(zo, 'agency.txt', list(rows(z, 'agency.txt')[0].keys()), rows(z, 'agency.txt'))
        write(zo, 'routes.txt', list(routes[0].keys()), routes)
        write(zo, 'trips.txt', list(trips[0].keys()), trips)
        write(zo, 'stops.txt', list(stops[0].keys()), stops)
        write(zo, 'stop_times.txt', list(st[0].keys()), st)
        write(zo, 'calendar.txt', list(cal[0].keys()), cal)
        if cd:
            write(zo, 'calendar_dates.txt', list(cd[0].keys()), cd)
        if tf:
            write(zo, 'transfers.txt', list(tf[0].keys()), tf)
    print('%-13s %d trips / %d stop_times / %d stops -> %.0f KB'
          % (mode, len(trips), len(st), len(stops), os.path.getsize(dst) / 1024))


if __name__ == '__main__':
    src_dir, out_dir = sys.argv[1], sys.argv[2]
    os.makedirs(out_dir, exist_ok=True)
    for m in (sys.argv[3:] or ['sydneytrains', 'metro']):
        build(os.path.join(src_dir, m + '.zip'), out_dir, m)
