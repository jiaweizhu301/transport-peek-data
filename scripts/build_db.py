#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""D2 · GTFS 静态 zip -> 每模式 SQLite（契约 2 sqlite-ddl.sql v2，schema 逐字照搬到 schema.sql）。

  python build_db.py <gtfs_dir> <out_dir> [mode ...] [--days SPEC] [--no-gzip]

裁剪口径：
  * 丢 shapes / occupancies / vehicle_* / notes；保留 transfers 与父站/子站
  * 按 agency_id 过滤（sydneytrains feed 混入 NSWTrains 车次，M0-0.5 实测 23%）
  * platform_code 从 stop_name 尾部解析（sydneytrains 的 stops.txt 无此列），正则与
    fixtures/tools/build_fixtures.py 的 PLATFORM_RE 一致
  * pattern 归一化：重复停站序列去重进 stop_patterns，trips.pattern_id 引用（M0-0.6 阈值触发）
  * headsign -> direction_groups 预计算，direction_key = 去 "via ..." 的归一化目的地
  * 非营运班次按 **headsign** 整条丢弃（NON_REVENUE_HEADSIGNS，见下）—— 空车调车不是「开往哪里」
  * 服务日历窗口**按模式**裁（缺省见 gtfs_modes.MODES[mode]['days']：sydneytrains=16 / metro=120）。
    砍窗口是拿可用天数换体积，只对被体积阈值逼到的模式做：火车 30 天 = 14.63 MB 超 10 MB 阈值，
    16 天 = 9.50 MB；metro 16 天与 116 天的 trips 和体积完全相同，砍它零收益 -> 不砍。
    --days 可写 "sydneytrains=16,metro=120" 逐模式覆盖，或单个数字全局覆盖
  * VACUUM + PRAGMA user_version = 1，再 gzip -9
