#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""非营运 headsign（'Empty Train' 等）的建库口径自测。合成 feed，不读网络。

  python nonrevenue_check.py

口径（2026-10-06 修，见 build_db.NON_REVENUE_HEADSIGNS 的注释）：
  * headsign 是非营运标记、但至少一站 pickup_type == 0 -> **保留**（与 flush() 的「可上客」同一口径；
    只有 pickup_type 2/3 预约上客站的 trip 仍算一站都不让上，照旧丢），headsign 改写成
    最后一个可下客站的站名（父站名去掉 ' Station'），各站 pickup/drop_off 原样保留；
  * headsign 是非营运标记、一站都不让上 -> 照旧丢弃。
"""
import datetime
import io
import os
import sqlite3
import sys
import tempfile
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import build_db  # noqa: E402

TODAY = datetime.date(2026, 10, 13)


def _csv(header, rows):
    return '\n'.join([','.join(header)] + [','.join(r) for r in rows]) + '\n'


def make_feed(path):
    stops = []
    for sid, name in [('A', 'Alpha'), ('B', 'Bravo'), ('C', 'Charlie'), ('D', 'Delta')]:
        stops.append((sid, '%s Station' % name, '-33.8', '151.0', '1', ''))
        stops.append((sid + '1', '%s Station Platform 1' % name, '-33.8', '151.0', '0', sid))
    st = []
    # KEEP：A、B 可上客；C 只下客；D 通过不停（pu=1, do=1） -> 末个可下客站 = Charlie
    for i, (s, pu, do) in enumerate([('A1', 0, 1), ('B1', 0, 0), ('C1', 1, 0), ('D1', 1, 1)]):
        st.append(('KEEP', '%02d:%02d:00' % (12, i), '%02d:%02d:00' % (12, i), s, str(i + 1),
                   str(pu), str(do)))
    # DROP：全程不上客
    for i, s in enumerate(['A1', 'B1', 'C1']):
        st.append(('DROP', '13:0%d:00' % i, '13:0%d:00' % i, s, str(i + 1), '1', '0'))
    # REAL：普通营运车，对照组
    for i, s in enumerate(['A1', 'B1']):
        st.append(('REAL', '14:0%d:00' % i, '14:0%d:00' % i, s, str(i + 1), '0', '0'))
    files = {
        'agency.txt': _csv(['agency_id', 'agency_name', 'agency_url', 'agency_timezone'],
                           [('SydneyTrains', 'Sydney Trains', 'http://x', 'Australia/Sydney')]),
        'routes.txt': _csv(['route_id', 'agency_id', 'route_short_name', 'route_long_name',
                            'route_type'], [('R1', 'SydneyTrains', 'T9', 'Test line', '2')]),
        'calendar.txt': _csv(['service_id', 'monday', 'tuesday', 'wednesday', 'thursday',
                              'friday', 'saturday', 'sunday', 'start_date', 'end_date'],
                             [('S', '1', '1', '1', '1', '1', '1', '1', '20261001', '20261031')]),
        'trips.txt': _csv(['route_id', 'service_id', 'trip_id', 'trip_headsign', 'direction_id'],
                          [('R1', 'S', 'KEEP', 'Empty Train', '0'),
                           ('R1', 'S', 'DROP', 'Empty Train', '0'),
                           ('R1', 'S', 'REAL', 'Bravo', '0')]),
        'stops.txt': _csv(['stop_id', 'stop_name', 'stop_lat', 'stop_lon', 'location_type',
                           'parent_station'], stops),
        'stop_times.txt': _csv(['trip_id', 'arrival_time', 'departure_time', 'stop_id',
                                'stop_sequence', 'pickup_type', 'drop_off_type'], st),
    }
    with zipfile.ZipFile(path, 'w') as z:
        for n, body in files.items():
            z.writestr(n, body)


def run():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        zp = os.path.join(td, 'feed.zip')
        out = os.path.join(td, 'out.sqlite')
        make_feed(zp)
        build_db.build(zp, 'sydneytrains', 'test', out, 16, today=TODAY)
        db = sqlite3.connect(out)
        trips = dict(db.execute('SELECT trip_id, headsign FROM trips'))
        assert 'DROP' not in trips, 'all-no-pickup Empty Train must be dropped: %r' % trips
        assert trips.get('REAL') == 'Bravo', trips
        assert trips.get('KEEP') == 'Charlie', \
            'Empty Train with pickups must be kept, headsign = last alighting stop: %r' % trips
        dgs = [r[0] for r in db.execute('SELECT headsign_pattern FROM direction_groups')]
        assert not any(build_db.is_non_revenue_headsign(h) for h in dgs), dgs
        flags = db.execute(
            'SELECT ps.stop_id, ps.pickup_type, ps.drop_off_type FROM trips t '
            'JOIN pattern_stops ps ON ps.pattern_id = t.pattern_id '
            "WHERE t.trip_id = 'KEEP' ORDER BY ps.stop_sequence").fetchall()
        assert flags == [('A1', 0, 1), ('B1', 0, 0), ('C1', 1, 0)], flags
        db.close()
    print('nonrevenue_check OK')


if __name__ == '__main__':
    run()
