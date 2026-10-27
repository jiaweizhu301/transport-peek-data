#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""XBE-02 · STOP_OPS（公交站 -> 实时分运营商子 feed）与有效站表的唯一真相。

  python stop_ops.py <buses.sqlite>   # 打印 worker/src/stop-ops.gen.js 的全文（与 worker/scripts/gen-stop-ops.py 逐字节相同）
  python stop_ops.py --selftest
  python stop_ops.py --observe <observed.json> <feed.pb[.gz]>…   # 已落盘的整份公交 TripUpdates 并进滚动文件
  python stop_ops.py --observe-live <observed.json>               # CI：拉一次整份公交 feed 并进（observe-bus-ops.yml）

TfNSW 的 v1/gtfs/realtime/buses/<OperatorID> 是整份 buses TripUpdates 按合同拆开的子 feed
（GSBC009 ≈ 250 KB，整份 ≈ 1.5 MB）。Worker 只取热站所属运营商的子 feed，CPU 降一个量级。

映射链：stop_id -(pattern_stops/routes)-> agency_id -(AGENCY_OP)-> OperatorID。
agency_id = route_id 的「_」前缀（TfNSW Realtime Bus Technical Doc v4.4：route_id =
"(CONTRACT ID)_(ROUTE ID)"）。AGENCY_OP 是 2026-10-10 拉全部子 feed 实测对出来的：每个子 feed 里
route_id 前缀唯一、两两不重叠，且与整份 feed 的同前缀 trip 一致。

**不在 AGENCY_OP 里的 agency 一律记 '*'**（Worker 回落取整份 feed）：
  2606/2607 中央海岸（OMBSC006/007，子 feed 404）、7xxx 换乘接驳巴士（只在整份 feed 里）、
  2454/2455/2457（OSMBSC008/009/011，子 feed 当前为空，无法验证）。

编码：站号是纯数字，按数值排序后把相邻同映射的站并成段（跨号段空洞也并：空洞里的新站按
邻居算，邻居同合同区）。runs = 「与上一段起点的差(36 进制).集合下标」以 ';' 连接。
超出 [min, max] 的站号 -> '*'。解码见 worker/src/busops.js（集合下标进 Uint8Array -> sets ≤ 256）。

