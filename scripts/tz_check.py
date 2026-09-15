#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""D4 · 「今天」口径回归测试（定点复现 2026-09-11~09-14 连挂 4 天的那个 bug）。

  python scripts/tz_check.py

只测一件事：管线的「今天」必须是**悉尼当地日**，不是 UTC 日。
不用真跑管线 —— 直接把 UTC 时刻注入 build_db.sydney_now()，断言日期。
同时对拍 tzdata 路径与无 tzdata 的明文 DST 回退，保证两条路径不会分叉。
"""
import datetime
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import build_db


def utc(s):
    return datetime.datetime.strptime(s, '%Y-%m-%dT%H:%MZ').replace(tzinfo=datetime.timezone.utc)


# (UTC 时刻, 期望的悉尼日历日, 期望偏移小时)
CASES = [
    # ↓ 就是挂掉的那一刻：cron `30 16 * * *` 在 UTC 09-14 触发，悉尼已经是 09-15。
    #   用 UTC 算「今天」= 20260914，上游 feed 的 calendar_start=20260915 就成了「未来」。
    ('2026-09-14T16:30Z', '2026-09-15', 10),
    ('2026-09-14T20:25Z', '2026-09-15', 10),   # 实际那次运行的时刻（Actions 延迟后）
    ('2026-09-11T13:00Z', '2026-09-11', 10),   # 手动 dispatch 躲过 bug 的时刻
    ('2026-09-15T13:59Z', '2026-09-15', 10),   # 悉尼 23:59，与 UTC 同日
    ('2026-09-15T14:00Z', '2026-09-16', 10),   # 悉尼 00:00，翻页
    # DST：AEDT 从 10 月第一个周日 02:00 起（2026 = 10-04），4 月第一个周日 03:00 止（2026 = 04-05）
    ('2026-10-03T15:59Z', '2026-10-04', 10),
    ('2026-10-03T16:00Z', '2026-10-04', 11),
    ('2026-04-04T15:59Z', '2026-04-05', 11),
    ('2026-04-04T16:00Z', '2026-04-05', 10),
    ('2026-12-31T13:00Z', '2027-01-01', 11),   # AEDT 下的跨年翻页
]


def main():
    fails = []
    for ts, want_day, want_off in CASES:
        t = utc(ts)
        got = build_db.sydney_now(t)
        if got.strftime('%Y-%m-%d') != want_day:
            fails.append('%s -> 悉尼日 %s，期望 %s' % (ts, got.strftime('%Y-%m-%d'), want_day))
        off = int(got.utcoffset().total_seconds() // 3600)
        if off != want_off:
            fails.append('%s -> 偏移 UTC+%d，期望 UTC+%d' % (ts, off, want_off))
        # 无 tzdata 的回退路径必须与 tzdata 一致，否则哪天 runner 少了 tzdata 会静默分叉
        fb = build_db._sydney_offset_fallback(t)
        if fb != want_off:
            fails.append('%s -> 回退路径偏移 UTC+%d，期望 UTC+%d' % (ts, fb, want_off))

    # service_today() 的钉死开关（黄金样本 / 复现用）
    os.environ['PIPELINE_TODAY'] = '20260914'
    if build_db.service_today().strftime('%Y%m%d') != '20260914':
        fails.append('PIPELINE_TODAY 没有生效')
    del os.environ['PIPELINE_TODAY']

    for f in fails:
        print('FAIL ' + f)
    if fails:
        print('「今天」口径校验失败：%d 处' % len(fails))
        return 1
    print('OK   「今天」口径：%d 个定点全部按悉尼当地日（含 DST 两个切换点）' % len(CASES))
    return 0


if __name__ == '__main__':
    sys.exit(main())
