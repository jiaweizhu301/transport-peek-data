#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""模式定义表：上游端点、agency 过滤口径。M0-0.5 实测（见 design/m0-measurements.md）。

三处与调研假设不符，已按实测写死：
  * metro 必须走 v2（v1 的静态/实时两个 feed 都已冻结在 2024-09）
  * ferries 无聚合端点，必须用子 feed ferries/sydneyferries
  * sydneytrains feed 里混着 NSW Trains 车次（agency_id='NSWTrains'，12,743 trips / 356,921 stop_times，占 23%），
    与独立的 nswtrains feed 重叠 → 按 agency_id 过滤掉
"""

API_BASE = 'https://api.transport.nsw.gov.au'

# 服务日历窗口（天）。**按模式给值**，不是全局一刀切：
#   砍窗口是拿「时刻表可用天数」换体积，只有被体积阈值逼到的模式才该砍。
#   实测（2026-09-07，challenger 复核）：
#     * sydneytrains：30 天 = 14.63 MB / 21 天 = 11.58 MB 都超 M1-D2 的 10 MB 阈值，
#       16 天 = 9.50 MB 是唯一能达标的路径（冻结的 DDL 下 pattern 归一化去重不了时刻行）。
#     * metro：窗口 16 天与 116 天的 trips（1,749）与 gz（0.58 MB）**完全相同** —— 砍它零收益纯亏，
#       故放到 120 天（上游 calendar 到 20261231，实际会被 feed 末尾截断）。
#   未发布的模式先给 DEFAULT_DAYS，等 M4+ 真跑出体积再按同样口径调。
DEFAULT_DAYS = 120
# 不变量 invariants.py 的下限；任何模式的窗口都不得低于它
MIN_DAYS = 14

MODES = {
    'sydneytrains': {
        'path': '/v1/gtfs/schedule/sydneytrains',
        # sydneytrains feed 混入 NSWTrains，只保留自家 agency
        'keep_agencies': ['SydneyTrains'],
        'route_type': 2,
        'days': 16,                     # 体积逼的，见上
    },
    'metro': {
        'path': '/v2/gtfs/schedule/metro',
        'keep_agencies': None,          # 单 agency（SMNW），不过滤
        'route_type': 1,
        'days': 120,                    # 砍窗口对 metro 零收益
    },
    # 以下 M1 不发布，仅保留端点定义（M4+ 启用）
    'lightrail': {'path': '/v1/gtfs/schedule/lightrail', 'keep_agencies': None, 'route_type': 0},
    'ferries': {'path': '/v1/gtfs/schedule/ferries/sydneyferries', 'keep_agencies': None, 'route_type': 4},
    'nswtrains': {'path': '/v1/gtfs/schedule/nswtrains', 'keep_agencies': None, 'route_type': 2},
    'buses': {'path': '/v1/gtfs/schedule/buses', 'keep_agencies': None, 'route_type': 3},
}

# M1 每日发布的模式
PUBLISH_MODES = ['sydneytrains', 'metro']


def days_for(mode):
    """该模式的缺省日历窗口天数。新增模式不写 'days' 就吃 DEFAULT_DAYS，不用改代码形状。"""
    return MODES.get(mode, {}).get('days', DEFAULT_DAYS)


def parse_days_spec(spec, modes):
    """把 --days 的取值解析成 {mode: days}。

    接受三种写法（都可省略，省略即全用每模式缺省）：
      * ''            -> 全用 days_for(mode)
      * '16'          -> 所有模式都用 16（全局覆盖，应急用）
      * 'sydneytrains=16,metro=120'  -> 逐模式覆盖，没写到的模式仍用缺省
    """
    out = {m: days_for(m) for m in modes}
    spec = (spec or '').strip()
    if not spec:
        return out
    if '=' not in spec:
        n = int(spec)
        return {m: n for m in modes}
    for part in spec.replace(';', ',').split(','):
        part = part.strip()
        if not part:
            continue
        mode, _, n = part.partition('=')
        mode = mode.strip()
        if mode not in MODES:
            raise SystemExit('FATAL: --days 里的模式名 %r 不认识（可用：%s）'
                             % (mode, '/'.join(MODES)))
        out[mode] = int(n)
    return out
