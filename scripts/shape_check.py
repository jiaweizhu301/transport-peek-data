#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""建库数据形状自测（M7.5 GAP-05）。合成 feed，不读网络。

  python shape_check.py

CO-19（2026-10-20）：每趟车的**末站不可上客**。上游 GTFS 给终点站也标 pickup_type = 0，
客户端的出发列表只看 pickup_type = 0，于是终点站把「到站的车」当成「出发的车」列出来
（Liverpool Stand A 前 40 条里 33 条是幻影）。build_db.flush() 把每趟最后一行的 pickup_type 改成 1，
drop_off_type 不动；环线（首站 = 末站）只改末次停靠，首次停靠仍可上客。

XCO-09（2026-10-20）：服务窗口从**昨天（D−1）**起算。cron 在悉尼 02:30 跑（常晚几小时），那一刻
D−1 运营日还在跑（24:00 以后的车，公交最晚 29:42），本地 04:00 前客户端也按 D−1 运营日查。
GAP-05 的四个计数（invariants.shape_counts）：末站可上客 / 同一父站两次可上客的环线 /
28:00 以后才发车的 trip / D−1 覆盖。
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
import gtfs_modes  # noqa: E402
import invariants  # noqa: E402

TODAY = datetime.date(2026, 10, 13)


def _csv(header, rows):
    return '\n'.join([','.join(header)] + [','.join(r) for r in rows]) + '\n'


D1 = 20261012          # TODAY − 1


def _trip(st, tid, hour, seq_stops):
    """seq_stops: [(stop_id, pickup, drop_off)]，按 stop_sequence 1.. 写。故意倒序写入，测 flush 先排序。
    hour 可以 >= 24（GTFS 运营日秒数，跨午夜的车）。"""
    rows = []
    for i, (s, pu, do) in enumerate(seq_stops):
        t = '%02d:%02d:00' % (hour, 30 + i)
        rows.append((tid, t, t, s, str(i + 1), str(pu), str(do)))
    st.extend(reversed(rows))


