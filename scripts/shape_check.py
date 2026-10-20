#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""建库数据形状自测（M7.5 GAP-05）。合成 feed，不读网络。

  python shape_check.py

CO-19（2026-10-20）：每趟车的**末站不可上客**。上游 GTFS 给终点站也标 pickup_type = 0，
客户端的出发列表只看 pickup_type = 0，于是终点站把「到站的车」当成「出发的车」列出来
（Liverpool Stand A 前 40 条里 33 条是幻影）。build_db.flush() 把每趟最后一行的 pickup_type 改成 1，
drop_off_type 不动；环线（首站 = 末站）只改末次停靠，首次停靠仍可上客。
"""
import datetime
import os
import sqlite3
import sys
import tempfile
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import build_db  # noqa: E402
import invariants  # noqa: E402

TODAY = datetime.date(2026, 10, 13)


def _csv(header, rows):
    return '\n'.join([','.join(header)] + [','.join(r) for r in rows]) + '\n'


def _trip(st, tid, hour, seq_stops):
    """seq_stops: [(stop_id, pickup, drop_off)]，按 stop_sequence 1.. 写。故意倒序写入，测 flush 先排序。"""
    rows = []
    for i, (s, pu, do) in enumerate(seq_stops):
        t = '%02d:%02d:00' % (hour, i)
        rows.append((tid, t, t, s, str(i + 1), str(pu), str(do)))
    st.extend(reversed(rows))


def make_feed(path):
    stops = []
    for sid, name in [('A', 'Alpha'), ('B', 'Bravo'), ('C', 'Charlie'), ('D', 'Delta'), ('E', 'Echo')]:
        stops.append((sid, '%s Station' % name, '-33.8', '151.0', '1', ''))
        stops.append((sid + '1', '%s Station Platform 1' % name, '-33.8', '151.0', '0', sid))
    st = []
    _trip(st, 'TWO', 10, [('A1', 0, 0), ('B1', 0, 0)])
    _trip(st, 'FIVE', 11, [('A1', 0, 1), ('B1', 0, 0), ('C1', 0, 0), ('D1', 0, 0), ('E1', 0, 0)])
    _trip(st, 'LOOP', 12, [('A1', 0, 0), ('B1', 0, 0), ('C1', 0, 0), ('A1', 0, 0)])
    files = {
        'agency.txt': _csv(['agency_id', 'agency_name', 'agency_url', 'agency_timezone'],
                           [('SydneyTrains', 'Sydney Trains', 'http://x', 'Australia/Sydney')]),
        'routes.txt': _csv(['route_id', 'agency_id', 'route_short_name', 'route_long_name',
                            'route_type'], [('R1', 'SydneyTrains', 'T9', 'Test line', '2')]),
        'calendar.txt': _csv(['service_id', 'monday', 'tuesday', 'wednesday', 'thursday',
                              'friday', 'saturday', 'sunday', 'start_date', 'end_date'],
                             [('S', '1', '1', '1', '1', '1', '1', '1', '20261001', '20261031')]),
        'trips.txt': _csv(['route_id', 'service_id', 'trip_id', 'trip_headsign', 'direction_id'],
                          [('R1', 'S', 'TWO', 'Bravo', '0'),
                           ('R1', 'S', 'FIVE', 'Echo', '0'),
                           ('R1', 'S', 'LOOP', 'Alpha', '0')]),
        'stops.txt': _csv(['stop_id', 'stop_name', 'stop_lat', 'stop_lon', 'location_type',
                           'parent_station'], stops),
        'stop_times.txt': _csv(['trip_id', 'arrival_time', 'departure_time', 'stop_id',
                                'stop_sequence', 'pickup_type', 'drop_off_type'], st),
    }
    with zipfile.ZipFile(path, 'w') as z:
        for n, body in files.items():
            z.writestr(n, body)


def flags(db, tid):
    return db.execute(
        'SELECT ps.stop_id, ps.pickup_type, ps.drop_off_type FROM trips t '
        'JOIN pattern_stops ps ON ps.pattern_id = t.pattern_id '
        'WHERE t.trip_id = ? ORDER BY ps.stop_sequence', (tid,)).fetchall()


def run():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        zp = os.path.join(td, 'feed.zip')
        out = os.path.join(td, 'out.sqlite')
        make_feed(zp)
        stats = build_db.build(zp, 'sydneytrains', 'test', out, 16, today=TODAY)
        db = sqlite3.connect(out)
        got = flags(db, 'TWO')
        assert got == [('A1', 0, 0), ('B1', 1, 0)], 'TWO: %r' % got
        got = flags(db, 'FIVE')
        assert got == [('A1', 0, 1), ('B1', 0, 0), ('C1', 0, 0), ('D1', 0, 0), ('E1', 1, 0)], \
            'FIVE: %r' % got
        got = flags(db, 'LOOP')
        assert got == [('A1', 0, 0), ('B1', 0, 0), ('C1', 0, 0), ('A1', 1, 0)], 'LOOP: %r' % got
        n = db.execute(invariants.TRIPFINAL_PICKUP0_SQL).fetchone()[0]
        assert n == 0, 'trip-final pickup_type = 0 patterns: %d' % n
        db.close()
        assert stats.get('tripfinal_pickup_cleared') == 3, stats.get('tripfinal_pickup_cleared')
        # 反例：把一个 pattern 的末站改回 pickup 0，invariants 的硬规则必须报出来
        db = sqlite3.connect(out)
        db.execute('UPDATE pattern_stops SET pickup_type = 0 WHERE pickup_type = 1 '
                   'AND stop_id = ? ', ('B1',))
        db.commit()
        db.close()
        fails = [f for f in invariants.check(out, 'sydneytrains') if '末站可上客' in f]
        assert fails, 'invariants must fail on a trip-final pickup_type = 0 row'
        # 反例（R5A-09）：把 TWO 的末站改成不可下客 -> 坐不了，invariants 的硬规则必须报出来
        db = sqlite3.connect(out)
        assert db.execute(invariants.UNRIDEABLE_SQL).fetchone()[0] == 0
        db.execute('UPDATE pattern_stops SET drop_off_type = 1 WHERE pattern_id = '
                   "(SELECT pattern_id FROM trips WHERE trip_id = 'TWO') AND stop_id = 'B1'")
        db.commit()
        db.close()
        fails = [f for f in invariants.check(out, 'sydneytrains') if '坐不了' in f]
        assert fails, 'invariants must fail on an unrideable pattern'
    print('shape_check OK')


if __name__ == '__main__':
    run()
