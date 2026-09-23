#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""模式定义表：上游端点、agency 过滤口径。M0-0.5 实测（见 design/m0-measurements.md）。

四处与调研假设不符，已按实测写死：
  * metro 必须走 v2（v1 的静态/实时两个 feed 都已冻结在 2024-09）
  * ferries 无聚合端点，必须用子 feed ferries/sydneyferries
  * sydneytrains feed 里混着 NSW Trains 车次（agency_id='NSWTrains'，12,743 trips / 356,921 stop_times，占 23%），
    与独立的 nswtrains feed 重叠 → 按 agency_id 过滤掉
  * **lightrail 的「聚合」静态端点是 innerwest 的别名**（2026-09-22 实测，M4-4.3）：
    `/v1/gtfs/schedule/lightrail` 与 `/v1/gtfs/schedule/lightrail/innerwest` **逐字节相同**
    （344,506 B，routes.txt 的 sha256 一致，都只有 L1、45,969 行 stop_times），
    而 L2/L3/LX 在 `/lightrail/cbdandsoutheast`（1,058,516 B、106,014 行）。
    照聚合端点建库会得到一个**只有 L1** 的轻轨库 —— 200、内容合法、不变量全过、golden 不红，
    用户只是搜不到 L2/L3。比 404 坏，故一个 mode 允许配多个 feed（见 `feeds_for`）。
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

# 一个客户端 mode 可以由**多个**上游静态 feed 合成（lightrail 就是）。
# 写 'feeds' 的按列表取，写 'path' 的按单个取 —— 两种写法由 feeds_for() 统一。
# 合并前已实测两个 lightrail 子 feed 的五个 id 空间**零交集**
# （routes/stops/trips/calendar/agency），所以直接拼接，不用重编 id。
# 若将来某个 mode 的子 feed 出现 id 冲突，必须在这里显式加前缀，不许默默 INSERT OR IGNORE。
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
    'lightrail': {
        # 不要写成 '/v1/gtfs/schedule/lightrail' —— 那个端点只是 innerwest 的别名，见模块头
        'feeds': ['/v1/gtfs/schedule/lightrail/innerwest',
                  '/v1/gtfs/schedule/lightrail/cbdandsoutheast'],
        'keep_agencies': None, 'route_type': 0,
        # newcastle（/v1/gtfs/schedule/lightrail/newcastle，80,244 B）存在但不在悉尼，M4 不收
    },
    'ferries': {'path': '/v1/gtfs/schedule/ferries/sydneyferries', 'keep_agencies': None, 'route_type': 4},
    'nswtrains': {'path': '/v1/gtfs/schedule/nswtrains', 'keep_agencies': None, 'route_type': 2},
    'buses': {
        'path': '/v1/gtfs/schedule/buses', 'keep_agencies': None, 'route_type': 3,
        # 按 route_type 剔除两类（M4-4.3）：
        #  712 校车 —— DP3 默认 A。5,074 条 route / 6,282 趟 / **1.08 趟每 pattern**，
        #      占掉一半几何 pattern 只换来 7% 的趟数，是整个库里最差的一笔；且 NSW 校车
        #      限定在校学生乘坐。剔除后 buses 库 35.7 → 28.1 MB。代价：站牌上真有校车经过时不显示，
        #      4.15 关账要在「已知限制」页写一行。
        #  4   渡轮 —— buses feed 里**混着一条 route_type=4 的渡轮 route**（188 趟），
        #      与将来独立的 ferries feed 重叠，与当年 NSWTrains 混进 sydneytrains 同形。
        'drop_route_types': [712, 4],
    },
}

# 每日发布的模式。M4 加上 buses / lightrail（schema 2 起）。
# ⚠ 这一行一旦进 data 仓 master，下一次定时 run 就会把 schema 2 + 公交/轻轨发成 stable ——
# 客户端只读 releases/latest，所以合进 master 的时刻必须与 M4 app 发版对齐，见 4.15 发版顺序。
PUBLISH_MODES = ['sydneytrains', 'metro', 'buses', 'lightrail']


def feeds_for(mode):
    """该 mode 的上游静态 **feed** 列表。单 feed 的模式返回一个元素的列表。

    命名与 Worker 侧统一：`mode` 是客户端可见的概念（用户看到「公交」「轻轨」），
    `feed` 是上游的一个 zip / 一个 protobuf 端点。**一个 mode ← 多个 feed。**
    两边同一套词，免得半年后一边叫 path 一边叫 feed 而没人知道是同一层。
    """
    cfg = MODES[mode]
    if 'feeds' in cfg:
        return list(cfg['feeds'])
    return [cfg['path']]


def drop_route_types_for(mode):
    """该 mode 要按 route_type 剔除的集合（见 MODES 里各自的理由）。"""
    return set(MODES.get(mode, {}).get('drop_route_types', ()))


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
