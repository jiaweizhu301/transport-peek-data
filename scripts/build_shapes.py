#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""M6 §6.1 · 按 mode 输出 `shapes.<mode>.bin`（+ `.gz`）。

  python build_shapes.py <gtfs_dir> <build_dir> [modes...]
  python build_shapes.py --selftest

- 只收 `<build_dir>/<mode>.sqlite` 里**真的留下来的** trip（agency / route_type / 非营运 / 日历窗口
  的裁剪全部以 build_db 的产物为准，这里不重写一遍口径）。trip → shape_id 取上游 trips.txt
  （库里没有 shape_id 列，schema 2 一个字不动）。
- 每个 (route_id, direction_id) 取最常见的 shape（shapes.pick_shapes），Douglas-Peucker 5 m。
- 编码见 shapes.py 模块头的字节契约。`.gz` 用 mtime=0，同输入同字节（sha256 可复现）。
"""
import csv
import gzip
import io
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import shapes  # noqa: E402
from gtfs_modes import PUBLISH_MODES, feeds_for  # noqa: E402


def _zip_paths(gtfs_dir, mode):
    n = len(feeds_for(mode))
    return ([os.path.join(gtfs_dir, '%s.%d.zip' % (mode, i)) for i in range(n)]
            if n > 1 else [os.path.join(gtfs_dir, mode + '.zip')])


def _rows(zips, name):
    for p in zips:
        with zipfile.ZipFile(p) as z:
            if name not in z.namelist():
                continue
            with z.open(name) as fh:
                for r in csv.DictReader(io.TextIOWrapper(fh, encoding='utf-8-sig')):
                    yield r


def build(zips, db_path, out_path):
    db = sqlite3.connect('file:%s?immutable=1' % db_path.replace('\\', '/'), uri=True)
    kept = {tid: (rid, did) for tid, rid, did in
            db.execute('SELECT trip_id, route_id, direction_id FROM trips')}
    db.close()
    shape_of = {}
    for t in _rows(zips, 'trips.txt'):
        if t['trip_id'] in kept:
            shape_of[t['trip_id']] = (t.get('shape_id') or '').strip()
    picked = shapes.pick_shapes([(rid, did, shape_of.get(tid, '')) for tid, (rid, did) in kept.items()])
    wanted = {sid for entries in picked.values() for _, sid in entries}
    pts = {}
    for r in _rows(zips, 'shapes.txt'):
        sid = r['shape_id']
        if sid in wanted:
            pts.setdefault(sid, []).append((int(r['shape_pt_sequence']),
                                            float(r['shape_pt_lat']), float(r['shape_pt_lon'])))
    missing = sorted(wanted - set(pts))
    if missing:
        # trips.txt 引用了 shapes.txt 里没有的 shape：丢掉这些索引项，并响亮地记一笔
        print('WARN  %d 个 shape 在 shapes.txt 里找不到：%s' % (len(missing), ', '.join(missing[:5])))
        picked = {rid: [(d, s) for d, s in e if s not in missing] for rid, e in picked.items()}
        picked = {rid: e for rid, e in picked.items() if e}
    raw_points = kept_points = 0
    blobs = {}
    for sid, seq in pts.items():
        seq.sort()
        line = [(lat, lon) for _, lat, lon in seq]
        simp = shapes.simplify(line)
        raw_points += len(line)
        kept_points += len(simp)
        blobs[sid] = shapes.encode_points(shapes.to_e5(simp))
    data = shapes.encode_file(picked, blobs)
    with open(out_path, 'wb') as f:
        f.write(data)
    with open(out_path, 'rb') as fi, gzip.GzipFile(out_path + '.gz', 'wb', compresslevel=9, mtime=0) as fo:
        shutil.copyfileobj(fi, fo)
    return {'routes': len(picked), 'shapes': len(blobs), 'raw_points': raw_points,
            'dp_points': kept_points, 'bin_bytes': len(data),
            'gz_bytes': os.path.getsize(out_path + '.gz')}


def asset_errors(build_dir, mode, entry, feed_static_version):
    """validate_assets 用：manifest.shapes[mode] 与盘上文件、与该 mode 的库逐项对账。-> 错误列表"""
    import hashlib
    gz = os.path.join(build_dir, 'shapes.%s.bin.gz' % mode)
    if not os.path.exists(gz):
        return ['%s: manifest 有 shapes 项，但缺 %s' % (mode, os.path.basename(gz))]
    raw = open(gz, 'rb').read()
    errs = []
    if entry['size_bytes'] != len(raw):
        errs.append('%s: shapes size_bytes 与实际不符' % mode)
    if entry['sha256'] != hashlib.sha256(raw).hexdigest():
        errs.append('%s: shapes sha256 与实际不符' % mode)
    if entry['static_version'] != feed_static_version:
        errs.append('%s: shapes static_version=%s ≠ feeds 的 %s' % (mode, entry['static_version'], feed_static_version))
    try:
        buf = gzip.decompress(raw)
    except (OSError, EOFError) as e:   # 被截断 / 尾部多了字节（gzip 把尾随垃圾当成坏的下一个 member）
        return errs + ['%s: shapes gz 解压失败：%s' % (mode, e)]
    if entry['bin_bytes'] != len(buf):
        errs.append('%s: shapes bin_bytes 与解压后不符' % mode)
    try:
        idx, at = shapes.decode_index(buf)
        for entries in idx.values():
            for _, _, off, ln in entries:
                shapes.decode_points(buf[at + off:at + off + ln])
    except ValueError as e:
        return errs + ['%s: shapes 文件解不开：%s' % (mode, e)]
    db = sqlite3.connect('file:%s?immutable=1' % os.path.join(build_dir, mode + '.sqlite').replace('\\', '/'),
                         uri=True)
    known = {r[0] for r in db.execute('SELECT route_id FROM routes')}
    db.close()
    extra = sorted(set(idx) - known)
    if extra:
        errs.append('%s: shapes 索引里有库里没有的 route %d 条：%s' % (mode, len(extra), ', '.join(extra[:5])))
    return errs


def main():
    gtfs_dir, build_dir = sys.argv[1], sys.argv[2]
    modes = sys.argv[3:] or PUBLISH_MODES
    stats = {}
    for mode in modes:
        st = build(_zip_paths(gtfs_dir, mode), os.path.join(build_dir, mode + '.sqlite'),
                   os.path.join(build_dir, 'shapes.%s.bin' % mode))
        stats[mode] = st
        print('%-13s shapes routes=%d shapes=%d points %d→%d  bin %.1f KB  gz %.1f KB'
              % (mode, st['routes'], st['shapes'], st['raw_points'], st['dp_points'],
                 st['bin_bytes'] / 1e3, st['gz_bytes'] / 1e3), flush=True)
    with open(os.path.join(build_dir, 'shapes_stats.json'), 'w', encoding='utf-8') as f:
        json.dump(stats, f, ensure_ascii=False, indent=1)


def selftest():
    with tempfile.TemporaryDirectory() as d:
        z = os.path.join(d, 'sydneytrains.zip')
        with zipfile.ZipFile(z, 'w') as zf:
            zf.writestr('trips.txt', 'route_id,service_id,trip_id,shape_id,direction_id\n'
                                     'R1,S,t1,SH_A,0\nR1,S,t2,SH_A,0\nR1,S,t3,SH_B,0\n'
                                     'R1,S,t4,SH_C,1\nR1,S,t_gone,SH_Z,0\n'
                                     'R2,S,t5,SH_MISSING,0\nR2,S,t6,,1\n')
            zf.writestr('shapes.txt', 'shape_id,shape_pt_lat,shape_pt_lon,shape_pt_sequence\n'
                                      'SH_A,-33.8,151.000,1\nSH_A,-33.8,151.002,3\nSH_A,-33.80001,151.001,2\n'
                                      'SH_B,-33.9,151.0,1\nSH_B,-33.9,151.1,2\n'
                                      'SH_C,-33.8,151.002,1\nSH_C,-33.8,151.0,2\n')
        dbp = os.path.join(d, 'sydneytrains.sqlite')
        db = sqlite3.connect(dbp)
        db.executescript(open(os.path.join(HERE, 'schema.sql'), encoding='utf-8').read())
        # t_gone 不在库里（被 build_db 裁掉的那种）→ 它的 SH_Z 不许进文件
        db.executemany('INSERT INTO trips VALUES (?,?,?,?,?,?,?,?,?,?)',
                       [(t, 'R1', 'S', None, di, 1, 1, 0, 0, b'') for t, di in
                        (('t1', 0), ('t2', 0), ('t3', 0), ('t4', 1))] +
                       [(t, 'R2', 'S', None, di, 1, 1, 0, 0, b'') for t, di in (('t5', 0), ('t6', 1))])
        db.execute("INSERT INTO routes VALUES ('R1','AG','X',NULL,2,NULL,NULL)")
        db.execute("INSERT INTO routes VALUES ('R2','AG','Y',NULL,2,NULL,NULL)")
        db.commit()
        db.close()
        out = os.path.join(d, 'shapes.sydneytrains.bin')
        st = build([z], dbp, out)
        buf = open(out, 'rb').read()
        idx, at = shapes.decode_index(buf)
        # R2 的 shape 在 shapes.txt 里没有、另一方向干脆没 shape_id → R2 整条不进索引，不崩
        assert idx.keys() == {'R1'}, idx
        entries = {(dirn, sid) for dirn, sid, _, _ in idx['R1']}
        assert entries == {(0, 'SH_A'), (1, 'SH_C')}, entries   # 方向 0 取 2 票的 SH_A
        a = [e for e in idx['R1'] if e[1] == 'SH_A'][0]
        got = shapes.decode_points(buf[at + a[2]:at + a[2] + a[3]])
        # shape_pt_sequence 乱序也按序号排；中点偏 1.1 m < 5 m 被 DP 删掉
        assert got == [(-3380000, 15100000), (-3380000, 15100200)], got
        assert st['raw_points'] == 5 and st['dp_points'] == 4, st
        # .gz 可复现：同输入两次，字节相同
        g1 = open(out + '.gz', 'rb').read()
        build([z], dbp, out)
        assert open(out + '.gz', 'rb').read() == g1
        # asset_errors：好的一份零错误；sha 不对 / 缺文件 / 版本不符 / 索引里有库里没有的 route 各报一条
        import hashlib
        good = {'url': 'x', 'sha256': hashlib.sha256(g1).hexdigest(), 'size_bytes': len(g1),
                'bin_bytes': len(buf), 'format': 1, 'static_version': 'V1', 'compression': 'gzip'}
        assert asset_errors(d, 'sydneytrains', good, 'V1') == []
        assert any('sha256' in e for e in asset_errors(d, 'sydneytrains', dict(good, sha256='0' * 64), 'V1'))
        assert any('static_version' in e for e in asset_errors(d, 'sydneytrains', good, 'V2'))
        assert any('缺' in e for e in asset_errors(d, 'metro', good, 'V1'))
        db = sqlite3.connect(dbp)
        db.execute("DELETE FROM routes WHERE route_id = 'R1'")
        db.commit()
        db.close()
        assert any('R1' in e for e in asset_errors(d, 'sydneytrains', good, 'V1'))
    return 1


if __name__ == '__main__':
    if sys.argv[1:] == ['--selftest']:
        print('build_shapes selftest ok，%d 组' % selftest())
        sys.exit(0)
    main()
