#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""D4 · 不变量校验（管线日常自检）。任一失败 -> 退出码 1 -> 不发 Release + 告警。

  python invariants.py <build_dir> [mode ...]

不变量（M1-D4 原文四条 + 契约 2 的硬约束）：
  1. 行数区间：每模式每表的行数落在预设区间内（上游突然缩水/暴涨都拦下来）
  2. calendar 覆盖 >= 14 天，且窗口必须从「今天」起算（挡上游 feed 停更）
  3. 无孤儿停站（pattern_stops.stop_id / trips.pattern_id 悬空）
  4. 每父站 >= 1 子站
  5. schema 硬约束：user_version=1 / journal_mode=delete / page_size=4096 /
     integrity_check=ok / foreign_key_check 零违规 / meta 单行且 mode 与文件名一致
  6. platform_code 下限（火车不解析站台号 -> 契约 3 /realtime 永远返 null）
  7. trips.pattern_id 全部非空且指向 stop_patterns（M0-0.6 阈值触发的 pattern 归一化）
  8. direction_groups 里不许出现非营运 headsign（'Empty Train' 等，见 build_db.NON_REVENUE_HEADSIGNS）。
     2026-09-07 真机在 Central 看到「开往 Empty Train」才发现管线漏了这一刀；加这条不变量是为了
     上游哪天冒出新的非营运词时管线**报警**，而不是又静默显示给用户。
