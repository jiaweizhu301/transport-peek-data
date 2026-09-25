#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""M6 §6.2 · 线路图 JSON（sydney-rail.json）校验。

  python check_schematic.py <schematic.json> <build_dir>
  python check_schematic.py --selftest

真源在主仓 `app/src/main/assets/schematic/sydney-rail.json`；data 仓 `schematic/sydney-rail.json`
是逐字节副本（同步方式见 M6 P3 计划 Task 11）。

退出码：
  0 = 全部通过
  1 = 只有告警（规则 R1–R3 / ④ / 颜色 / 连通 / 45° 布局）。daily.yml 里这一步
      continue-on-error —— **只告警，不阻塞发版**（M6-design §6.2）。
  2 = JSON 结构本身坏了（格式、引用、下标）。这份 JSON 连 app 都读不了，必须修。
"""
import contextlib
import copy
import io
import json
import math
import os
import re
import sqlite3
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
RAIL_MODES = ('sydneytrains', 'metro', 'lightrail')
MAX_VEHICLE_ROUTES = 20   # 契约 3 v2.1：vehicles= 同 mode 1–20 个 route_id；班次详情按整条线的 routeKey 一次请求
ANCHORS = {'n', 'ne', 'e', 'se', 's', 'sw', 'w', 'nw'}
COLOUR_RE = re.compile(r'^#[0-9A-Fa-f]{6}$')


def structural_errors(doc):
    """结构错误（退出码 2）。返回字符串列表。"""
    errs = []
    if doc.get('format') != 1:
        return ['format 必须是 1，实际 %r' % doc.get('format')]
    b = doc.get('bounds')
    if not (isinstance(b, list) and len(b) == 4 and all(isinstance(v, (int, float)) for v in b)
            and b[2] > b[0] and b[3] > b[1]):
        errs.append('bounds 必须是 [x0, y0, x1, y1] 且 x1>x0、y1>y0')
        b = [float('-inf'), float('-inf'), float('inf'), float('inf')]
    stations = {}
    for s in doc.get('stations', []):
        sid = s.get('id')
        if not isinstance(sid, str) or not sid:
            errs.append('有站缺 id')
            continue
        if sid in stations:
            errs.append('站 id 重复：%s' % sid)
        stations[sid] = s
        x, y = s.get('x'), s.get('y')
        if not isinstance(x, (int, float)) or not isinstance(y, (int, float)):
            errs.append('%s: x/y 必须是数字' % sid)
        elif not (b[0] <= x <= b[2] and b[1] <= y <= b[3]):
            errs.append('%s: (%s, %s) 在 bounds 外' % (sid, x, y))
        lab = s.get('label') or {}
        if not lab.get('text'):
            errs.append('%s: label.text 为空' % sid)
        if lab.get('anchor') not in ANCHORS:
            errs.append('%s: label.anchor=%r 不在 %s' % (sid, lab.get('anchor'), sorted(ANCHORS)))
        if not isinstance(s.get('interchange', False), bool):
            errs.append('%s: interchange 必须是 true/false' % sid)
        parents = s.get('parents')
        if not isinstance(parents, dict):
            errs.append('%s: parents 必须是 {mode: [stop_id…]}' % sid)
        else:
            for m, ids in parents.items():
                if m not in RAIL_MODES:
                    errs.append('%s: parents 里有未知 mode %r' % (sid, m))
                if not (isinstance(ids, list) and all(isinstance(i, str) for i in ids)):
                    errs.append('%s: parents[%s] 必须是字符串列表' % (sid, m))
    line_ids = set()
    for ln in doc.get('lines', []):
        lid = ln.get('id')
        if lid in line_ids:
            errs.append('线 id 重复：%s' % lid)
        line_ids.add(lid)
        if ln.get('mode') not in RAIL_MODES:
            errs.append('%s: mode=%r' % (lid, ln.get('mode')))
        for k in ('colour', 'text_colour'):
            if not COLOUR_RE.match(str(ln.get(k, ''))):
                errs.append('%s: %s=%r 不是 #RRGGBB' % (lid, k, ln.get(k)))
        if not ln.get('route_ids'):
            errs.append('%s: route_ids 为空' % lid)
        if not ln.get('paths'):
            errs.append('%s: 没有 paths' % lid)
        for pi, p in enumerate(ln.get('paths', [])):
            st = p.get('stations', [])
            if len(st) < 2:
                errs.append('%s.paths[%d]: 少于 2 站' % (lid, pi))
            for sid in st:
                if sid not in stations:
                    errs.append('%s.paths[%d]: 站 %s 不在 stations 里' % (lid, pi, sid))
            for key, pts in (p.get('via') or {}).items():
                if not (key.isdigit() and 0 <= int(key) < len(st) - 1):
                    errs.append('%s.paths[%d].via: 键 %r 不是 0..%d 的下标' % (lid, pi, key, len(st) - 2))
                if not (isinstance(pts, list) and all(isinstance(q, list) and len(q) == 2 for q in pts)):
                    errs.append('%s.paths[%d].via[%s]: 必须是 [[x, y], …]' % (lid, pi, key))
            t = p.get('track', 0)
            if not (isinstance(t, int) and t >= 0):
                errs.append('%s.paths[%d]: track=%r 必须是 ≥0 的整数' % (lid, pi, t))
    if not all(isinstance(r, str) for r in doc.get('ignored_route_ids', [])):
        errs.append('ignored_route_ids 必须是字符串列表')
    return errs


def _open(build_dir, mode):
    p = os.path.join(build_dir, mode + '.sqlite')
    if not os.path.exists(p):
        return None
    return sqlite3.connect('file:%s?immutable=1' % p.replace('\\', '/'), uri=True)


def _adjacent_parent_pairs(db):
    """静态库里「连续经过」的父站对（两个方向都算）。按 pattern 的 stop_sequence 走。"""
    pairs = set()
    rows = db.execute(
        'SELECT ps.pattern_id, COALESCE(s.parent_station, s.stop_id) '
        'FROM pattern_stops ps JOIN stops s ON s.stop_id = ps.stop_id '
        'ORDER BY ps.pattern_id, ps.stop_sequence').fetchall()
    prev_pat, prev = None, None
    for pat, parent in rows:
        if pat == prev_pat and prev is not None and parent != prev:
            pairs.add((prev, parent))
            pairs.add((parent, prev))
        prev_pat, prev = pat, parent
    return pairs


def _octilinear(p, q):
    dx, dy = q[0] - p[0], q[1] - p[1]
    if dx == 0 and dy == 0:
        return True
    ang = math.degrees(math.atan2(dy, dx)) % 45
    return ang < 0.5 or ang > 44.5


def static_warnings(doc, build_dir):
    """对照静态库的告警（退出码 1）。"""
    warns = []
    stations = {s['id']: s for s in doc['stations']}
    # ④：每条线的 route_id ≤ 20（不需要静态库，最先查）
    for ln in doc['lines']:
        if len(ln['route_ids']) > MAX_VEHICLE_ROUTES:
            warns.append('④ %s: route_ids 有 %d 个 > %d（vehicles= 上限，spec §6.2）'
                         % (ln['id'], len(ln['route_ids']), MAX_VEHICLE_ROUTES))
    dbs = {m: _open(build_dir, m) for m in RAIL_MODES}
    for m, db in dbs.items():
        if db is None:
            warns.append('（跳过）%s.sqlite 不在 %s，%s 的 R1–R3 没查' % (m, build_dir, m))
    parents_of = {}
    for m, db in dbs.items():
        if db is not None:
            parents_of[m] = {r[0] for r in db.execute('SELECT stop_id FROM stops WHERE location_type = 1')}
    # R1：parents 里每个 stop_id 在静态库存在
    for sid, s in stations.items():
        for m, ids in s['parents'].items():
            if m not in parents_of:
                continue
            for pid in ids:
                if pid not in parents_of[m]:
                    warns.append('R1 %s: %s 父站 %s 不在静态库' % (sid, m, pid))
    # R2：每个 route_id 恰好映射到一条线，或在 ignored_route_ids
    ignored = set(doc.get('ignored_route_ids', []))
    owner = {}
    for ln in doc['lines']:
        for rid in ln['route_ids']:
            owner.setdefault((ln['mode'], rid), []).append(ln['id'])
    for m, db in dbs.items():
        if db is None:
            continue
        routes = {r[0]: r[1] for r in db.execute('SELECT route_id, route_color FROM routes')}
        for rid in sorted(routes):
            got = owner.get((m, rid), [])
            if rid in ignored:
                continue
            if len(got) != 1:
                warns.append('R2 %s/%s 映射到 %d 条线 %s（应恰好 1 条，或进 ignored_route_ids）'
                             % (m, rid, len(got), got))
        for (mm, rid), lids in sorted(owner.items()):
            if mm == m and rid not in routes:
                warns.append('R2 %s: route_id %s 在静态库里已不存在（网络变了？）' % (','.join(lids), rid))
        # 颜色：线的 colour 与它每个 route 的 route_color 一致
        for ln in doc['lines']:
            if ln['mode'] != m:
                continue
            for rid in ln['route_ids']:
                rc = routes.get(rid)
                if rc and rc.upper() != ln['colour'].lstrip('#').upper():
                    warns.append('颜色 %s: colour=%s，但 %s 的 route_color=#%s' % (ln['id'], ln['colour'], rid, rc))
    # R3：相邻两站在静态 stop_times 里至少有一个 trip 连续经过
    pair_cache = {m: _adjacent_parent_pairs(db) for m, db in dbs.items() if db is not None}
    for ln in doc['lines']:
        m = ln['mode']
        if m not in pair_cache:
            continue
        pairs = pair_cache[m]
        for pi, p in enumerate(ln['paths']):
            st = p['stations']
            for a, b in zip(st, st[1:]):
                pa = stations[a]['parents'].get(m, [])
                pb = stations[b]['parents'].get(m, [])
                if not any((x, y) in pairs for x in pa for y in pb):
                    warns.append('R3 %s.paths[%d]: %s → %s 在静态库里没有任何 trip 连续经过' % (ln['id'], pi, a, b))
    # 连通：同一条线的所有 path 连成一张图
    for ln in doc['lines']:
        parent = {}

        def find(x):
            while parent.setdefault(x, x) != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x
        for p in ln['paths']:
            for a, b in zip(p['stations'], p['stations'][1:]):
                parent[find(a)] = find(b)
        roots = {find(x) for p in ln['paths'] for x in p['stations']}
        if len(roots) > 1:
            warns.append('连通 %s: paths 分成 %d 块，互不相连' % (ln['id'], len(roots)))
    # 布局：每一段（含折点）都是 0/45/90°（TV-2 的「横平竖直 + 45°」）
    for ln in doc['lines']:
        for pi, p in enumerate(ln['paths']):
            st = p['stations']
            via = p.get('via') or {}
            for i in range(len(st) - 1):
                pts = [[stations[st[i]]['x'], stations[st[i]]['y']]] + via.get(str(i), []) + \
                      [[stations[st[i + 1]]['x'], stations[st[i + 1]]['y']]]
                if not all(_octilinear(q0, q1) for q0, q1 in zip(pts, pts[1:])):
                    warns.append('布局 %s.paths[%d]: %s→%s 有一段不是 0/45/90°' % (ln['id'], pi, st[i], st[i + 1]))
    for db in dbs.values():
        if db is not None:
            db.close()
    return warns


def run(json_path, build_dir):
    doc = json.load(open(json_path, encoding='utf-8'))
    # 首行打印版本：每日 Job Summary 看得出 data 仓副本停在哪一版（副本只能从主仓真源拷，§6.2）
    print('schematic data_version %s' % doc.get('data_version'), flush=True)
    errs = structural_errors(doc)
    for e in errs:
        print('ERROR ' + e)
    if errs:
        print('线路图 JSON 结构错误 %d 处 -> 这份 JSON app 读不了，必须修' % len(errs))
        return 2
    warns = static_warnings(doc, build_dir)
    for w in warns:
        print('WARN  ' + w)
    summary = os.environ.get('GITHUB_STEP_SUMMARY')
    if summary and warns:
        with open(summary, 'a', encoding='utf-8') as f:
            f.write('## 线路图 JSON 告警（不阻塞发版）\n\n网络可能变了：agent 改 '
                    '`app/src/main/assets/schematic/sydney-rail.json` 并随下一个 APK 发出。\n\n')
            for w in warns:
                f.write('- %s\n' % w)
    if warns:
        print('线路图 JSON：%d 条告警（只告警，不阻塞发版）' % len(warns))
        return 1
    print('OK   线路图 JSON：%d 站 / %d 线，结构与静态库对照全部通过'
          % (len(doc['stations']), len(doc['lines'])))
    return 0


def _selftest_db(path):
    db = sqlite3.connect(path)
    db.executescript(open(os.path.join(HERE, 'schema.sql'), encoding='utf-8').read())
    db.executemany('INSERT INTO stops(stop_id, name, name_normalized, lat, lon, location_type, parent_station)'
                   ' VALUES (?,?,?,?,?,?,?)', [
                       ('P_A', 'A', 'a', 0, 0, 1, None), ('P_B', 'B', 'b', 0, 0, 1, None),
                       ('P_C', 'C', 'c', 0, 0, 1, None),
                       ('a1', 'A P1', 'a p1', 0, 0, 0, 'P_A'), ('b1', 'B P1', 'b p1', 0, 0, 0, 'P_B'),
                       ('c1', 'C P1', 'c p1', 0, 0, 0, 'P_C')])
    db.executemany('INSERT INTO routes VALUES (?,?,?,?,?,?,?)', [
        ('R1', 'AG', 'T9', None, 2, 'D11F2F', 'FFFFFF'), ('R2', 'AG', 'T9', None, 2, 'D11F2F', 'FFFFFF'),
        ('RTTA_DEF', 'AG', 'RTTA', None, 2, None, None)])
    db.execute('INSERT INTO stop_patterns VALUES (1, ?, 3)', ('R1',))
    db.executemany('INSERT INTO pattern_stops VALUES (1, ?, ?, 0, 0)', [(1, 'a1'), (2, 'b1'), (3, 'c1')])
    db.commit()
    db.close()


def _selftest_doc():
    return {
        'format': 1, 'network': 'selftest', 'data_version': '2026-09-24', 'source': 'selftest',
        'bounds': [0, 0, 1000, 1000],
        'stations': [
            {'id': 'a', 'parents': {'sydneytrains': ['P_A']}, 'x': 100, 'y': 100,
             'label': {'text': 'A', 'anchor': 'e', 'dx': 6, 'dy': 0}},
            {'id': 'b', 'parents': {'sydneytrains': ['P_B']}, 'x': 200, 'y': 100,
             'label': {'text': 'B', 'anchor': 'e', 'dx': 6, 'dy': 0}},
            {'id': 'c', 'parents': {'sydneytrains': ['P_C']}, 'x': 300, 'y': 200,
             'label': {'text': 'C', 'anchor': 'e', 'dx': 6, 'dy': 0}}],
        'lines': [{'id': 'T9', 'mode': 'sydneytrains', 'route_ids': ['R1', 'R2'],
                   'colour': '#D11F2F', 'text_colour': '#FFFFFF',
                   'paths': [{'stations': ['a', 'b', 'c'], 'track': 0}]}],
        'ignored_route_ids': ['RTTA_DEF']}


def selftest():
    n = 0
    with tempfile.TemporaryDirectory() as d:
        _selftest_db(os.path.join(d, 'sydneytrains.sqlite'))
        good = _selftest_doc()
        assert structural_errors(good) == [], structural_errors(good)
        w = [x for x in static_warnings(good, d) if not x.startswith('（跳过）')]
        assert w == [], w
        n += 1
        # R1：父站不存在
        bad = copy.deepcopy(good)
        bad['stations'][0]['parents']['sydneytrains'] = ['P_GONE']
        assert any(x.startswith('R1 a:') for x in static_warnings(bad, d))
        n += 1
        # 跨 mode 共用站（Central 同时挂 trains + metro）：metro 库不在 → 只跳过 metro，trains 照查、不误报
        multi = copy.deepcopy(good)
        multi['stations'][0]['parents']['metro'] = ['M_WHATEVER']
        w = static_warnings(multi, d)
        assert any(x.startswith('（跳过）metro.sqlite') for x in w), w
        assert not any(x.startswith('R1') for x in w), w
        n += 1
        # R2：route 没人认领
        bad = copy.deepcopy(good)
        bad['lines'][0]['route_ids'] = ['R1']
        assert any(x.startswith('R2 sydneytrains/R2') for x in static_warnings(bad, d))
        n += 1
        # R2：route 被两条线认领
        bad = copy.deepcopy(good)
        bad['lines'].append(dict(copy.deepcopy(bad['lines'][0]), id='T9b'))
        assert any('映射到 2 条线' in x for x in static_warnings(bad, d))
        n += 1
        # R2 反向：线上写了一个静态库里没有的 route
        bad = copy.deepcopy(good)
        bad['lines'][0]['route_ids'].append('R_GONE')
        assert any('R_GONE 在静态库里已不存在' in x for x in static_warnings(bad, d))
        n += 1
        # R3：a → c 没有 trip 连续经过（中间隔着 b）
        bad = copy.deepcopy(good)
        bad['lines'][0]['paths'][0]['stations'] = ['a', 'c']
        assert any(x.startswith('R3 T9.paths[0]: a → c') for x in static_warnings(bad, d))
        n += 1
        # ④：21 个 route_id 告警，20 个不告警（多出来的 route 另被 R2 反向报「不在静态库」，这里只看 ④）
        bad = copy.deepcopy(good)
        bad['lines'][0]['route_ids'] = ['R1', 'R2'] + ['RX%d' % i for i in range(19)]
        assert any(x.startswith('④ T9: route_ids 有 21 个') for x in static_warnings(bad, d))
        bad['lines'][0]['route_ids'] = bad['lines'][0]['route_ids'][:20]
        assert not any(x.startswith('④') for x in static_warnings(bad, d))
        n += 1
        # run() 首行打印 data_version；selftest 里不许往 Job Summary 写东西
        os.environ.pop('GITHUB_STEP_SUMMARY', None)
        gp = os.path.join(d, 'good.json')
        json.dump(good, open(gp, 'w', encoding='utf-8'))
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = run(gp, d)
        assert buf.getvalue().startswith('schematic data_version 2026-09-24\n'), buf.getvalue()[:80]
        assert rc == 1   # 只建了 sydneytrains 库：metro / lightrail 两条「（跳过）」算告警
        n += 1
        # 颜色不一致
        bad = copy.deepcopy(good)
        bad['lines'][0]['colour'] = '#000000'
        assert any(x.startswith('颜色 T9') for x in static_warnings(bad, d))
        n += 1
        # 连通：共用 b 的两段 path 不告警；完全分开的两段告警
        bad = copy.deepcopy(good)
        bad['lines'][0]['paths'] = [{'stations': ['a', 'b']}, {'stations': ['c', 'b']}]
        assert not any(x.startswith('连通') for x in static_warnings(bad, d))
        bad['stations'].append({'id': 'z', 'parents': {}, 'x': 900, 'y': 900,
                                'label': {'text': 'Z', 'anchor': 'e'}})
        bad['lines'][0]['paths'] = [{'stations': ['a', 'b']}, {'stations': ['c', 'z']}]
        assert any(x.startswith('连通 T9') for x in static_warnings(bad, d))
        n += 1
        # 布局：b(200,100) → c(300,200) 是 45°；c 挪到 (300,150) 就不是；加折点 (300,100) 变成 0°+90° 又合规
        bad = copy.deepcopy(good)
        bad['stations'][2]['y'] = 150
        assert any(x.startswith('布局 T9') for x in static_warnings(bad, d))
        bad['lines'][0]['paths'][0]['via'] = {'1': [[300, 100]]}
        assert not any(x.startswith('布局') for x in static_warnings(bad, d))
        n += 1
        # 结构错误（退出码 2 那一类）
        for mutate, expect in (
                (lambda x: x.update(format=2), 'format'),
                (lambda x: x['lines'][0]['paths'][0]['stations'].append('nope'), '不在 stations'),
                (lambda x: x['lines'][0]['paths'][0].update(via={'2': [[1, 1]]}), '不是 0..1 的下标'),
                (lambda x: x['stations'][0]['label'].update(anchor='x'), 'label.anchor'),
                (lambda x: x['lines'][0].update(colour='red'), '不是 #RRGGBB'),
                (lambda x: x['stations'][1].update(x=5000), 'bounds 外'),
                (lambda x: x['stations'].append(copy.deepcopy(x['stations'][0])), '站 id 重复'),
        ):
            bad = copy.deepcopy(good)
            mutate(bad)
            errs = structural_errors(bad)
            assert any(expect in e for e in errs), (expect, errs)
            n += 1
    return n


if __name__ == '__main__':
    if sys.argv[1:] == ['--selftest']:
        print('check_schematic selftest ok，%d 组用例' % selftest())
        sys.exit(0)
    if len(sys.argv) != 3:
        print(__doc__)
        sys.exit(2)
    sys.exit(run(sys.argv[1], sys.argv[2]))