def make_feed(path):
    stops = []
    for sid, name in [('A', 'Alpha'), ('B', 'Bravo'), ('C', 'Charlie'), ('D', 'Delta'), ('E', 'Echo')]:
        stops.append((sid, '%s Station' % name, '-33.8', '151.0', '1', ''))
        stops.append((sid + '1', '%s Station Platform 1' % name, '-33.8', '151.0', '0', sid))
    stops.append(('A2', 'Alpha Station Platform 2', '-33.8', '151.0', '0', 'A'))
    st = []
    _trip(st, 'TWO', 10, [('A1', 0, 0), ('B1', 0, 0)])
    _trip(st, 'FIVE', 11, [('A1', 0, 1), ('B1', 0, 0), ('C1', 0, 0), ('D1', 0, 0), ('E1', 0, 0)])
    _trip(st, 'LOOP', 12, [('A1', 0, 0), ('B1', 0, 0), ('C1', 0, 0), ('A1', 0, 0)])
    # City Circle 形状（CO-10）：同一父站 Alpha 两次可上客，两次走不同站台（Central 200060 实测如此）
    _trip(st, 'CIRCLE', 13, [('A1', 0, 0), ('B1', 0, 0), ('C1', 0, 0), ('A2', 0, 0), ('D1', 0, 0)])
    # 只在 D−1 跑的夜班车，28:30 才发车（= 次日 04:30）：窗口从 D 起算时整趟丢失
    _trip(st, 'LATE', 28, [('D1', 0, 0), ('E1', 0, 0)])
    files = {
        'agency.txt': _csv(['agency_id', 'agency_name', 'agency_url', 'agency_timezone'],
                           [('SydneyTrains', 'Sydney Trains', 'http://x', 'Australia/Sydney')]),
        'routes.txt': _csv(['route_id', 'agency_id', 'route_short_name', 'route_long_name',
                            'route_type'], [('R1', 'SydneyTrains', 'T9', 'Test line', '2')]),
        'calendar.txt': _csv(['service_id', 'monday', 'tuesday', 'wednesday', 'thursday',
                              'friday', 'saturday', 'sunday', 'start_date', 'end_date'],
                             [('S', '1', '1', '1', '1', '1', '1', '1', '20261001', '20261031')]),
        'calendar_dates.txt': _csv(['service_id', 'date', 'exception_type'],
                                   [('Y', str(D1), '1')]),
        'trips.txt': _csv(['route_id', 'service_id', 'trip_id', 'trip_headsign', 'direction_id'],
                          [('R1', 'S', 'TWO', 'Bravo', '0'),
                           ('R1', 'S', 'FIVE', 'Echo', '0'),
                           ('R1', 'S', 'LOOP', 'Alpha', '0'),
                           ('R1', 'S', 'CIRCLE', 'Delta', '0'),
                           ('R1', 'Y', 'LATE', 'Echo', '0')]),
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
        assert stats.get('tripfinal_pickup_cleared') == 5, stats.get('tripfinal_pickup_cleared')
        # XCO-09：窗口 = D−1 起 16 天（D−1 … D+14），D−1 才有的 LATE 在库里
        db = sqlite3.connect(out)
        meta = db.execute('SELECT calendar_start, calendar_end FROM meta').fetchone()
        assert meta == (D1, 20261027), 'meta calendar window: %r' % (meta,)
        assert stats['calendar_days'] == 16, stats['calendar_days']
        got = flags(db, 'LATE')
        assert got == [('D1', 0, 0), ('E1', 1, 0)], 'LATE (D−1 only) must be kept: %r' % got
        # GAP-05 四个计数
        c = invariants.shape_counts(db, TODAY)
        db.close()
        assert c['tripfinal_pickup0'] == 0, c
        assert c['loop_patterns'] == 1 and c['loop_trips'] == 1, c     # CIRCLE；LOOP 末次已不可上客
        assert c['start_ge_28'] == 1, c                                 # LATE 28:30
        assert c['d1'] == D1 and c['d1_covered'] is True, c
        assert c['d1_late_trips'] == 1, c                               # D−1 跑过午夜的 = LATE
        # feed 回溯不到 D−1（feed 从今天才开始）：窗口从 feed 起点，计数如实报「未覆盖」
        out2 = os.path.join(td, 'out2.sqlite')
        build_db.build(zp, 'sydneytrains', 'test', out2, 16, today=datetime.date(2026, 10, 1))
        db = sqlite3.connect(out2)
        assert db.execute('SELECT calendar_start FROM meta').fetchone()[0] == 20261001
        c = invariants.shape_counts(db, datetime.date(2026, 10, 1))
        db.close()
        assert c['d1'] == 20260930 and c['d1_covered'] is False and c['d1_late_trips'] == 0, c
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
    check_lightrail_feeds()
    print('shape_check OK')


def _routes_zip(path, rows):
    with zipfile.ZipFile(path, 'w') as z:
        z.writestr('routes.txt', _csv(['route_id', 'agency_id', 'route_short_name', 'route_type'], rows))


def check_lightrail_feeds():
    """R5A-12（2026-10-20）：lightrail = innerwest (L1) + cbdandsoutheast (L2/L3/LX) + parramatta (L4)。
    L4 漏了一整条线：它不在 MODES 里，库里就没有 16 个站，用户搜「Ngara」什么也搜不到。"""
    feeds = gtfs_modes.feeds_for('lightrail')
    assert feeds == ['/v1/gtfs/schedule/lightrail/innerwest',
                     '/v1/gtfs/schedule/lightrail/cbdandsoutheast',
                     '/v1/gtfs/schedule/lightrail/parramatta'], feeds
    # 第三个子 feed 与前两个撞 id -> 必须响亮失败（不许后写覆盖前写让一条线消失）
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        zs = [os.path.join(td, '%d.zip' % i) for i in range(3)]
        _routes_zip(zs[0], [('IWL', 'LR', 'L1', '900')])
        _routes_zip(zs[1], [('CSE', 'SLR', 'L2', '900')])
        _routes_zip(zs[2], [('PLR', 'PLR', 'L4', '900')])
        build_db.Feed(zs).check_no_id_collisions()
        _routes_zip(zs[2], [('PLR', 'PLR', 'L4', '900'), ('IWL', 'PLR', 'L4x', '900')])
        try:
            build_db.Feed(zs).check_no_id_collisions()
        except SystemExit as e:
            assert '子 feed 0 与 2' in str(e), e
        else:
            raise AssertionError('route_id collision between sub-feed 0 and 2 must fail')


if __name__ == '__main__':
    run()