"""
import datetime as _dt
import json
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import tripoffsets    # noqa: E402  —— trips.offsets 的唯一 Python 编解码实现
from build_db import (NON_REVENUE_HEADSIGNS, is_non_revenue_headsign,  # noqa: E402  唯一真相来源
                      service_today)

MIN_CALENDAR_DAYS = 14

# 行数区间：(下限, 上限)。基线取 2026-09-07 实测值，上下各留约 ±40%（上游换版会波动）。
RANGES = {
    'sydneytrains': {
        'stops': (400, 2000), 'routes': (30, 200), 'direction_groups': (60, 600),
        'trips': (15000, 90000), 'pattern_stops': (5000, 200000),
        'stop_patterns': (300, 8000), 'calendar': (20, 400),
    },
    'metro': {
        'stops': (40, 200), 'routes': (1, 10), 'direction_groups': (2, 40),
        'trips': (800, 8000), 'pattern_stops': (20, 5000),
        'stop_patterns': (2, 200), 'calendar': (2, 60),
    },
    # M4-4.3 基线（2026-09-22 实测，剔除校车 712 与混入的渡轮 4 之后）：
    # routes 821（700 普通巴士 793 + 714 铁路接驳 28）、stops 32,169、trips 84,076、
    # stop_patterns 5,251、pattern_stops 约 22.7 万。上下各留约 ±40%。
    'buses': {
        'stops': (20000, 60000), 'routes': (400, 2000), 'direction_groups': (500, 8000),
        'trips': (50000, 150000), 'pattern_stops': (100000, 500000),
        'stop_patterns': (2000, 15000), 'calendar': (100, 800),
    },
    # ⚠ lightrail 的 routes 下界是**刻意**卡在 4 的（L1/L2/L3/LX）。
    # 它守的不是「数据质量」，是「**两个子 feed 都进来了**」：
    # /v1/gtfs/schedule/lightrail 这个「聚合」端点其实是 innerwest 的别名，只有 L1。
    # 照它建库会得到一个 200、内容合法、其余不变量全过、golden 也不红的**缺三条线**的库。
    # 这条下界是目前唯一能让那种错变红的东西 —— 别为了「宽松一点」把它调低。
    'lightrail': {
        'stops': (60, 400), 'routes': (4, 20), 'direction_groups': (4, 100),
        'trips': (3000, 20000), 'pattern_stops': (100, 3000),
        'stop_patterns': (10, 300), 'calendar': (5, 100),
    },
}
# 有站台号的行数下界。**公交没有站台号**（上游 stops.txt 的 platform_code 全空，
# 3.2 万站一个都没有），所以它不在这张表里 —— 不是忘了写，是那个概念在公交上不存在。
MIN_PLATFORM_CODE = {'sydneytrains': 400, 'metro': 30, 'lightrail': 50}


def days_between(lo, hi):
    a = _dt.date(lo // 10000, lo // 100 % 100, lo % 100)
    b = _dt.date(hi // 10000, hi // 100 % 100, hi % 100)
    return (b - a).days + 1


def check(db_path, mode):
    fails = []
    db = sqlite3.connect('file:%s?immutable=1' % db_path.replace('\\', '/'), uri=True)
    q1 = lambda s: db.execute(s).fetchone()[0]

    # 5. schema 硬约束
    if q1('PRAGMA user_version') != 2:
        fails.append('user_version != 2')
    if q1('PRAGMA journal_mode') != 'delete':
        fails.append('journal_mode != delete')
    if q1('PRAGMA page_size') != 4096:
        fails.append('page_size != 4096')
    if q1('PRAGMA integrity_check') != 'ok':
        fails.append('integrity_check != ok')
    fk = db.execute('PRAGMA foreign_key_check').fetchall()
    if fk:
        fails.append('foreign_key_check 违规 %d 条：%s' % (len(fk), fk[:3]))
    meta = db.execute('SELECT schema_version, mode, static_version, calendar_start, calendar_end '
                      'FROM meta').fetchall()
    if len(meta) != 1:
        fails.append('meta 行数 = %d（应为 1）' % len(meta))
        db.close()
        return fails
    sv, mmode, static_version, cal_lo, cal_hi = meta[0]
    if mmode != mode:
        fails.append('meta.mode=%s 与文件名 %s 不一致' % (mmode, mode))
    if sv != 2:
        fails.append('meta.schema_version=%s' % sv)
    if not static_version:
        fails.append('meta.static_version 为空')

    # 2. calendar 覆盖 >= 14 天，**且这 14 天必须从今天算起**。
    #    只比 cal_hi - cal_lo 挡不住「上游 feed 冻结」这类故障：M0-0.5 实测 v1/metro 的静态 feed
    #    自 2024-09 起就不再更新，那种 feed 建出来的库整段日历都在过去，却仍然「覆盖 30 天」，
    #    客户端拿到手只会显示「时刻表待更新」。所以必须与当天比。
    d = days_between(cal_lo, cal_hi)
    if d < MIN_CALENDAR_DAYS:
        fails.append('calendar 只覆盖 %d 天 < %d（%d–%d）' % (d, MIN_CALENDAR_DAYS, cal_lo, cal_hi))
    # 「今天」必须是**悉尼当地日**（build_db.service_today 是唯一真相）。用 UTC 会让 cron
    # 在悉尼 02:30 跑时把上游的当日 feed 误判成「未来」—— 见 build_db 里那段注释。
    today = int(service_today().strftime('%Y%m%d'))
    if cal_lo > today:
        fails.append('calendar_start=%d 晚于今天 %d（今天没有时刻表）' % (cal_lo, today))
    ahead = days_between(today, cal_hi)
    if ahead < MIN_CALENDAR_DAYS:
        fails.append('calendar_end=%d 距今只剩 %d 天 < %d（上游 feed 停更 / 窗口算错？）'
                     % (cal_hi, ahead, MIN_CALENDAR_DAYS))

    # 1. 行数区间
    for t, (lo, hi) in RANGES.get(mode, {}).items():
        n = q1('SELECT count(*) FROM ' + t)
        if not lo <= n <= hi:
            fails.append('%s 行数 %d 越界 [%d, %d]' % (t, n, lo, hi))

    # 3. 无孤儿停站。schema 2 起 stop_times 是视图，走 pattern_stops / trips 两张真表查，
    #    否则每条都要跑一遍整表 join。
    n = q1('SELECT count(*) FROM pattern_stops ps LEFT JOIN stops x USING(stop_id) '
           'WHERE x.stop_id IS NULL')
    if n:
        fails.append('孤儿 pattern_stops（stop_id 悬空）%d 行' % n)
    n = q1('SELECT count(*) FROM pattern_stops ps LEFT JOIN stop_patterns p USING(pattern_id) '
           'WHERE p.pattern_id IS NULL')
    if n:
        fails.append('孤儿 pattern_stops（pattern_id 悬空）%d 行' % n)
    n = q1('SELECT count(*) FROM stop_patterns p LEFT JOIN pattern_stops ps USING(pattern_id) '
           'WHERE ps.pattern_id IS NULL')
    if n:
        fails.append('没有停站的 stop_patterns %d 条' % n)

    # 4. 每父站要么有子站，**要么它自己就是可停靠的站**
    #    后半句是公交逼出来的：公交零 parent_station，管线把站整体提成父站（1:1 退化），
    #    那些父站没有子站，但它们自己出现在 pattern_stops 里。
    #    不要把这条直接放宽成「允许没有子站的父站」—— 那样火车真的丢了子站也不会红。
    n = q1('SELECT count(*) FROM stops p WHERE p.location_type = 1'
           ' AND NOT EXISTS (SELECT 1 FROM stops c WHERE c.parent_station = p.stop_id)'
           ' AND NOT EXISTS (SELECT 1 FROM pattern_stops ps WHERE ps.stop_id = p.stop_id)')
    if n:
        fails.append('既没有子站、自己也不被任何 pattern 停靠的父站 %d 个' % n)
    n = q1('SELECT count(*) FROM stops c WHERE c.parent_station IS NOT NULL AND NOT EXISTS '
           '(SELECT 1 FROM stops p WHERE p.stop_id = c.parent_station)')
    if n:
        fails.append('父站缺失的子站 %d 个' % n)

    # 6. platform_code 下限
    n = q1('SELECT count(*) FROM stops WHERE platform_code IS NOT NULL')
    floor = MIN_PLATFORM_CODE.get(mode, 0)
    if n < floor:
        fails.append('platform_code 非空 %d 行 < 下限 %d（站台号没解析出来？）' % (n, floor))

    # 7. pattern 归一化确实做了
    n = q1('SELECT count(*) FROM trips WHERE pattern_id IS NULL')
    if n:
        fails.append('trips.pattern_id 为空 %d 条（pattern 归一化没跑）' % n)
    n = q1('SELECT count(*) FROM trips t LEFT JOIN stop_patterns p USING(pattern_id) '
           'WHERE p.pattern_id IS NULL')
    if n:
        fails.append('trips.pattern_id 悬空 %d 条' % n)
    npat, ntrip = q1('SELECT count(*) FROM stop_patterns'), q1('SELECT count(*) FROM trips')
    if ntrip and npat > ntrip:
        fails.append('stop_patterns(%d) > trips(%d)' % (npat, ntrip))

    # 8. 非营运 headsign 不许出现在方向列表里（按 headsign 判，口径与 build_db 同一份常量）
    bad = [(hs, n) for hs, n in db.execute(
        'SELECT headsign_pattern, count(*) FROM direction_groups GROUP BY 1')
        if is_non_revenue_headsign(hs)]
    if bad:
        fails.append('direction_groups 含非营运 headsign %d 种：%s（NON_REVENUE_HEADSIGNS 共 %d 条，'
                     '管线该在 build_db 里就丢掉这些 trip）'
                     % (len(bad), bad[:5], len(NON_REVENUE_HEADSIGNS)))
    bad_trips = sum(1 for (hs,) in db.execute('SELECT headsign FROM trips')
                    if is_non_revenue_headsign(hs))
    if bad_trips:
        fails.append('trips.headsign 含非营运标记 %d 条（共 %d 条 trip）' % (bad_trips, ntrip))

    # 9. schema 2：每趟的 offsets 必须解得开、站数对得上 pattern、末站 departure 对得上
    #    duration_secs，且同一趟内时刻单调不减。这一条是本次 schema 变更的核心防线 ——
    #    编解码不一致的失败形态是「时刻悄悄偏了几秒」，没有任何报错，只能靠这里逐趟验。
    bad_blob = []
    nonmono = []
    for tid, pid, start, dur, blob, nst in db.execute(
            'SELECT t.trip_id, t.pattern_id, t.start_secs, t.duration_secs, t.offsets, p.n_stops '
            'FROM trips t JOIN stop_patterns p USING(pattern_id)'):
        try:
            times = tripoffsets.decode(start, blob, n_stops=nst)
        except ValueError as e:
            if len(bad_blob) < 5:
                bad_blob.append((tid, str(e)))
            continue
        if times[-1][1] - start != dur:
            if len(bad_blob) < 5:
                bad_blob.append((tid, 'duration_secs=%d 但末站 departure−start=%d'
                                 % (dur, times[-1][1] - start)))
            continue
        prev = start
        for arr, dep in times:
            if dep < prev:
                if len(nonmono) < 5:
                    nonmono.append(tid)
                break
            prev = dep
    if bad_blob:
        fails.append('trips.offsets 解码失败 %d 例（只列前几条）：%s' % (len(bad_blob), bad_blob))
    if nonmono:
        fails.append('trips.offsets 时刻非单调 %d 例（只列前几条）：%s' % (len(nonmono), nonmono))

    db.close()
    return fails


def main():
    build_dir = sys.argv[1]
    modes = sys.argv[2:]
    if not modes:
        stats = os.path.join(build_dir, 'build_stats.json')
        modes = list(json.load(open(stats, encoding='utf-8'))) if os.path.exists(stats) \
            else ['sydneytrains', 'metro']
    total = 0
    for mode in modes:
        p = os.path.join(build_dir, mode + '.sqlite')
        if not os.path.exists(p):
            print('FAIL %s: 产物不存在 %s' % (mode, p))
            total += 1
            continue
        fails = check(p, mode)
        for f in fails:
            print('FAIL %s: %s' % (mode, f))
        if not fails:
            print('OK   %s: 全部不变量通过' % mode)
        total += len(fails)
    if total:
        print('不变量校验失败：%d 处 -> 不发 Release' % total)
        return 1
    print('不变量校验通过（%s）' % '/'.join(modes))
    return 0


if __name__ == '__main__':
    sys.exit(main())