"""
import argparse
import csv
import datetime
import gzip
import io
import json
import os
import re
import shutil
import sqlite3
import sys
import zipfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from gtfs_modes import MODES, PUBLISH_MODES, parse_days_spec

SCHEMA = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'schema.sql')

# ---------------------------------------------------------------------------
# 非营运（空车调车 / 不载客）班次的 headsign 清单。**按 headsign 过滤，不按 route 或 agency。**
#
# 为什么必须单独有这一条：build() 里原有的「整条 trip 没有任何一站 pickup_type=0 就丢」只抓得住
# 「一站都不让上」的调车。上游 sydneytrains feed 里的 'Empty Train' **有 59,727 行 pickup_type=0**
# （13,254 条 trip 里 3,880 条至少有一站可上客），那条规则完全抓不到，于是「开往 Empty Train」
# 一路漏到了真机的方向列表上（2026-09-07 真机验收在 Central 抓到：NSN_1a / NTH_1a，17 班车）。
# 这是上游数据的坑：TfNSW 把空车调车挂在真实营运 route 上（ESI_1a / NTH_1a / NSN_1a ...），
# 只有 headsign 诚实地写着它不载客。
#
# 清单的观测来源（2026-09-07 实测：sydneytrains / nswtrains / metro / lightrail / ferries
# 五个 feed 的 trips.txt 全量 headsign 直方图）：
#   'Empty Train'       sydneytrains feed 13,254 trips（SydneyTrains 10,707 / NSWTrains 2,547）——
#                       全 feed 出现次数最多的 headsign，比任何真实目的地都多。**本次 bug 的元凶。**
#                       16 天真库里过滤前留下 27 个 direction_group / 1,943 条 trip。
#   'Does Not Pick Up'  sydneytrains feed 2 trips（NSN_2a，T1）。headsign 自己写明不上客，不是目的地。
#                       16 天真库里过滤前留下 1 个 direction_group / 2 条 trip。
#   'Charter'           sydneytrains feed 5 trips（BMT_2，agency=NSWTrains，M1 已被 agency 过滤掉）。
#                       包车不对公众开放；留在清单里是给 M4+ 启用 nswtrains 模式用的。
#   'Non Revenue'       未作为 headsign 出现，但它是 route_id='RTTA_REV' 的 route_long_name；
#   'Out Of Service'    未作为 headsign 出现，但它是 route_id='RTTA_DEF' 的 route_long_name。
#                       这两条是 TfNSW 自己的非营运词汇表（'Empty Train' 有 6,555 条就挂在 RTTA_REV 上），
#                       上游哪天把它们挪到 headsign 上，这里直接接住。
#   'Not In Service' / 'Empty Cars' / 'Empty Cars To Depot' / 'Empty'
#                       行业通用写法，2026-09-07 在上述五个 feed 里**一次都没出现**；纯防御位，不匹配零成本。
#
# 观测到但**故意不放进来**的一个：'Special'（sydneytrains feed 61 trips，16 天真库里 3 个
#   direction_group / 8 条 trip）。它的 61 条里有 42 条至少有一站可上客，其中 5 条跑在真实营运线
#   IWL_1j / IWL_2j（T2 City Circle to Campbelltown via Granville）上 —— 更像「标签含糊的真实加班车」
#   而不是调车。当成非营运会**删掉真实的发车**（出发列表里也一起消失），代价比「方向列表多一行难看的字」大。
#   「Special 不该当目的地显示」是展示层的问题，留给 UI/文案处理，不在管线删数据。
#
# 匹配规则：**大小写不敏感 + 折叠空白 + 子串包含**（见 is_non_revenue_headsign）。整条 trip 丢弃，
# 它的 stop_times 不入库，因此变空的 direction_groups / stop_patterns 也不会被写出（下面的 live_*
# 集合过滤），零悬挂。
#
# 为什么是**子串包含**而不是全等：客户端（core-gtfs 的全库不变量测试）与管线必须用同一套语义，
# 而客户端要挡的是「任何来源的库」，子串更严。2026-09-07 实测：在 5 个 feed 的全量 headsign 上
# **子串判定与全等判定命中的 trip 完全相同**（都是 13,261 条，全在 sydneytrains），所以这次对齐
# 语义**没有改变任何一条数据**；子串只是把 'Empty Train To Depot' 这类将来可能出现的变体也接住。
# 因此 'empty' 这一条实际上已经涵盖了 'empty train' / 'empty cars' / 'empty cars to depot'，
# 后三条保留是为了让词表本身可读、也让上面的注释能逐条对上观测数据。
#
# **这份常量是唯一真相**，由 export_non_revenue_headsigns() 导出成
# `non_revenue_headsigns.json`，随 fixtures 冻结、随 Release 发布，客户端读那份文件，
# 不许在 Kotlin/JS 侧另手写一份（本项目已经因为「build_fixtures 与 build_db 各写一份裁剪逻辑」栽过一次）。
NON_REVENUE_HEADSIGNS = frozenset([
    'empty train',
    'does not pick up',
    'charter',
    'non revenue',
    'out of service',
    'not in service',
    'empty cars',
    'empty cars to depot',
    'empty',
])


# 导出给客户端的文件名。fixtures/ 与 Release 资产用同一个名字，客户端两边都能读。
NON_REVENUE_HEADSIGNS_FILENAME = 'non_revenue_headsigns.json'


def normalize_headsign(headsign):
    """比较前的归一化：小写 + 折叠空白。"""
    return ' '.join((headsign or '').split()).lower()


def is_non_revenue_headsign(headsign):
    """headsign 是否是非营运标记。

    口径 = 归一化（小写 + 折叠空白）后做**子串包含**。invariants.py / build_fixtures.py /
    客户端（读 non_revenue_headsigns.json）全部复用这一份语义，别再抄第二份。
    """
    h = normalize_headsign(headsign)
    return any(w in h for w in NON_REVENUE_HEADSIGNS)


def non_revenue_headsigns_doc():
    """导出给客户端的词表文档（build_fixtures.py 与 gen_manifest.py 都调它，形状必须一致）。"""
    return {
        '_source': 'pipeline/scripts/build_db.py :: NON_REVENUE_HEADSIGNS',
        '_warning': '自动生成，**不要手改这个文件**。要加/删词请改 build_db.py 的 '
                    'NON_REVENUE_HEADSIGNS，然后重跑 fixtures/tools/build_fixtures.py'
                    '（迷你库那份）与 pipeline/scripts/gen_manifest.py（Release 那份）。'
                    'fixtures/tools/build_fixtures.py 收尾会断言这个文件与常量逐条相等。',
        'schema_version': 1,
        'generated_at': datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0)
                        .isoformat().replace('+00:00', 'Z'),
        'match': {
            'rule': 'substring_contains',
            'normalize': ['lowercase', 'collapse_whitespace'],
            'description': '把 headsign 归一化（转小写、把连续空白折叠成单个空格、去首尾空白）后，'
                           '只要 patterns 里任意一条是它的子串，就判为非营运班次。',
            'applies_to': ['trips.headsign', 'direction_groups.headsign_pattern',
                           'direction_groups.label', 'direction_groups.direction_key'],
        },
        'note': '非营运 = 空车调车 / 不载客 / 包车，不是「开往哪里」，不该出现在方向列表或出发列表里。'
                '管线在 build_db.build() 里就整条丢弃这些 trip；这份词表给客户端做第二道闸门。'
                '"Special" 故意**不在**表内：它 61 条 trip 里有 42 条有可上客站、5 条跑在真实营运线 '
                'IWL_1j / IWL_2j 上，当非营运会删掉真实发车 —— 那是展示层的措辞问题（M2 处理）。',
        'patterns': sorted(NON_REVENUE_HEADSIGNS),
    }


def export_non_revenue_headsigns(out_dir):
    """把词表写成 <out_dir>/non_revenue_headsigns.json，返回文件路径。"""
    path = os.path.join(out_dir, NON_REVENUE_HEADSIGNS_FILENAME)
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(non_revenue_headsigns_doc(), f, ensure_ascii=False, indent=1)
    return path


# 导出给客户端的解析规则文件名。fixtures/ 与 Release 资产同名。
PARSING_RULES_FILENAME = 'parsing_rules.json'


def parsing_rules_doc():
    r"""导出给客户端的解析规则（正则本体 + 口径）。

    **为什么要有这个文件**：`PLATFORM_RE` 一直是「管线一份、Kotlin 侧一份、只靠注释钉住
    改一处必须改另一处」。本项目已经在同一形状上栽过两次（`build_fixtures` 与 `build_db`
    各写一份裁剪逻辑；非营运词表的全等 vs 子串），不该留第三处。导出成文件后，
    Kotlin 侧 `Regex(读到的字符串)` 直接用 —— `` `\s` `[0-9]` `[A-Za-z]` `$`
    这些在 Python 与 Kotlin(java.util.regex) 里语义相同（P 流已确认）。

    大小写不敏感统一用**内联 `(?i)`** 表达（`regex_inline_flags` 字段），
    Python 与 Kotlin 都认，客户端不用去翻 `flags` 数组怎么映射。
    """
    return {
        '_source': 'pipeline/scripts/build_db.py :: PLATFORM_RE / normalize_name / direction_key_of',
        '_warning': '自动生成，**不要手改这个文件**。要改正则请改 build_db.py 里的常量，然后重跑 '
                    'fixtures/tools/build_fixtures.py（迷你库那份）与 '
                    'pipeline/scripts/gen_manifest.py（Release 那份）。'
                    'fixtures/tools/build_fixtures.py 与 pipeline/scripts/validate_assets.py '
                    '都会断言这个文件与常量逐字相等。',
        'schema_version': 1,
        'generated_at': datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0)
                        .isoformat().replace('+00:00', 'Z'),
        'rules': {
            'platform_code': {
                'regex': PLATFORM_RE.pattern,
                'flags': ['IGNORE_CASE'],
                'regex_inline_flags': '(?i)' + PLATFORM_RE.pattern,
                'capture_group': 1,
                'input': 'stops.stop_name',
                'output': 'stops.platform_code',
                'applies_to_modes': ['sydneytrains'],
                'description': '站台号只藏在子站名尾巴上（"Hornsby Station Platform 3" / '
                               '"Barangaroo Station, Platform 1"）。取第 1 个捕获组。'
                               '匹配不上就是 null（父站没有站台号是正确的）。',
                'why': 'sydneytrains 上游 stops.txt **根本没有 platform_code 列**（2026-09-07 实测：'
                       '1,214 行 stops，0 行有站台号），而契约 3 的 /realtime 要回 platform_code，'
                       '所以管线必须自己从站名解析，否则火车永远返 null。',
                'metro_note': 'metro 上游 stops.txt **有** platform_code 列（63 行里 42 行非空），'
                              '管线直接用上游给的值，正则只在该列缺失/为空时兜底 —— '
                              '见 build_db.platform_of()。所以 applies_to_modes 只写 sydneytrains。',
                'already_materialized': True,
                'materialized_note': '库里的 stops.platform_code 已经是解析好的结果，'
                                     '客户端**读库时不需要再跑这个正则**。导出它是为了让客户端的'
                                     '单测能验「管线解析口径」，以及将来客户端要自己解析别的来源时对齐。',
            },
            'name_normalized': {
                'steps': [
                    {'op': 'lowercase'},
                    {'op': 'replace_regex', 'regex': PUNCT_RE.pattern, 'replacement': ' ',
                     'note': '非 [a-z0-9] 的连续片段全部换成单个空格'},
                    {'op': 'replace_regex', 'regex': STATION_WORD_RE.pattern, 'replacement': ' ',
                     'note': '去掉独立的 "station" 一词'},
                    {'op': 'collapse_whitespace'},
                ],
                'input': 'stops.stop_name',
                'output': 'stops.name_normalized',
                'example': {'in': 'Barangaroo Station, Platform 1', 'out': 'barangaroo platform 1'},
                'description': '前缀搜索用的归一化名。查询是 name_normalized LIKE "<prefix>%"。',
                'already_materialized': True,
                'materialized_note': '**但客户端必须用同一套步骤归一化用户输入的前缀**，'
                                     '否则输入 "St. Leonards" 永远搜不到 —— 这是这个文件里'
                                     '唯一一条客户端在运行时真的要执行的规则。',
            },
            'direction_key': {
                'steps': [
                    {'op': 'replace_regex', 'regex': VIA_RE.pattern, 'replacement': '',
                     'flags': ['IGNORE_CASE'], 'regex_inline_flags': '(?i)' + VIA_RE.pattern,
                     'note': '去掉 " via ..." 及其后全部内容'},
                    {'op': 'lowercase'},
                    {'op': 'replace_regex', 'regex': PUNCT_RE.pattern, 'replacement': ' '},
                    {'op': 'collapse_whitespace'},
                    {'op': 'fallback_if_empty', 'value': 'unknown'},
                ],
                'input': 'trips.headsign',
                'output': 'direction_groups.direction_key',
                'example': {'in': 'Hornsby via Strathfield', 'out': 'hornsby'},
                'description': '方向归并键 = 去掉 "via ..." 的归一化目的地。'
                               '"Hornsby" 与 "Hornsby via Strathfield" 归成同一个方向。',
                'already_materialized': True,
                'materialized_note': '库里的 direction_groups.direction_key 已算好，客户端读库即可。',
            },
        },
        'note': '三条规则的**唯一真相**都在 pipeline/scripts/build_db.py。'
                'platform_code / name_normalized / direction_key 三个字段在 SQLite 里都已物化，'
                '客户端读库时唯一需要真正执行的是 name_normalized（用来归一化搜索前缀）。',
    }


def export_parsing_rules(out_dir):
    """把解析规则写成 <out_dir>/parsing_rules.json，返回文件路径。"""
    path = os.path.join(out_dir, PARSING_RULES_FILENAME)
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(parsing_rules_doc(), f, ensure_ascii=False, indent=1)
    return path


# 站台号只藏在子站名里（"Central Station Platform 16" / "Barangaroo Station, Platform 1"）。
# fixtures/tools/build_fixtures.py 从这里 import（不再各存一份）；客户端侧由
# export_parsing_rules() 导出成 parsing_rules.json，Kotlin 读那个字符串建 Regex，
# **不许再手抄一份**。
PLATFORM_RE = re.compile(r'\bPlatform\s+([0-9]+[A-Za-z]?)\s*$', re.I)
VIA_RE = re.compile(r'\s+via\s+.*$', re.I)
PUNCT_RE = re.compile(r'[^a-z0-9]+')
STATION_WORD_RE = re.compile(r'\bstation\b')
WEEKDAY_COL = ['monday', 'tuesday', 'wednesday', 'thursday', 'friday', 'saturday', 'sunday']


def platform_of(name, existing):
    if existing:
        return existing
    m = PLATFORM_RE.search(name or '')
    return m.group(1) if m else None


def normalize_name(name):
    """小写、去标点/多余空白、去 "Station"，供 LIKE 'x%' 前缀搜索。

    "Barangaroo Station, Platform 1" -> "barangaroo platform 1"
    """
    s = (name or '').lower()
    s = PUNCT_RE.sub(' ', s)
    s = STATION_WORD_RE.sub(' ', s)
    return ' '.join(s.split())


def direction_key_of(headsign):
    """稳定键 = 归一化目的地（小写、去 "via ..."、折叠空白）。"Hornsby via Strathfield" -> "hornsby" """
    s = VIA_RE.sub('', headsign or '').lower()
    s = PUNCT_RE.sub(' ', s)
    s = ' '.join(s.split())
    return s or 'unknown'


def secs(hms):
    """GTFS "HH:MM:SS" -> 运营日秒数（可 > 86400）。"""
    if not hms:
        return None
    p = hms.strip().split(':')
    return int(p[0]) * 3600 + int(p[1]) * 60 + int(p[2])


def ymd(s):
    return int(s) if s else 0


class Feed(object):
    """按需读 zip 里的某个 txt；不存在返回空迭代（上游 sydneytrains 无 transfers/calendar_dates）。"""

    def __init__(self, path):
        self.z = zipfile.ZipFile(path)
        self.names = set(self.z.namelist())

    def rows(self, name):
        if name not in self.names:
            return iter(())
        return csv.DictReader(io.TextIOWrapper(self.z.open(name), encoding='utf-8-sig'))


def service_days(cal_rows, cd_rows, day):
    d = datetime.date(day // 10000, day // 100 % 100, day % 100)
    col = WEEKDAY_COL[d.weekday()]
    out = {c['service_id'] for c in cal_rows
           if c[col] == '1' and ymd(c['start_date']) <= day <= ymd(c['end_date'])}
    for c in cd_rows:
        if ymd(c['date']) == day:
            if c['exception_type'] == '1':
                out.add(c['service_id'])
            else:
                out.discard(c['service_id'])
    return out


def build(zip_path, mode, static_version, out_path, horizon_days, today=None):
    cfg = MODES[mode]
    keep_agencies = set(cfg['keep_agencies']) if cfg['keep_agencies'] else None
    f = Feed(zip_path)

    # ---- routes（agency 过滤）
    routes = []
    keep_routes = set()
    for r in f.rows('routes.txt'):
        if keep_agencies and r.get('agency_id') not in keep_agencies:
            continue
        keep_routes.add(r['route_id'])
        routes.append((r['route_id'], r.get('agency_id') or '', r.get('route_short_name') or '',
                       r.get('route_long_name') or None, int(r['route_type']),
                       (r.get('route_color') or '').strip() or None,
                       (r.get('route_text_color') or '').strip() or None))

    # ---- calendar（窗口裁剪）
    cal_rows = list(f.rows('calendar.txt'))
    cd_rows = list(f.rows('calendar_dates.txt'))
    if today is None:
        today = datetime.datetime.now(datetime.timezone.utc).date()
    starts = [ymd(c['start_date']) for c in cal_rows] + \
             [ymd(c['date']) for c in cd_rows if c['exception_type'] == '1']
    ends = [ymd(c['end_date']) for c in cal_rows] + \
           [ymd(c['date']) for c in cd_rows if c['exception_type'] == '1']
    feed_lo, feed_hi = (min(starts) if starts else 0), (max(ends) if ends else 0)
    lo_d = max(feed_lo, int(today.strftime('%Y%m%d')))
    days = []
    d0 = datetime.date(lo_d // 10000, lo_d // 100 % 100, lo_d % 100)
    for i in range(horizon_days):
        v = int((d0 + datetime.timedelta(days=i)).strftime('%Y%m%d'))
        if v > feed_hi:
            break
        days.append(v)
    if not days:                      # 上游整包都在过去（不该发生）-> 退回整包
        days = [feed_lo, feed_hi]
    cal_lo, cal_hi = days[0], days[-1]
    keep_services = set()
    for day in days:
        keep_services |= service_days(cal_rows, cd_rows, day)

    calendar = [(c['service_id'], int(c['monday']), int(c['tuesday']), int(c['wednesday']),
                 int(c['thursday']), int(c['friday']), int(c['saturday']), int(c['sunday']),
                 max(ymd(c['start_date']), cal_lo), min(ymd(c['end_date']), cal_hi))
                for c in cal_rows if c['service_id'] in keep_services]
    calendar_dates = [(c['service_id'], ymd(c['date']), int(c['exception_type']))
                      for c in cd_rows
                      if c['service_id'] in keep_services and cal_lo <= ymd(c['date']) <= cal_hi]

    # ---- trips（route + service 过滤）+ direction_groups
    dg_id = {}
    dgs = []
    trips = {}
    dropped_nonrevenue_headsign = 0
    for t in f.rows('trips.txt'):
        if t['route_id'] not in keep_routes or t['service_id'] not in keep_services:
            continue
        hs = (t.get('trip_headsign') or '').strip() or 'unknown'
        # 非营运班次（'Empty Train' 等，见 NON_REVENUE_HEADSIGNS）：headsign 说明它不载客。
        # 在这里丢掉 -> 既不会建 direction_group，stop_times 也进不来（下面按 trips 过滤），零悬挂。
        if is_non_revenue_headsign(hs):
            dropped_nonrevenue_headsign += 1
            continue
        k = (t['route_id'], hs)
        if k not in dg_id:
            dg_id[k] = len(dgs) + 1
            dgs.append((dg_id[k], t['route_id'], direction_key_of(hs), hs, hs))
        di = t.get('direction_id')
        trips[t['trip_id']] = [t['trip_id'], t['route_id'], t['service_id'], hs,
                               int(di) if di not in (None, '') else None, dg_id[k], None]

    # ---- stops（丢出入口/通用节点；解析 platform_code）
    stops = {}
    for s in f.rows('stops.txt'):
        lt = int(s.get('location_type') or 0)
        if lt not in (0, 1):
            continue
        name = s.get('stop_name') or ''
        stops[s['stop_id']] = (s['stop_id'], name, normalize_name(name),
                               float(s['stop_lat']), float(s['stop_lon']), lt,
                               (s.get('parent_station') or '').strip() or None,
                               platform_of(name, (s.get('platform_code') or '').strip() or None))

    # ---- 建库（索引留到灌完数据再建）
    db_tmp = out_path + '.tmp'
    if os.path.exists(db_tmp):
        os.remove(db_tmp)
    db = sqlite3.connect(db_tmp)
    ddl = open(SCHEMA, encoding='utf-8').read()
    index_stmts = re.findall(r'^CREATE INDEX .*?;', ddl, re.M)
    db.executescript(re.sub(r'^CREATE INDEX .*?;', '', ddl, flags=re.M))
    db.execute('PRAGMA synchronous = OFF')
    db.execute('PRAGMA journal_mode = MEMORY')

    # ---- stop_times（流式；只留保留下来的 trip；同时抽停站序列做 pattern 归一化）
    pattern_id = {}
    patterns = []
    ins = 'INSERT OR IGNORE INTO stop_times VALUES (?,?,?,?,?,?,?)'

    dropped_nonrevenue = [0]

    def flush(tid, rows):
        if not rows:
            return
        # 非营运班次：整条 trip 没有任何一站可上客（RTTA_REV/RTTA_DEF 空车与调车）-> 整条丢
        if not any(r[5] == 0 for r in rows):
            dropped_nonrevenue[0] += 1
            return
        rows.sort(key=lambda r: r[1])
        db.executemany(ins, rows)
        route_id = trips[tid][1]
        key = (route_id, tuple(r[2] for r in rows))
        pid = pattern_id.get(key)
        if pid is None:
            pid = len(patterns) + 1
            pattern_id[key] = pid
            patterns.append((pid, route_id, json.dumps(list(key[1]), separators=(',', ':'))))
        trips[tid][6] = pid

    cur_tid, buf = None, []
    orphan_stop = 0
    dropped_passthrough = 0
    for r in f.rows('stop_times.txt'):
        tid = r['trip_id']
        if tid not in trips:
            continue
        sid = r['stop_id']
        if sid not in stops:
            orphan_stop += 1
            continue
        pu, do = int(r.get('pickup_type') or 0), int(r.get('drop_off_type') or 0)
        # 「通过不停靠」：既不上客也不下客（火车飞站，M0 数据里占 21%）。出发/到达列表都用不到，丢
        if pu == 1 and do == 1:
            dropped_passthrough += 1
            continue
        dep = secs(r.get('departure_time') or r.get('arrival_time'))
        arr = secs(r.get('arrival_time') or r.get('departure_time'))
        row = (tid, int(r['stop_sequence']), sid, arr, dep, pu, do)
        if tid != cur_tid:
            if cur_tid is not None:
                flush(cur_tid, buf)
            cur_tid, buf = tid, []
        buf.append(row)
    if cur_tid is not None:
        flush(cur_tid, buf)

    # 无停站的 trip 一律丢弃（会造成 direction_groups 悬挂，也没有业务意义）
    for tid in [t for t, v in trips.items() if v[6] is None]:
        del trips[tid]

    used_stops = {r[0] for r in db.execute('SELECT DISTINCT stop_id FROM stop_times')}
    keep_stops = set(used_stops)
    for sid in list(used_stops):
        p = stops[sid][6] if sid in stops else None
        if p:
            keep_stops.add(p)
    # 保留被保留父站的全部子站（客户端「站台待定」与 stop_parents 需要完整子站集）
    for sid, s in stops.items():
        if s[6] in keep_stops:
            keep_stops.add(sid)

    live_routes = {t[1] for t in trips.values()}
    live_services = {t[2] for t in trips.values()}
    live_dg = {t[5] for t in trips.values()}
    live_pat = {t[6] for t in trips.values()}

    db.executemany('INSERT INTO stops VALUES (?,?,?,?,?,?,?,?)',
                   [stops[s] for s in sorted(keep_stops) if s in stops])
    db.executemany('INSERT INTO routes VALUES (?,?,?,?,?,?,?)',
                   [r for r in routes if r[0] in live_routes])
    db.executemany('INSERT INTO direction_groups VALUES (?,?,?,?,?)',
                   [g for g in dgs if g[0] in live_dg])
    db.executemany('INSERT INTO stop_patterns VALUES (?,?,?)',
                   [p for p in patterns if p[0] in live_pat])
    db.executemany('INSERT INTO trips VALUES (?,?,?,?,?,?,?)', list(trips.values()))
    db.executemany('INSERT INTO calendar VALUES (?,?,?,?,?,?,?,?,?,?)',
                   [c for c in calendar if c[0] in live_services])
    db.executemany('INSERT INTO calendar_dates VALUES (?,?,?)',
                   [c for c in calendar_dates if c[0] in live_services])

    # transfers：只留两端都在库里的（sydneytrains 上游无 transfers.txt）
    tf = [(r['from_stop_id'], r['to_stop_id'], int(r.get('transfer_type') or 0),
           int(r['min_transfer_time']) if (r.get('min_transfer_time') or '').strip() else None)
          for r in f.rows('transfers.txt')]
    db.executemany('INSERT OR IGNORE INTO transfers VALUES (?,?,?,?)',
                   [r for r in tf if r[0] in keep_stops and r[1] in keep_stops])

    db.execute('INSERT INTO meta VALUES (1,?,?,?,?,?,?)', (
        1, mode, static_version, cal_lo, cal_hi,
        datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0)
        .isoformat().replace('+00:00', 'Z')))
    for stmt in index_stmts:
        db.execute(stmt)
    db.commit()
    db.execute('PRAGMA journal_mode = DELETE')
    db.execute('PRAGMA user_version = 1')
    db.commit()
    db.execute('VACUUM')
    db.commit()

    counts = {t: db.execute('SELECT count(*) FROM ' + t).fetchone()[0]
              for t in ('stops', 'routes', 'direction_groups', 'trips', 'stop_times',
                        'stop_patterns', 'calendar', 'calendar_dates', 'transfers')}
    plat = db.execute('SELECT count(*) FROM stops WHERE platform_code IS NOT NULL').fetchone()[0]
    db.close()
    if os.path.exists(out_path):
        os.remove(out_path)
    os.rename(db_tmp, out_path)
    return dict(mode=mode, static_version=static_version, calendar_start=cal_lo,
                calendar_end=cal_hi, calendar_days=len(days), platform_code_rows=plat,
                orphan_stop_time_rows=orphan_stop,
                dropped_passthrough_rows=dropped_passthrough,
                dropped_nonrevenue_trips=dropped_nonrevenue[0],
                dropped_nonrevenue_headsign_trips=dropped_nonrevenue_headsign,
                sqlite_bytes=os.path.getsize(out_path), **counts)


def gzip_file(path):
    gz = path + '.gz'
    with open(path, 'rb') as fi, gzip.GzipFile(gz, 'wb', compresslevel=9, mtime=0) as fo:
        shutil.copyfileobj(fi, fo, 1 << 20)
    return gz


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('gtfs_dir')
    ap.add_argument('out_dir')
    ap.add_argument('modes', nargs='*')
    ap.add_argument('--days', default='',
                    help='服务日历窗口天数。留空 = 每模式各自的缺省（gtfs_modes.MODES[mode]["days"]，'
                         '当前 sydneytrains=16 / metro=120）；'
                         '"sydneytrains=16,metro=120" = 逐模式覆盖；"16" = 全部模式统一覆盖。'
                         '不变量下限 14')
    ap.add_argument('--no-gzip', action='store_true')
    ap.add_argument('--today', default=None,
                    help='把「今天」钉死成 YYYYMMDD（黄金样本可复现，日常跑不要传）')
    a = ap.parse_args()
    os.makedirs(a.out_dir, exist_ok=True)
    modes = a.modes or PUBLISH_MODES
    days_by_mode = parse_days_spec(a.days, modes)
    out = {}
    for mode in modes:
        vf = os.path.join(a.gtfs_dir, mode + '.version')
        sv = open(vf, encoding='utf-8').read().strip() if os.path.exists(vf) else \
            datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%d-%H%M')
        st = build(os.path.join(a.gtfs_dir, mode + '.zip'), mode, sv,
                   os.path.join(a.out_dir, mode + '.sqlite'), days_by_mode[mode],
                   datetime.datetime.strptime(a.today, '%Y%m%d').date() if a.today else None)
        if not a.no_gzip:
            st['gz_bytes'] = os.path.getsize(gzip_file(os.path.join(a.out_dir, mode + '.sqlite')))
        out[mode] = st
        print('%-13s sqlite %.1f MB  gz %.2f MB  trips=%d patterns=%d stop_times=%d '
              'stops=%d platform_code=%d cal=%d-%d(%dd)'
              % (mode, st['sqlite_bytes'] / 1e6, st.get('gz_bytes', 0) / 1e6, st['trips'],
                 st['stop_patterns'], st['stop_times'], st['stops'], st['platform_code_rows'],
                 st['calendar_start'], st['calendar_end'], st['calendar_days']), flush=True)
    with open(os.path.join(a.out_dir, 'build_stats.json'), 'w', encoding='utf-8') as fo:
        json.dump(out, fo, ensure_ascii=False, indent=1)


if __name__ == '__main__':
    main()