M7.5 XBE-02：此前只有 worker 的脚本在部署时生成一次（冻结 7 天就有 98 站缺运营商）。现在由
gen_manifest.py 每天把同一份 payload 发成 Release 资产 `stop_ops.json`，再加每个 mode 的
`valid_stops.<mode>.json`（客户端可请求的父站 id 全集，WP-06 拿它挡 BE-02 的垃圾站号）。
两份都**不进 manifest**（manifest.schema.json 是 additionalProperties=false 的冻结契约）：
URL 与 stop_parents_url_template 同源，`<base>/stop_ops.json`、`<base>/valid_stops.<mode>.json`。
"""
import collections
import json
import os
import sqlite3
import sys

STOP_OPS_FILENAME = 'stop_ops.json'
STOP_OPS_MODE = 'buses'
MAX_SETS = 256  # busops.js 把集合下标放进 Uint8Array

AGENCY_OP = {
    '2447': 'OSMBSC001', '2448': 'OSMBSC002', '2449': 'OSMBSC003', '2450': 'OSMBSC004',
    '2456': 'OSMBSC010', '2458': 'OSMBSC012', '2459': 'SBSC006', '3000': 'NISC001',
    '2501': 'GSBC001', '2502': 'GSBC002', '2503': 'GSBC003', '2504': 'GSBC004',
    '2507': 'GSBC007', '2508': 'GSBC008', '2509': 'GSBC009', '2510': 'GSBC010', '2514': 'GSBC014',
}


def valid_stops_filename(mode):
    return 'valid_stops.%s.json' % mode


def _open(path):
    return sqlite3.connect('file:%s?immutable=1' % path.replace(chr(92), '/'), uri=True)


def b36(n):
    s = ''
    while True:
        n, r = divmod(n, 36)
        s = '0123456789abcdefghijklmnopqrstuvwxyz'[r] + s
        if not n:
            return s


def stop_agencies(db):
    """stop_id -> {OperatorID 或 '*'}，只含有车停的站（pattern_stops）。"""
    m = collections.defaultdict(set)
    for s, a in db.execute(
            'SELECT DISTINCT ps.stop_id, r.agency_id FROM pattern_stops ps'
            ' JOIN stop_patterns p USING (pattern_id) JOIN routes r USING (route_id)'):
        m[s].add(AGENCY_OP.get(a, '*'))
    return m


# ---------------------------------------------------------------- 实时观测到的「站 -> 运营商」（M7.5 WP-06 B2，§9.9 stop_ops_miss）
#
# 静态 pattern_stops 列不出「只在实时里有」的线路（临时 / 仅实时线路，例：2026-10-16 的 2507_722w 在 2150116、
# 2503_9049 在 216411）。B2 的 Worker 只拉 stop_ops 列出的运营商子 feed，所以这些行就没了。修法在这里、不在 Worker：
# 把实时整份公交 TripUpdates 里**观测到**的 (stop_id, agency) 并进来。
#
# 生产输入 = 仓里提交的滚动文件 OBSERVED_PATH（{stop_id: {agency_id: 最近一次见到的悉尼日}}），由
# .github/workflows/observe-bus-ops.yml 白天定时拉一次整份公交 feed（每次 1 个 TfNSW 请求）用 --observe-live 并入、提交。
# 记 agency（route_id 前缀）而不是 OperatorID：AGENCY_OP 改了，下次建库自动跟着改。
# 建库时只取「最近一次见到」在 static_version 日期前 OBSERVED_WINDOW_DAYS 天内的对（窗口锚在库的 static_version 上，
# 不是墙钟 —— validate_assets 重算出逐字节相同的 payload）。
#
# 规则（保持 '*' 语义）：
#   * 已是 '*' 的站不变（'*' 吸收一切）。
#   * 只并 AGENCY_OP 里有的 agency：观测到一个未知 agency **不**把站变成 '*'。否则 BUS_REST=off 时该站从
#     「列出的运营商有实时」退成「全部只有时刻表」—— 为了几趟仅实时车把整站的实时都丢掉。未知 agency 只计数告警。
#   * 只并库里存在的父站（location_type=1）：库外的站号客户端请求不到（valid_stops 会挡），并进来只会撑大 min/max。
# 多列一个运营商只是 Worker 多拉一份子 feed（行仍按站过滤），少列才丢行 —— 所以只并不减，过期靠窗口。

OBSERVED_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                             'observed', 'bus_stop_agencies.json')
OBSERVED_WINDOW_DAYS = 35
# 只收客户端**显示得出来**的 trip：ADDED(1)（DepartureMerger 规则 5：时刻表里没有也插入）与 REPLACEMENT(5)
# （trip_id 就是静态 id）。SCHEDULED(0) 的仅实时 trip 不收：它的 trip_id 不在客户端静态库里，DepartureMerger
# 只把 SCHEDULED 行挂到已有班次上，挂不上就丢 —— 列它的运营商只多花 Worker 的子 feed，不多显示一行。
# 2026-10-16 peak 75 份快照实测：全收 = +1,696 个 (站, 运营商)，几乎全是 DP3 故意裁掉的校车（712）路线，
# 包括 §9.9 举的 2507_722w / 2503_9049（都是 SCHEDULED、静态库里无此 trip）；只收 ADDED/REPLACEMENT = +8 个。
OBSERVE_TRIP_RELS = frozenset({1, 5})
RT_BUSES_URL = 'https://api.transport.nsw.gov.au/v1/gtfs/realtime/buses'


def _varint(b, i):
    r = s = 0
    while True:
        x = b[i]
        i += 1
        r |= (x & 0x7f) << s
        if x < 0x80:
            return r, i
        s += 7


def _fields(b):
    """protobuf 线格式：逐个 (字段号, 值)；varint -> int，LEN -> memoryview。用不到的定长类型照规则跳过。"""
    i, n = 0, len(b)
    while i < n:
        key, i = _varint(b, i)
        f, wt = key >> 3, key & 7
        if wt == 0:
            v, i = _varint(b, i)
        elif wt == 2:
            ln, i = _varint(b, i)
            v = b[i:i + ln]
            i += ln
        elif wt == 1:
            v = None
            i += 8
        elif wt == 5:
            v = None
            i += 4
        else:
            raise ValueError('protobuf wire type %d 不支持' % wt)
        yield f, v


def feed_stop_agencies(data, rels=None):
    """GTFS-RT FeedMessage（可 gzip）-> (header.timestamp, {(stop_id, agency_id)})。
    agency_id = trip.route_id 的「_」前缀（同 AGENCY_OP 的口径）；没有 route_id 或 stop_id 的跳过。
    rels = 只收 trip 级 schedule_relationship 在其中的 trip（缺省 OBSERVE_TRIP_RELS；'all' = 全收，只供分析）。"""
    rels = OBSERVE_TRIP_RELS if rels is None else rels
    import gzip
    if data[:2] == b'\x1f\x8b':
        data = gzip.decompress(data)
    b = memoryview(data)
    ts = None
    out = set()
    for f, v in _fields(b):
        if f == 1:
            for hf, hv in _fields(v):
                if hf == 3:
                    ts = hv
        elif f == 2:
            for ef, ev in _fields(v):
                if ef != 3:
                    continue
                route, rel, stops = None, 0, []
                for tf, tv in _fields(ev):
                    if tf == 1:
                        for df, dv in _fields(tv):
                            if df == 5:
                                route = bytes(dv).decode('utf-8', 'replace')
                            elif df == 4:
                                rel = dv
                    elif tf == 2:
                        for sf, sv in _fields(tv):
                            if sf == 4:
                                stops.append(bytes(sv).decode('utf-8', 'replace'))
                if route and '_' in route and (rels == 'all' or rel in rels):
                    a = route.split('_', 1)[0]
                    out.update((s, a) for s in stops if s)
    return ts, out


def sydney_date(ts):
    """epoch 秒 -> 悉尼当地日 'YYYY-MM-DD'（与 daily.yml 的 tag 同口径）。"""
    import datetime
    try:
        from zoneinfo import ZoneInfo
        tz = ZoneInfo('Australia/Sydney')
    except Exception:  # 本机没装 tzdata：按 AEST 近似（只记「哪天见过」，差一天无妨；CI 的 ubuntu 有 tzdata）
        tz = datetime.timezone(datetime.timedelta(hours=10))
    return datetime.datetime.fromtimestamp(ts, tz).date().isoformat()


def load_observed(path=OBSERVED_PATH):
    if not path or not os.path.exists(path):
        return {}
    doc = json.load(open(path, encoding='utf-8'))
    return doc.get('pairs') or {}


def merge_observed(pairs, observed, day, window_days=OBSERVED_WINDOW_DAYS):
    """把一份 feed 的观测 {(stop, agency)} 记为 day 见过，再删掉比 day 早 window_days 天以上的对。返回新 pairs（不改入参）。"""
    import datetime
    out = {s: dict(a) for s, a in pairs.items()}
    for s, a in observed:
        prev = out.setdefault(s, {}).get(a)
        if prev is None or prev < day:
            out[s][a] = day
    floor = (datetime.date.fromisoformat(day) - datetime.timedelta(days=window_days)).isoformat()
    out = {s: {a: d for a, d in ags.items() if d >= floor} for s, ags in out.items()}
    return {s: dict(sorted(ags.items())) for s, ags in sorted(out.items()) if ags}


def write_observed(path, pairs):
    doc = {'note': 'stop_id -> {agency_id: 最近一次在 TfNSW 实时整份公交 feed 里见到的悉尼日}。'
                   '由 scripts/stop_ops.py --observe / --observe-live 维护（observe-bus-ops.yml），不要手改。',
           'window_days': OBSERVED_WINDOW_DAYS, 'pairs': pairs}
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    # 一站一行：每天的 diff 只落在变了的站上
    lines = ['  %s: %s' % (json.dumps(s), json.dumps(a, separators=(', ', ': '))) for s, a in pairs.items()]
    with open(path, 'w', encoding='utf-8', newline='\n') as f:
        f.write('{"note": %s,\n "window_days": %d,\n "pairs": {\n%s\n }}\n'
                % (json.dumps(doc['note'], ensure_ascii=False), doc['window_days'], ',\n'.join(lines)))


def _sv_date(static_version):
    """'20261020-1305' -> '2026-10-20'"""
    s = str(static_version)
    return '%s-%s-%s' % (s[0:4], s[4:6], s[6:8])


def union_observed(m, observed, parents, static_version, window_days=OBSERVED_WINDOW_DAYS):
    """静态 m（stop -> {op|'*'}）并入窗口内的观测对。-> (新 m, 并进来的 (stop, op) 数, 未知 agency 计数)。"""
    import datetime
    floor = (datetime.date.fromisoformat(_sv_date(static_version))
             - datetime.timedelta(days=window_days)).isoformat()
    out = {s: set(v) for s, v in m.items()}
    added = 0
    unknown = collections.Counter()
    for s, ags in observed.items():
        if s not in parents:
            continue
        for a, day in ags.items():
            if day < floor:
                continue
            op = AGENCY_OP.get(a)
            if op is None:
                unknown[a] += 1
                continue
            cur = out.setdefault(s, set())
            if '*' not in cur and op not in cur:
                cur.add(op)
                added += 1
    return out, added, unknown


def _code(v):
    return '*' if '*' in v else ','.join(sorted(v))


def is_numeric(s):
    """可编码的站号：ASCII 纯数字且无前导零（'²' 让 int() 崩，'0123' 与 int 往返对不上）。"""
    return s.isascii() and s.isdigit() and str(int(s)) == s


def non_numeric(m):
    """编码不了的站号（非纯数字）。不编进 runs：busops.js 对它们一律返回 null = 取整份 feed，
    与 '*' 同一结局 —— 宁可多花 CPU，也不让一个怪站号挡住全部 mode 的 Release。"""
    return sorted(s for s in m if not is_numeric(s))


def encode(m, static_version):
    """-> (payload, 编进去的站数)。payload 键序与 .gen.js 逐字相同：static_version, min, max, sets, runs。
    非纯数字的站号跳过（见 non_numeric）；全是纯数字时与原 gen-stop-ops.py 逐字节相同。"""
    m = {s: v for s, v in m.items() if is_numeric(s)}
    if not m:
        raise ValueError('no numeric bus stop_id at all')
    sets = ['*']
    idx = {'*': 0}
    runs = []
    prev_lo = 0
    last = None
    ids = sorted(int(s) for s in m)
    for i in ids:
        c = _code(m[str(i)])
        if c == last:
            continue
        if c not in idx:
            idx[c] = len(sets)
            sets.append(c)
        runs.append('%s.%s' % (b36(i - prev_lo), b36(idx[c])))
        prev_lo = i
        last = c
    out = {'static_version': static_version, 'min': ids[0], 'max': ids[-1], 'sets': sets,
           'runs': ';'.join(runs)}
    return out, len(ids)


def release_map(db, observed):
    """Release 资产用的 stop -> 集合：静态 pattern_stops ∪ 窗口内的实时观测（observed = load_observed() 的 pairs；
    None = 只用静态，即 Worker 打包副本 / CLI 的口径）。-> (m, {'added': n, 'unknown': Counter})"""
    m = stop_agencies(db)
    if observed is None:
        return m, {'added': 0, 'unknown': collections.Counter()}
    sv = db.execute('SELECT static_version FROM meta').fetchone()[0]
    parents = {r[0] for r in db.execute('SELECT stop_id FROM stops WHERE location_type = 1')}
    m, added, unknown = union_observed(m, observed, parents, sv)
    return m, {'added': added, 'unknown': unknown}


def build(db_path, observed=None):
    """-> (payload, 编进去的站数, 跳过的非数字站号列表, 观测统计)。observed=None 时与原 gen-stop-ops.py 逐字节相同。"""
    db = _open(db_path)
    try:
        sv = db.execute('SELECT static_version FROM meta').fetchone()[0]
        m, stats = release_map(db, observed)
    finally:
        db.close()
    payload, n = encode(m, sv)
    return payload, n, non_numeric(m), stats


def lookup(payload, stop_id):
    """与 busops.js opsOfStop 同一算法：-> 集合字符串（'*' = 取整份 feed）。"""
    if not is_numeric(str(stop_id)):
        return '*'
    n = int(stop_id)
    if n < payload['min'] or n > payload['max']:
        return '*'
    lo = 0
    best = 0
    for part in payload['runs'].split(';'):
        d, k = part.split('.')
        lo += int(d, 36)
        if lo > n:
            break
        best = int(k, 36)
    return payload['sets'][best]


def render_gen_js(payload, n_stops):
    """worker/src/stop-ops.gen.js 的全文（头注释保持原生成器的字样，逐字节相同）。"""
    return ('// GENERATED by worker/scripts/gen-stop-ops.py — do not edit.\n'
            '// stops=%d runs=%d sets=%d\n' % (n_stops, len(payload['runs'].split(';')),
                                               len(payload['sets']))
            + 'export const STOP_OPS = %s;\n' % json.dumps(payload, separators=(',', ':')))


def write_stop_ops(build_dir, db_path, observed_path=OBSERVED_PATH):
    """-> (路径, payload, 站数, 跳过的非数字站号, 观测统计)。资产 = .gen.js 同形的 payload 转成 JSON，
    集合 = 静态 ∪ 滚动文件 observed_path 里窗口内的实时观测（§9.9 stop_ops_miss）。"""
    payload, n, skipped, stats = build(db_path, load_observed(observed_path))
    p = os.path.join(build_dir, STOP_OPS_FILENAME)
    with open(p, 'w', encoding='utf-8') as f:
        json.dump(payload, f, separators=(',', ':'))
    return p, payload, n, skipped, stats


def valid_stops(db_path, mode, static_version):
    """客户端可请求的父站 id（hot_stops.parent_stop_id 的全集）= 库里所有 location_type=1 的站。
    公交（self_parent）的站在 build_db 里整体提成了父站，同一句 SQL 适用。"""
    db = _open(db_path)
    try:
        ids = [r[0] for r in db.execute(
            'SELECT stop_id FROM stops WHERE location_type = 1 ORDER BY stop_id')]
    finally:
        db.close()
    return {'mode': mode, 'static_version': static_version, 'stops': ids}


def write_valid_stops(build_dir, db_path, mode, static_version):
    doc = valid_stops(db_path, mode, static_version)
    p = os.path.join(build_dir, valid_stops_filename(mode))
    with open(p, 'w', encoding='utf-8') as f:
        json.dump(doc, f, separators=(',', ':'))
    return p, doc


# ---------------------------------------------------------------- 轻轨：父站 -> 实时子 feed（B2，contract §9.2）
#
# 与 Worker 的 worker/scripts/gen-lr-feeds.py（-> worker/src/lr-feeds.gen.js 的 LR_FEEDS_BY_STOP）同一算法、同一形状：
# 有车停的站取 coalesce(parent_station, stop_id)，agency LR -> innerwest、SLR -> cbdandsoutheast、其它 -> parramatta；
# 值是排好序的 feed 键列表（Worker 的 feed 名，refresh.js LR_FEEDS）。每天随库重算，Worker 像 stop_ops 一样同步，
# 不再只信部署时打包、只对一个 static_version 有效的副本。不进 manifest（同 stop_ops.json）。

STOP_FEEDS_MODE = 'lightrail'
LR_FEEDS = ('rt:lr/innerwest', 'rt:lr/cbdandsoutheast', 'rt:lr/parramatta')
LR_AGENCY_FEED = {'LR': 'rt:lr/innerwest', 'SLR': 'rt:lr/cbdandsoutheast'}
LR_OTHER_FEED = 'rt:lr/parramatta'


def stop_feeds_filename(mode=STOP_FEEDS_MODE):
    return 'stop_feeds.%s.json' % mode


def lr_stop_feeds(db):
    """-> {parent_stop_id: [feed…]}，键按字符串序、值排序（= gen-lr-feeds.py 的 out）。"""
    m = {}
    for stop, agency in db.execute(
            "SELECT DISTINCT coalesce(nullif(s.parent_station, ''), s.stop_id), r.agency_id FROM pattern_stops ps"
            " JOIN stop_patterns sp ON sp.pattern_id = ps.pattern_id JOIN routes r ON r.route_id = sp.route_id"
            " JOIN stops s ON s.stop_id = ps.stop_id"):
        m.setdefault(stop, set()).add(LR_AGENCY_FEED.get(agency, LR_OTHER_FEED))
    return {k: sorted(v) for k, v in sorted(m.items())}


def write_stop_feeds(build_dir, db_path, static_version, mode=STOP_FEEDS_MODE):
    db = _open(db_path)
    try:
        stops = lr_stop_feeds(db)
    finally:
        db.close()
    doc = {'mode': mode, 'static_version': static_version, 'stops': stops}
    p = os.path.join(build_dir, stop_feeds_filename(mode))
    with open(p, 'w', encoding='utf-8') as f:
        json.dump(doc, f, separators=(',', ':'))
    return p, doc


def stop_feeds_errors(build_dir, db_path, static_version, mode=STOP_FEEDS_MODE):
    """validate_assets 用：stop_feeds.lightrail.json 存在、mode/static_version 对、与从库重算逐字段相等、
    值只含三条 LR feed 且非空有序、每个键都是 valid_stops 里的父站。"""
    p = os.path.join(build_dir, stop_feeds_filename(mode))
    name = os.path.basename(p)
    if not os.path.exists(p):
        return ['%s: 缺 %s（gen_manifest.py 没跑？）' % (mode, name)]
    doc = json.load(open(p, encoding='utf-8'))
    errs = []
    if list(doc) != ['mode', 'static_version', 'stops']:
        return ['%s: 键 %s != [mode, static_version, stops]' % (name, list(doc))]
    if doc['mode'] != mode or doc['static_version'] != static_version:
        errs.append('%s: mode/static_version 对不上（%r/%r）' % (name, doc['mode'], doc['static_version']))
    stops = doc['stops'] if isinstance(doc['stops'], dict) else {}
    bad = sorted(s for s, v in stops.items()
                 if not isinstance(v, list) or not v or v != sorted(set(v)) or any(f not in LR_FEEDS for f in v))
    if bad:
        errs.append('%s: %d 个站的 feed 列表为空 / 无序 / 有未知 feed：%s' % (name, len(bad), ', '.join(bad[:5])))
    db = _open(db_path)
    try:
        want = lr_stop_feeds(db)
        parents = {r[0] for r in db.execute('SELECT stop_id FROM stops WHERE location_type = 1')}
    finally:
        db.close()
    if stops != want:
        errs.append('%s 与从库重算的结果不同（被手改过，或生成器与校验器漂移）' % name)
    np = sorted(set(stops) - parents)
    if np:
        errs.append('%s: %d 个键不是库里的父站（Worker 按父站查）：%s' % (name, len(np), ', '.join(np[:5])))
    if not stops:
        errs.append('%s 是空表' % name)
    return errs


def client_requestable_stops(gz_path):
    """客户端能拿来请求实时的站 = 它装的库（gz 解压）里的父站，口径逐字取自客户端：
      core-gtfs SqliteTimetableRepository（parentStops / findStop / 搜索 / 附近：`WHERE location_type = 1`）；
      core-journey ModeIndex（`location_type = 1 OR (location_type = 0 AND parent_station IS NULL)`）。
    -> (父站集合, Journey 多出来的站（应为空）, 错误或 None)"""
    import gzip
    import shutil
    import tempfile
    if not os.path.exists(gz_path):
        return set(), [], '缺 %s（客户端装的就是它）' % os.path.basename(gz_path)
    fd, tmp = tempfile.mkstemp(suffix='.sqlite')
    os.close(fd)
    try:
        with gzip.open(gz_path, 'rb') as src, open(tmp, 'wb') as dst:
            shutil.copyfileobj(src, dst)
        db = _open(tmp)
        try:
            parents = {r[0] for r in db.execute('SELECT stop_id FROM stops WHERE location_type = 1')}
            journey = {r[0] for r in db.execute(
                'SELECT stop_id FROM stops WHERE location_type = 1'
                ' OR (location_type = 0 AND parent_station IS NULL)')}
        finally:
            db.close()
    finally:
        os.remove(tmp)
    return parents, sorted(journey - parents), None


def valid_stops_errors(build_dir, mode, db_path, static_version):
    """validate_assets 用：valid_stops.<mode>.json 存在、mode/static_version 对、与**客户端装的库**（<mode>.sqlite.gz）
    按客户端口径得出的父站集合逐个相等、Journey 索引不多出站、有序无重。"""
    p = os.path.join(build_dir, valid_stops_filename(mode))
    if not os.path.exists(p):
        return ['%s: 缺 %s（gen_manifest.py 没跑？）' % (mode, os.path.basename(p))]
    doc = json.load(open(p, encoding='utf-8'))
    errs = []
    if doc.get('mode') != mode or doc.get('static_version') != static_version:
        errs.append('%s: %s 的 mode/static_version 对不上（%r/%r）'
                    % (mode, os.path.basename(p), doc.get('mode'), doc.get('static_version')))
    got = doc.get('stops') or []
    # 对的是**客户端装的那份库**（<mode>.sqlite.gz 解压，FeedInstaller 原样装），不是旁边的 .sqlite ——
    # suburbs.py 等步骤会改库再 gzip，两份一旦不同步，Worker 的有效站表就和手机上的库对不上。
    parents, journey_extra, cerr = client_requestable_stops(db_path + '.gz')
    if cerr:
        return errs + ['%s: %s' % (mode, cerr)]
    if journey_extra:
        errs.append('%s: 客户端 Journey 索引（ModeIndex）会把 %d 个非父站当站点用，它们不在 %s：%s'
                    % (mode, len(journey_extra), os.path.basename(p), ', '.join(journey_extra[:5])))
    missing = sorted(parents - set(got))
    extra = sorted(set(got) - parents)
    if missing:
        errs.append('%s: 库里 %d 个父站不在 %s（Worker 会把它们当垃圾站号拒掉）：%s'
                    % (mode, len(missing), os.path.basename(p), ', '.join(missing[:5])))
    if extra:
        errs.append('%s: %s 有 %d 个库里没有的站：%s'
                    % (mode, os.path.basename(p), len(extra), ', '.join(extra[:5])))
    if got != sorted(set(got)):
        errs.append('%s: %s 的 stops 没排序或有重复' % (mode, os.path.basename(p)))
    if not got:
        errs.append('%s: %s 是空表' % (mode, os.path.basename(p)))
    return errs


def stop_ops_errors(build_dir, db_path, static_version, observed_path=OBSERVED_PATH):
    """validate_assets 用：stop_ops.json 存在、static_version = 库、与从库 + 同一份观测文件重算的 payload 逐字段相等、
    每个有车停 / 实时观测到的站解码回自己的集合、sets 不超 Uint8Array。"""
    p = os.path.join(build_dir, STOP_OPS_FILENAME)
    if not os.path.exists(p):
        return ['%s: 缺 %s（gen_manifest.py 没跑？）' % (STOP_OPS_MODE, STOP_OPS_FILENAME)]
    doc = json.load(open(p, encoding='utf-8'))
    errs = []
    if doc.get('static_version') != static_version:
        errs.append('%s: static_version %r != 库 %r'
                    % (STOP_OPS_FILENAME, doc.get('static_version'), static_version))
    if list(doc) != ['static_version', 'min', 'max', 'sets', 'runs']:
        errs.append('%s: 键 %s 与 .gen.js 的 payload 不同' % (STOP_OPS_FILENAME, list(doc)))
        return errs
    if len(doc['sets']) > MAX_SETS:
        errs.append('%s: sets %d 个 > %d（busops.js 的 Uint8Array 会溢出）'
                    % (STOP_OPS_FILENAME, len(doc['sets']), MAX_SETS))
    db = _open(db_path)
    try:
        m, _ = release_map(db, load_observed(observed_path))
    finally:
        db.close()
    want, _ = encode(m, static_version)
    if doc != want:
        errs.append('%s 与从库重算的结果不同（被手改过，或生成器与校验器漂移）' % STOP_OPS_FILENAME)
    # 非数字站号按设计解码成 '*'（取整份 feed），其余必须解码回自己的集合
    bad = [s for s, v in m.items() if lookup(doc, s) != (_code(v) if is_numeric(s) else '*')]
    if bad:
        errs.append('%s: %d 个有车停的站解码不回自己的运营商集合：%s'
                    % (STOP_OPS_FILENAME, len(bad), ', '.join(sorted(bad)[:5])))
    return errs


# ---------------------------------------------------------------- selftest

def _fixture_db(path, rows, parents_extra=()):
    """rows: [(stop_id, agency_id)]，每对建一条单站 pattern。"""
    db = sqlite3.connect(path)
    db.executescript(
        'CREATE TABLE meta (static_version TEXT);'
        'CREATE TABLE stops (stop_id TEXT, location_type INTEGER, parent_station TEXT);'
        'CREATE TABLE routes (route_id TEXT, agency_id TEXT);'
        'CREATE TABLE stop_patterns (pattern_id INTEGER, route_id TEXT);'
        'CREATE TABLE pattern_stops (pattern_id INTEGER, stop_id TEXT);')
    db.execute("INSERT INTO meta VALUES ('20261017-0000')")
    stops = sorted({s for s, _ in rows} | set(parents_extra))
    db.executemany('INSERT INTO stops VALUES (?, 1, NULL)', [(s,) for s in stops])
    db.execute("INSERT INTO stops VALUES ('9999990', 0, ?)", (stops[0],))  # 子站：不进 valid_stops
    for k, (s, a) in enumerate(rows):
        db.execute('INSERT INTO routes VALUES (?, ?)', ('%s_R%d' % (a, k), a))
        db.execute('INSERT INTO stop_patterns VALUES (?, ?)', (k, '%s_R%d' % (a, k)))
        db.execute('INSERT INTO pattern_stops VALUES (?, ?)', (k, s))
    db.commit()
    db.close()
    _gzip_copy(path)


def _gzip_copy(path):
    """测试用：像 build_db / suburbs 那样把库 gzip 成客户端下载的 <path>.gz"""
    import gzip
    import shutil
    with open(path, 'rb') as src, gzip.open(path + '.gz', 'wb') as dst:
        shutil.copyfileobj(src, dst)


def selftest():
    import tempfile
    n = 0
    with tempfile.TemporaryDirectory() as d:
        dbp = os.path.join(d, 'buses.sqlite')
        _fixture_db(dbp, [('200010', '2509'), ('200011', '2509'), ('200050', '2606'),
                          ('200060', '2501'), ('200060', '2509'), ('2782193', '2514')],
                    parents_extra=('200099',))
        payload, ns, skipped, _ = build(dbp)
        assert ns == 5 and skipped == [] and payload['min'] == 200010 and payload['max'] == 2782193, payload
        assert payload['sets'][0] == '*'
        for s, want in (('200010', 'GSBC009'), ('200011', 'GSBC009'), ('200030', 'GSBC009'),
                        ('200050', '*'), ('200060', 'GSBC001,GSBC009'), ('2782193', 'GSBC014'),
                        ('200009', '*'), ('2782194', '*'), ('20A', '*')):
            assert lookup(payload, s) == want, (s, lookup(payload, s), want)
            n += 1
        js = render_gen_js(payload, ns)
        assert js.startswith('// GENERATED by worker/scripts/gen-stop-ops.py — do not edit.\n'
                             '// stops=5 runs=4 sets=4\nexport const STOP_OPS = {"static_version"')
        n += 1
        # 非数字站号：跳过（解码 = '*'），payload 与没有它时逐字相同，不抛错、不挡 Release
        m = {'200010': {'GSBC009'}, '200050': {'*'}}
        mixed = dict(m, **{'20A': {'GSBC001'}, 'X1': {'*'}})
        assert non_numeric(mixed) == ['20A', 'X1']
        assert encode(mixed, 'x') == encode(m, 'x'), encode(mixed, 'x')
        assert lookup(encode(mixed, 'x')[0], '20A') == '*'
        n += 1
        # 非 ASCII 数字（int('²') 会崩）与前导零（str(int) 往返不等）同样跳过、解码为 '*'
        for odd in ('²', '0123'):
            mo = dict(m, **{odd: {'GSBC001'}})
            assert non_numeric(mo) == [odd] and encode(mo, 'x') == encode(m, 'x'), odd
            assert lookup(encode(mo, 'x')[0], odd) == '*', odd
            n += 1
        try:
            encode({'20A': {'*'}}, 'x')
            raise AssertionError('all non-numeric must raise')
        except ValueError:
            n += 1

        # 校验器：好资产 -> 0 错；四类坏资产 -> 各自报错
        sv = '20261017-0000'
        write_stop_ops(d, dbp, None)
        write_valid_stops(d, dbp, 'buses', sv)
        assert stop_ops_errors(d, dbp, sv, None) == [] and valid_stops_errors(d, 'buses', dbp, sv) == []
        vs = json.load(open(os.path.join(d, 'valid_stops.buses.json'), encoding='utf-8'))
        assert '200099' in vs['stops'] and '9999990' not in vs['stops'], vs
        n += 1

        def expect_fail(errs, needle):
            assert errs and any(needle in e for e in errs), (needle, errs)

        expect_fail(stop_ops_errors(d, dbp, 'other-version', None), 'static_version')
        expect_fail(valid_stops_errors(d, 'buses', dbp, 'other-version'), 'static_version')
        # 手改一段的运营商
        doc = json.load(open(os.path.join(d, STOP_OPS_FILENAME), encoding='utf-8'))
        doc['sets'][1] = 'GSBC001'
        json.dump(doc, open(os.path.join(d, STOP_OPS_FILENAME), 'w', encoding='utf-8'))
        expect_fail(stop_ops_errors(d, dbp, sv, None), '重算')
        # 漏一个父站
        vs['stops'].remove('200099')
        json.dump(vs, open(os.path.join(d, 'valid_stops.buses.json'), 'w', encoding='utf-8'))
        expect_fail(valid_stops_errors(d, 'buses', dbp, sv), '200099')
        # 资产缺失
        os.remove(os.path.join(d, STOP_OPS_FILENAME))
        os.remove(os.path.join(d, 'valid_stops.buses.json'))
        expect_fail(stop_ops_errors(d, dbp, sv, None), '缺 stop_ops.json')
        expect_fail(valid_stops_errors(d, 'buses', dbp, sv), '缺 valid_stops.buses.json')
        n += 6

        # 库里混进一个非数字站号：照常出资产、校验通过（不挡 Release），该站解码为 '*'
        dbx = os.path.join(d, 'mixed.sqlite')
        _fixture_db(dbx, [('200010', '2509'), ('X200', '2501')])
        _, _, _, skipped, _ = write_stop_ops(d, dbx, None)
        write_valid_stops(d, dbx, 'buses', sv)
        assert skipped == ['X200'], skipped
        assert stop_ops_errors(d, dbx, sv, None) == [] and valid_stops_errors(d, 'buses', dbx, sv) == []
        n += 1
        n += _selftest_client_stops(d, sv)
        n += _selftest_observed(d, dbp, sv)
        n += _selftest_stop_feeds(d)
    return n


def _selftest_client_stops(d, sv):
    """valid_stops 对的是客户端装的 .sqlite.gz、客户端的口径：旁边的 .sqlite 与 gz 不同步、或库里有 Journey
    会当站点用的孤站（location_type=0 且无父站）都要红。旧校验器读 .sqlite、只看 location_type=1，两类都放过。"""
    n = 0
    dbc = os.path.join(d, 'client.sqlite')
    _fixture_db(dbc, [('200010', '2509'), ('200011', '2509')])
    write_valid_stops(d, dbc, 'buses', sv)
    assert valid_stops_errors(d, 'buses', dbc, sv) == []
    # 1) gz 是旧的：.sqlite 后来多了一个父站（如某步改库后忘了重新 gzip）。valid_stops 按 .sqlite 生成，客户端却没有它
    db = sqlite3.connect(dbc)
    db.execute("INSERT INTO stops VALUES ('200012', 1, NULL)")
    db.commit()
    db.close()
    write_valid_stops(d, dbc, 'buses', sv)
    errs = valid_stops_errors(d, 'buses', dbc, sv)
    assert errs and any('200012' in e and '库里没有' in e for e in errs), errs
    _gzip_copy(dbc)
    assert valid_stops_errors(d, 'buses', dbc, sv) == []
    n += 1
    # 2) 孤站：Journey 的 ModeIndex 会把它当站点（首段实时会请求它），valid_stops 里却没有
    db = sqlite3.connect(dbc)
    db.execute("INSERT INTO stops VALUES ('200013', 0, NULL)")
    db.commit()
    db.close()
    _gzip_copy(dbc)
    errs = valid_stops_errors(d, 'buses', dbc, sv)
    assert errs and any('Journey' in e and '200013' in e for e in errs), errs
    n += 1
    # 3) 缺 gz
    os.remove(dbc + '.gz')
    errs = valid_stops_errors(d, 'buses', dbc, sv)
    assert errs and any('缺 client.sqlite.gz' in e for e in errs), errs
    n += 1
    return n


def _selftest_stop_feeds(d):
    """轻轨 stop_feeds：子站归到父站、agency -> feed、多 agency 的站列多条，校验器四类坏资产各报错。"""
    n = 0
    dbp = os.path.join(d, 'lightrail.sqlite')
    db = sqlite3.connect(dbp)
    db.executescript(
        'CREATE TABLE meta (static_version TEXT);'
        'CREATE TABLE stops (stop_id TEXT, location_type INTEGER, parent_station TEXT);'
        'CREATE TABLE routes (route_id TEXT, agency_id TEXT);'
        'CREATE TABLE stop_patterns (pattern_id INTEGER, route_id TEXT);'
        'CREATE TABLE pattern_stops (pattern_id INTEGER, stop_id TEXT);'
        "INSERT INTO meta VALUES ('20261020-1305');"
        "INSERT INTO stops VALUES ('P1', 1, NULL), ('P1a', 0, 'P1'), ('P2', 1, ''), ('P3', 1, NULL), ('P4', 1, NULL);"
        "INSERT INTO routes VALUES ('IWLR-191', 'LR'), ('L2', 'SLR'), ('L4', 'PLR');"
        "INSERT INTO stop_patterns VALUES (1, 'IWLR-191'), (2, 'L2'), (3, 'L4');"
        "INSERT INTO pattern_stops VALUES (1, 'P1a'), (2, 'P2'), (2, 'P3'), (3, 'P3');")
    db.commit()
    db.close()
    sv = '20261020-1305'
    p, doc = write_stop_feeds(d, dbp, sv)
    assert os.path.basename(p) == 'stop_feeds.lightrail.json'
    assert doc == {'mode': 'lightrail', 'static_version': sv, 'stops': {
        'P1': ['rt:lr/innerwest'], 'P2': ['rt:lr/cbdandsoutheast'],
        'P3': ['rt:lr/cbdandsoutheast', 'rt:lr/parramatta']}}, doc      # P4 无车停：不列（Worker 回落三条）
    assert stop_feeds_errors(d, dbp, sv) == []
    n += 1

    def expect_fail(errs, needle):
        assert errs and any(needle in e for e in errs), (needle, errs)

    expect_fail(stop_feeds_errors(d, dbp, 'other'), 'static_version')
    bad = dict(doc, stops=dict(doc['stops'], P1=['rt:lr/somewhere']))
    json.dump(bad, open(p, 'w', encoding='utf-8'))
    expect_fail(stop_feeds_errors(d, dbp, sv), '未知 feed')
    bad = dict(doc, stops=dict(doc['stops'], P1a=['rt:lr/innerwest']))
    json.dump(bad, open(p, 'w', encoding='utf-8'))
    expect_fail(stop_feeds_errors(d, dbp, sv), '不是库里的父站')
    expect_fail(stop_feeds_errors(d, dbp, sv), '重算')
    os.remove(p)
    expect_fail(stop_feeds_errors(d, dbp, sv), '缺 stop_feeds.lightrail.json')
    n += 4
    return n


def _pb(f, v):
    """测试用的最小 protobuf 编码：int -> varint 字段，bytes/str -> LEN 字段。"""
    def vi(x):
        o = b''
        while True:
            if x < 0x80:
                return o + bytes([x])
            o += bytes([(x & 0x7f) | 0x80])
            x >>= 7
    if isinstance(v, int):
        return vi(f << 3) + vi(v)
    v = v.encode() if isinstance(v, str) else v
    return vi(f << 3 | 2) + vi(len(v)) + v


def _feed(ts, trips):
    """trips: [(route_id, trip 级 schedule_relationship, [stop_id…])] -> FeedMessage 字节"""
    out = _pb(1, _pb(1, '2.0') + _pb(3, ts))
    for k, (route, rel, stops) in enumerate(trips):
        td = _pb(1, 't%d' % k) + _pb(4, rel) + _pb(5, route)
        stus = b''.join(_pb(2, _pb(1, i + 1) + _pb(4, s)) for i, s in enumerate(stops))
        out += _pb(2, _pb(1, 'e%d' % k) + _pb(3, _pb(1, td) + stus))
    return out


def _selftest_observed(d, dbp, sv):
    """§9.9 stop_ops_miss：实时观测并进 stop_ops。静态 200010 = GSBC009、200050 = '*'（2606）。"""
    import gzip
    n = 0
    ts = 1760576400  # 2025-10-16 01:00 UTC = 悉尼 2025-10-16 12:00
    feed = _feed(ts, [('2501_X1', 1, ['200010', '300000']),   # ADDED：并进 200010；300000 不在库里 -> 跳过
                      ('2507_722w', 0, ['200011']),            # SCHEDULED 仅实时（校车类）：客户端挂不上 -> 不收
                      ('2503_R', 5, ['200099']),               # REPLACEMENT：收（200099 是无车停的父站）
                      ('2606_CC', 1, ['200011']),              # 未知 agency：不收进集合、不把站变 '*'
                      ('2501_X2', 1, ['200050']),              # '*' 站：保持 '*'
                      ('', 1, ['200010']), ('noprefix', 1, ['200010'])])
    got_ts, pairs = feed_stop_agencies(gzip.compress(feed))
    assert got_ts == ts, got_ts
    assert pairs == {('200010', '2501'), ('300000', '2501'), ('200099', '2503'), ('200011', '2606'),
                     ('200050', '2501')}, pairs
    assert ('200011', '2507') in feed_stop_agencies(feed, 'all')[1]
    n += 1
    # 滚动文件：记日子、窗口外的删掉、重复观测只前移日期
    day = sydney_date(ts)
    assert day == '2025-10-16', day
    old = {'200011': {'2502': '2025-09-01'}, '200010': {'2501': '2025-10-01'}}
    merged = merge_observed(old, pairs, day)
    assert merged['200010'] == {'2501': day} and '200011' in merged and '2502' not in merged['200011'], merged
    n += 1
    obs = os.path.join(d, 'observed.json')
    write_observed(obs, dict(merged, **{'200060': {'2510': '2025-01-01'}}))   # 窗口外：不并
    # 静态库 static_version 20261017 -> 窗口锚在 2026-10-17；把观测日期挪到 2026-10 让它们在窗口内
    pairs26 = {s: {a: '2026-10-16' if dt == day else dt for a, dt in ags.items()}
               for s, ags in load_observed(obs).items()}
    write_observed(obs, pairs26)
    p, payload, _, _, stats = write_stop_ops(d, dbp, obs)
    assert lookup(payload, '200010') == 'GSBC001,GSBC009', lookup(payload, '200010')
    assert lookup(payload, '200011') == 'GSBC009'          # 2606 未知、2507 SCHEDULED：都不进
    assert lookup(payload, '200050') == '*'                # '*' 吸收一切
    assert lookup(payload, '200099') == 'GSBC003'          # REPLACEMENT 在无车停的父站
    assert lookup(payload, '200060') == 'GSBC001,GSBC009'  # 窗口外的 2510 不进
    assert stats['added'] == 2 and stats['unknown'] == {'2606': 1}, stats
    n += 1
    # 没有观测文件 = 与静态逐字节相同（Worker 打包副本 / CLI 的口径不变）
    assert build(dbp, {})[0] == build(dbp)[0]
    n += 1
    # 校验器：同一份观测文件 -> 0 错；拿别的观测文件（或不用）重算 -> 报「重算」
    assert stop_ops_errors(d, dbp, sv, obs) == [], stop_ops_errors(d, dbp, sv, obs)
    errs = stop_ops_errors(d, dbp, sv, None)
    assert errs and any('重算' in e for e in errs), errs
    n += 1
    return n


def observe_files(path, feeds):
    """把若干份已落盘的整份公交 TripUpdates（.pb / .pb.gz）并进滚动文件。-> 新增 / 前移的对数"""
    pairs = load_observed(path)
    before = json.dumps(pairs, sort_keys=True)
    for fp in feeds:
        ts, obs = feed_stop_agencies(open(fp, 'rb').read())
        if not ts:
            raise SystemExit('%s: FeedHeader 没有 timestamp' % fp)
        pairs = merge_observed(pairs, obs, sydney_date(ts))
    write_observed(path, pairs)
    return before != json.dumps(pairs, sort_keys=True), sum(len(v) for v in pairs.values())


def observe_live(path):
    """CI（observe-bus-ops.yml）：拉 **一次** 整份公交 TripUpdates（1 个 TfNSW 请求，只在内存里），并进滚动文件。
    key 同 fetch_gtfs.py（TFNSW_API_KEY），永不打印、永不写盘；feed 也不落盘。"""
    import urllib.request
    from fetch_gtfs import api_key
    req = urllib.request.Request(RT_BUSES_URL, headers={'Authorization': 'apikey ' + api_key(),
                                                        'User-Agent': 'transport-peek-data/observe-bus-ops'})
    with urllib.request.urlopen(req, timeout=60) as r:
        data = r.read()
    ts, obs = feed_stop_agencies(data)
    if not ts:
        raise SystemExit('实时 feed 没有 header.timestamp')
    pairs = merge_observed(load_observed(path), obs, sydney_date(ts))
    write_observed(path, pairs)
    print('observe: feed %s，%d 个 ADDED/REPLACEMENT (站, agency)，滚动文件共 %d 对'
          % (sydney_date(ts), len(obs), sum(len(v) for v in pairs.values())))


if __name__ == '__main__':
    if sys.argv[1:] == ['--selftest']:
        print('stop_ops selftest ok，%d 组' % selftest())
    elif sys.argv[1:2] == ['--observe'] and len(sys.argv) >= 4:
        changed, total = observe_files(sys.argv[2], sys.argv[3:])
        print('observe: %d 份 feed，滚动文件共 %d 对%s' % (len(sys.argv) - 3, total, '' if changed else '（无变化）'))
    elif sys.argv[1:2] == ['--observe-live'] and len(sys.argv) == 3:
        observe_live(sys.argv[2])
    elif len(sys.argv) == 2:
        try:
            payload, n, skipped, _ = build(sys.argv[1])
        except ValueError as e:
            sys.exit(str(e))
        if skipped:
            sys.stderr.write('skipped %d non-numeric stop_id (-> full feed): %s\n'
                             % (len(skipped), ', '.join(skipped[:5])))
        sys.stdout.write(render_gen_js(payload, n))
    else:
        sys.exit(__doc__)
