#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""M6 §6.3 · 公交区表（F5「按区找站」）。每日一步，**纯标准库**（CI 只装 jsonschema）。

  python suburbs.py <build_dir> <sal2021_nsw.json.gz>
  python suburbs.py --selftest

1. 校验派生文件的 sha256 == DERIVED_SHA256（派生文件由主仓 `pipeline/tools/derive_sal.py` 一次性产出，
   入 data 仓 `geo/`；每日流程**不下载** ABS 原始 zip）。
2. 取 `<build_dir>/buses.sqlite` 的父站（location_type = 1），bbox 预筛 + 射线法（偶奇规则，整数 1e-5° 坐标，
   精确无浮点误差）归区。落在公共边 / 公共顶点上的站按半开规则恰好归一个区；多个区都命中（抽稀造成的
   细小重叠）取 suburb_id 最小者；一个都不中就不写行。
3. 写两张表（schema 仍是 2，只加表不改表，D20），VACUUM，**重新 gzip**（manifest 的 sha 取 .sqlite.gz，
   不重压就对不上新表），更新 build_stats.json 的 buses 行。
"""
import gzip
import hashlib
import json
import os
import sqlite3
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

# 派生文件 geo/sal2021_nsw.json.gz 的 sha256（M6 P3 Task 7 Step 5 钉入）。None = 还没派生，apply() 拒绝运行。
DERIVED_SHA256 = 'f5beb08a56b85460f01d0247ca0fc6766f18f1725938087002cd186b94453bf5'
FORMAT = 1

DDL = '''
CREATE TABLE suburbs (
  suburb_id INTEGER PRIMARY KEY,   -- ABS SAL_CODE21（跨版本稳定）
  name      TEXT NOT NULL          -- 已去掉「(NSW)」类后缀
);
CREATE TABLE stop_suburbs (
  suburb_id INTEGER NOT NULL REFERENCES suburbs(suburb_id),
  stop_id   TEXT    NOT NULL REFERENCES stops(stop_id),
  PRIMARY KEY (suburb_id, stop_id)
) WITHOUT ROWID;
'''


def sha256_file(p):
    h = hashlib.sha256()
    with open(p, 'rb') as f:
        for b in iter(lambda: f.read(1 << 20), b''):
            h.update(b)
    return h.hexdigest()


def load(path, expect_sha):
    """-> [{'id': int, 'name': str, 'bbox': [lat0, lon0, lat1, lon1], 'rings': [[lat, lon, lat, lon, …], …]}]（1e-5° 整数）"""
    got = sha256_file(path)
    if got != expect_sha:
        raise ValueError('派生文件 sha256=%s ≠ 钉死的 %s（文件被改过？重派生要同步改 DERIVED_SHA256）' % (got, expect_sha))
    doc = json.loads(gzip.decompress(open(path, 'rb').read()).decode('utf-8'))
    if doc.get('format') != FORMAT:
        raise ValueError('派生文件 format=%r，本脚本只认 %d' % (doc.get('format'), FORMAT))
    return doc['suburbs']


def point_in_rings(lat, lon, rings):
    """偶奇规则射线法，射线朝 +lon。坐标全是整数，用交叉相乘避免除法，结果精确。
    半开规则 `(yi > y) != (yj > y)` 让公共边 / 公共顶点上的点恰好归一侧。"""
    inside = False
    for r in rings:
        n = len(r) // 2
        j = n - 1
        for i in range(n):
            yi, xi = r[2 * i], r[2 * i + 1]
            yj, xj = r[2 * j], r[2 * j + 1]
            if (yi > lat) != (yj > lat):
                dy = yj - yi
                lhs = (lon - xi) * dy
                rhs = (lat - yi) * (xj - xi)
                if (lhs < rhs) if dy > 0 else (lhs > rhs):
                    inside = not inside
            j = i
    return inside


def assign(stops, suburbs, prefilter=True):
    """stops = [(stop_id, lat_e5, lon_e5)] -> ({stop_id: suburb_id}, {'unassigned': n, 'multi': n})。
    prefilter=False 只给阴性对照用：去掉 bbox 预筛，结果必须逐站相同。"""
    subs = sorted((s for s in suburbs if s['rings']), key=lambda s: s['id'])
    out, unassigned, multi = {}, 0, 0
    for sid, lat, lon in stops:
        hits = []
        for s in subs:
            if prefilter:
                b = s['bbox']
                if not (b[0] <= lat <= b[2] and b[1] <= lon <= b[3]):
                    continue
            if point_in_rings(lat, lon, s['rings']):
                hits.append(s['id'])
        if not hits:
            unassigned += 1
            continue
        if len(hits) > 1:
            multi += 1
        out[sid] = hits[0]
    return out, {'unassigned': unassigned, 'multi': multi}


def e5(x):
    return int(round(x * 1e5))


def write_tables(db_path, suburbs, mapping):
    """在已建好的 buses.sqlite 上加两张表（重跑时先删旧表），只收 ≥ 1 站的区。-> (区数, 行数)"""
    names = {s['id']: s['name'] for s in suburbs}
    used = sorted(set(mapping.values()))
    db = sqlite3.connect(db_path)
    db.execute('PRAGMA foreign_keys = OFF')
    db.executescript('DROP TABLE IF EXISTS stop_suburbs; DROP TABLE IF EXISTS suburbs;' + DDL)
    db.executemany('INSERT INTO suburbs VALUES (?, ?)', [(i, names[i]) for i in used])
    db.executemany('INSERT INTO stop_suburbs VALUES (?, ?)', sorted((v, k) for k, v in mapping.items()))
    db.commit()
    fk = db.execute('PRAGMA foreign_key_check').fetchall()
    if fk:
        db.close()
        raise ValueError('区表 foreign_key_check 违规 %d 条：%s' % (len(fk), fk[:3]))
    db.execute('PRAGMA journal_mode = DELETE')
    db.execute('VACUUM')
    db.commit()
    db.close()
    return len(used), len(mapping)


def apply(build_dir, geo_path, expect_sha=None):
    import build_db  # noqa: E402  gzip 口径（mtime=0，可复现）的唯一实现
    expect_sha = expect_sha or DERIVED_SHA256
    if not expect_sha:
        raise SystemExit('FATAL: suburbs.DERIVED_SHA256 未钉（M6 P3 Task 7 Step 5 没做）')
    db_path = os.path.join(build_dir, 'buses.sqlite')
    if not os.path.exists(db_path):
        print('SKIP 区表：%s 不存在（本次没建 buses）' % db_path)
        return None
    suburbs = load(geo_path, expect_sha)
    db = sqlite3.connect('file:%s?immutable=1' % db_path.replace('\\', '/'), uri=True)
    stops = [(sid, e5(lat), e5(lon)) for sid, lat, lon in
             db.execute('SELECT stop_id, lat, lon FROM stops WHERE location_type = 1 ORDER BY stop_id')]
    db.close()
    mapping, st = assign(stops, suburbs)
    n_sub, n_rows = write_tables(db_path, suburbs, mapping)
    gz = build_db.gzip_file(db_path)
    stats_p = os.path.join(build_dir, 'build_stats.json')
    if os.path.exists(stats_p):
        stats = json.load(open(stats_p, encoding='utf-8'))
        if 'buses' in stats:
            stats['buses'].update(sqlite_bytes=os.path.getsize(db_path), gz_bytes=os.path.getsize(gz),
                                  suburbs=n_sub, stop_suburbs=n_rows,
                                  suburb_unassigned=st['unassigned'], suburb_multi=st['multi'])
            json.dump(stats, open(stats_p, 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
    rate = n_rows / len(stops) if stops else 0.0
    print('buses 区表  区 %d / 站 %d / 归属 %.2f%%（未归 %d，多中 %d）  sqlite %.2f MB  gz %.2f MB'
          % (n_sub, len(stops), rate * 100, st['unassigned'], st['multi'],
             os.path.getsize(db_path) / 1e6, os.path.getsize(gz) / 1e6), flush=True)
    return {'suburbs': n_sub, 'rows': n_rows, 'stops': len(stops), **st}


# ---------------- 自检（人造多边形，不碰真数据） ----------------
def _square(lat0, lon0, lat1, lon1):
    return [lat0, lon0, lat0, lon1, lat1, lon1, lat1, lon0, lat0, lon0]


def _sub(i, name, *rings):
    pts = [v for r in rings for v in r]
    lats, lons = pts[0::2], pts[1::2]
    return {'id': i, 'name': name, 'bbox': [min(lats), min(lons), max(lats), max(lons)], 'rings': list(rings)}


def _mini_buses_db(path, stops):
    db = sqlite3.connect(path)
    db.executescript(open(os.path.join(HERE, 'schema.sql'), encoding='utf-8').read())
    db.execute("INSERT INTO meta VALUES (1, 2, 'buses', '20260925', 20260925, 20261025, '2026-09-25T00:00:00Z')")
    db.executemany('INSERT INTO stops(stop_id, name, name_normalized, lat, lon, location_type) VALUES (?,?,?,?,?,1)',
                   [(sid, sid, sid.lower(), lat / 1e5, lon / 1e5) for sid, lat, lon in stops])
    db.commit()
    db.close()


def selftest():
    import random
    n = 0
    A = _sub(10, 'A', _square(0, 0, 100, 100))
    B = _sub(20, 'B', _square(0, 100, 100, 200))
    C = _sub(30, 'C', _square(100, 0, 200, 100))
    D = _sub(40, 'D', _square(100, 100, 200, 200))
    subs = [A, B, C, D]
    # 内部 / 外部
    m, st = assign([('in', 50, 50), ('out', 500, 500)], subs)
    assert m == {'in': 10} and st == {'unassigned': 1, 'multi': 0}, (m, st)
    n += 1
    # 公共边（lon = 100 这条竖边）上的点恰好归一个区（半开规则 → 右侧 B）；公共顶点（100, 100）恰好归 D
    m, _ = assign([('edge', 50, 100), ('corner', 100, 100)], subs)
    assert m == {'edge': 20, 'corner': 40}, m
    n += 1
    # 带洞：洞里的点不归这个区；环的方向无关
    ring_out = _square(0, 300, 100, 400)
    sq = _square(40, 340, 60, 360)
    hole = [v for k in range(len(sq) // 2 - 1, -1, -1) for v in sq[2 * k:2 * k + 2]]   # 反向环（按点反转，不是按数反转）
    H = _sub(50, 'H', ring_out, hole)
    m, st = assign([('hole', 50, 350), ('donut', 10, 310)], [H])
    assert m == {'donut': 50} and st['unassigned'] == 1, (m, st)
    n += 1
    # 空几何的区（rings = []）永不命中、不崩
    E = {'id': 5, 'name': 'Empty', 'bbox': [0, 0, 0, 0], 'rings': []}
    m, _ = assign([('in', 50, 50)], [E, A])
    assert m == {'in': 10}, m
    n += 1
    # 抽稀造成的重叠：两个区都中 → 取 id 小者，计 multi
    A2 = _sub(15, 'A2', _square(0, 0, 60, 60))
    m, st = assign([('both', 10, 10)], [A2, A])
    assert m == {'both': 10} and st['multi'] == 1, (m, st)
    n += 1
    # 阴性对照（spec §8）：去掉 bbox 预筛，逐站结果必须完全相同
    rnd = random.Random(20260925)
    grid = []
    for gi in range(10):
        for gj in range(10):
            rings = [_square(gi * 100, gj * 100, gi * 100 + 100, gj * 100 + 100)]
            if (gi + gj) % 3 == 0:
                rings.append(_square(gi * 100 + 30, gj * 100 + 30, gi * 100 + 70, gj * 100 + 70))
            grid.append(_sub(1000 + gi * 10 + gj, 'G%d%d' % (gi, gj), *rings))
    pts = [('p%d' % k, rnd.randint(-50, 1050), rnd.randint(-50, 1050)) for k in range(2000)]
    pts += [('e%d' % k, rnd.randrange(0, 1001, 100), rnd.randint(0, 1000)) for k in range(200)]   # 正好在格线上
    with_pf, s1 = assign(pts, grid, prefilter=True)
    without, s2 = assign(pts, grid, prefilter=False)
    assert with_pf == without and s1 == s2, 'bbox 预筛改变了结果'
    n += 1
    with tempfile.TemporaryDirectory() as d:
        # sha 不符 → 拒绝
        doc = {'format': 1, 'suburbs': [A, B]}
        geo = os.path.join(d, 'g.json.gz')
        with gzip.GzipFile(geo, 'wb', mtime=0) as f:
            f.write(json.dumps(doc).encode('utf-8'))
        try:
            load(geo, '0' * 64)
        except ValueError:
            pass
        else:
            raise AssertionError('sha 不符没有报错')
        good_sha = sha256_file(geo)
        assert [s['id'] for s in load(geo, good_sha)] == [10, 20]
        n += 1
        # 端到端：写表 + 重 gzip + schema 仍是 2 + FK 全有效 + 只收有站的区 + 重跑幂等
        dbp = os.path.join(d, 'buses.sqlite')
        _mini_buses_db(dbp, [('s1', 50, 50), ('s2', 50, 150), ('s3', 900, 900)])
        json.dump({'buses': {'sqlite_bytes': 0, 'gz_bytes': 0}}, open(os.path.join(d, 'build_stats.json'), 'w'))
        r = apply(d, geo, good_sha)
        assert r['suburbs'] == 2 and r['rows'] == 2 and r['unassigned'] == 1, r
        db = sqlite3.connect(dbp)
        assert db.execute('SELECT schema_version FROM meta').fetchone()[0] == 2
        assert db.execute('PRAGMA user_version').fetchone()[0] == 2
        assert db.execute('PRAGMA journal_mode').fetchone()[0] == 'delete'
        assert db.execute('PRAGMA foreign_key_check').fetchall() == []
        assert db.execute("SELECT sql FROM sqlite_master WHERE name='stop_suburbs'").fetchone()[0].rstrip().endswith('WITHOUT ROWID')
        assert db.execute('SELECT * FROM suburbs ORDER BY suburb_id').fetchall() == [(10, 'A'), (20, 'B')]
        db.close()
        g1 = open(dbp + '.gz', 'rb').read()
        assert gzip.decompress(g1) == open(dbp, 'rb').read(), '.gz 必须是加表之后的库'
        r2 = apply(d, geo, good_sha)                       # 重跑幂等：先删旧表再建，内容相同，.gz 仍是新库
        assert r2 == r, (r2, r)
        assert gzip.decompress(open(dbp + '.gz', 'rb').read()) == open(dbp, 'rb').read()
        g1 = open(dbp + '.gz', 'rb').read()
        st = json.load(open(os.path.join(d, 'build_stats.json')))['buses']
        assert st['stop_suburbs'] == 2 and st['gz_bytes'] == len(g1), st
        n += 1
    return n


if __name__ == '__main__':
    if sys.argv[1:] == ['--selftest']:
        print('suburbs selftest ok，%d 组' % selftest())
        sys.exit(0)
    if len(sys.argv) != 3:
        print(__doc__)
        sys.exit(2)
    apply(sys.argv[1], sys.argv[2])
