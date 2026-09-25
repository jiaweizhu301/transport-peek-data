# -*- coding: utf-8 -*-
"""`shapes.<mode>.bin` 的编解码 + Douglas-Peucker 抽稀 + 「每方向最常见 shape」（M6 §6.1）。

**这是唯一的 Python 实现**：build_shapes.py 编、validate_assets.py 解。Kotlin 侧
（core-map 的 `ShapesFile`）必须逐字节读出同样的结果 —— 两边的样例共用 `selftest()` 里
钉死的十六进制串（`ShapesFileTest` 逐字照抄）。改格式 = 改 FORMAT，并同步两边。

## 字节契约（FORMAT = 1）
所有整数是 unsigned LEB128 varint（下称 uv），`str` = uv 字节长 + UTF-8。

    magic      4 B   b'TPSH'
    format     1 B   = 1
    header_len uv    = 下面「索引」的字节数（payload 从 magic 起第 5 + len(uv) + header_len 字节开始）
    索引：
      n_routes uv
      按 route_id 升序，每条：
        route_id  str
        n_entries uv
        按 (direction, shape_id) 升序，每条：
          direction 1 B   0 / 1 = GTFS direction_id；2 = 上游没给
          shape_id  str
          offset    uv    相对 payload 起点
          length    uv    该 shape blob 的字节数
    payload：各 shape blob 按 shape_id 升序拼接；同一 shape 被多条 route 引用时只存一次。
    shape blob：
      n_points uv
      每点两个 zigzag + uv：dlat、dlon，单位 1e-5°，相对上一点（首点相对 (0, 0)）

zigzag 与 tripoffsets.py 同一个定义（-1→1, 1→2）。
"""
import math

MAGIC = b'TPSH'
FORMAT = 1
DIR_UNKNOWN = 2
EARTH_R = 6371008.8
DP_TOLERANCE_M = 5.0


def _zigzag(n):
    return (n << 1) if n >= 0 else ((-n) << 1) - 1


def _unzigzag(u):
    return (u >> 1) ^ -(u & 1)


def _put_uvarint(out, n):
    if n < 0:
        raise ValueError('uvarint 不收负数：%d' % n)
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out.append(b | 0x80)
        else:
            out.append(b)
            return


def _get_uvarint(buf, i):
    shift = 0
    u = 0
    while True:
        if i >= len(buf):
            raise ValueError('varint 在偏移 %d 处截断' % i)
        b = buf[i]
        i += 1
        u |= (b & 0x7F) << shift
        if not (b & 0x80):
            return u, i
        shift += 7
        if shift > 63:
            raise ValueError('varint 超过 64 位')


def _put_str(out, s):
    b = s.encode('utf-8')
    _put_uvarint(out, len(b))
    out.extend(b)


def _get_str(buf, i):
    n, i = _get_uvarint(buf, i)
    if i + n > len(buf):
        raise ValueError('字符串越界')
    return bytes(buf[i:i + n]).decode('utf-8'), i + n


def to_e5(points):
    """[(lat, lon) 浮点度] -> [(lat_e5, lon_e5) 整数]。舍入误差 ≤ 0.5e-5°。"""
    return [(int(round(lat * 1e5)), int(round(lon * 1e5))) for lat, lon in points]


def encode_points(points_e5):
    out = bytearray()
    _put_uvarint(out, len(points_e5))
    plat = plon = 0
    for lat, lon in points_e5:
        _put_uvarint(out, _zigzag(lat - plat))
        _put_uvarint(out, _zigzag(lon - plon))
        plat, plon = lat, lon
    return bytes(out)


def decode_points(blob):
    n, i = _get_uvarint(blob, 0)
    out = []
    plat = plon = 0
    for _ in range(n):
        u, i = _get_uvarint(blob, i)
        plat += _unzigzag(u)
        u, i = _get_uvarint(blob, i)
        plon += _unzigzag(u)
        out.append((plat, plon))
    if i != len(blob):
        raise ValueError('点列解完还剩 %d 字节' % (len(blob) - i))
    return out


def encode_file(index, blobs):
    """index = {route_id: [(direction, shape_id), …]}；blobs = {shape_id: encode_points(...)}。"""
    payload = bytearray()
    where = {}
    for sid in sorted(blobs):
        where[sid] = (len(payload), len(blobs[sid]))
        payload.extend(blobs[sid])
    head = bytearray()
    _put_uvarint(head, len(index))
    for rid in sorted(index):
        entries = sorted(index[rid])
        _put_str(head, rid)
        _put_uvarint(head, len(entries))
        for direction, sid in entries:
            off, ln = where[sid]
            head.append(direction)
            _put_str(head, sid)
            _put_uvarint(head, off)
            _put_uvarint(head, ln)
    out = bytearray(MAGIC)
    out.append(FORMAT)
    _put_uvarint(out, len(head))
    out.extend(head)
    out.extend(payload)
    return bytes(out)


def decode_index(buf):
    """-> ({route_id: [(direction, shape_id, offset, length), …]}, payload 起点)。越界 / 格式不符就抛。"""
    if bytes(buf[:4]) != MAGIC:
        raise ValueError('magic 不是 TPSH')
    if len(buf) < 5 or buf[4] != FORMAT:
        raise ValueError('format 不是 %d' % FORMAT)
    hlen, i = _get_uvarint(buf, 5)
    payload_at = i + hlen
    n_routes, i = _get_uvarint(buf, i)
    index = {}
    for _ in range(n_routes):
        rid, i = _get_str(buf, i)
        n, i = _get_uvarint(buf, i)
        entries = []
        for _ in range(n):
            direction = buf[i]
            i += 1
            sid, i = _get_str(buf, i)
            off, i = _get_uvarint(buf, i)
            ln, i = _get_uvarint(buf, i)
            if payload_at + off + ln > len(buf):
                raise ValueError('%s/%s 越界' % (rid, sid))
            entries.append((direction, sid, off, ln))
        index[rid] = entries
    if i != payload_at:
        raise ValueError('header_len 与实际索引长度不符')
    return index, payload_at


def simplify(points, tol_m=DP_TOLERANCE_M):
    """Douglas-Peucker，等距投影（以首点纬度为基准），**迭代**实现（公交 shape 上千点，递归会爆栈）。"""
    n = len(points)
    if n <= 2:
        return list(points)
    lat0 = math.radians(points[0][0])
    k = math.pi / 180 * EARTH_R
    xy = [(lon * math.cos(lat0) * k, lat * k) for lat, lon in points]
    keep = [False] * n
    keep[0] = keep[-1] = True
    stack = [(0, n - 1)]
    while stack:
        a, b = stack.pop()
        ax, ay = xy[a]
        bx, by = xy[b]
        dx, dy = bx - ax, by - ay
        seg2 = dx * dx + dy * dy
        best, best_d = -1, -1.0
        for j in range(a + 1, b):
            px, py = xy[j]
            if seg2 == 0:
                d = math.hypot(px - ax, py - ay)
            else:
                t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / seg2))
                d = math.hypot(px - (ax + t * dx), py - (ay + t * dy))
            if d > best_d:
                best, best_d = j, d
        if best_d > tol_m:
            keep[best] = True
            stack.append((a, best))
            stack.append((best, b))
    return [p for p, kept in zip(points, keep) if kept]


def pick_shapes(trips):
    """trips = [(route_id, direction_id | None, shape_id | '')]。
    每个 (route_id, 方向) 取被最多 trip 用的 shape；平票取 shape_id 字典序最小（可复现）。
    -> {route_id: [(direction, shape_id), …]}"""
    count = {}
    for route_id, direction, shape_id in trips:
        if not shape_id:
            continue
        d = direction if direction in (0, 1) else DIR_UNKNOWN
        c = count.setdefault((route_id, d), {})
        c[shape_id] = c.get(shape_id, 0) + 1
    out = {}
    for (route_id, d), c in sorted(count.items()):
        sid = sorted(c.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]
        out.setdefault(route_id, []).append((d, sid))
    return out


# 与 core-map `ShapesFileTest` 共用的样例。**改这里就要同步改那边。**
SAMPLE_POINTS = [(-33.88390, 151.20580), (-33.87360, 151.20690), (-33.86590, 151.20580)]
SAMPLE_POINTS_HEX = '03CBCF9D0388E3B50E8C10DC01840CDB01'
SAMPLE_FILE_HEX = ('54505348012502064E534E5F31610100064E534E5F31610011064E534E5F32610101064E534E5F3161'
                   '001103CBCF9D0388E3B50E8C10DC01840CDB01')


def selftest():
    n = 0
    e5 = to_e5(SAMPLE_POINTS)
    assert e5 == [(-3388390, 15120580), (-3387360, 15120690), (-3386590, 15120580)], e5
    blob = encode_points(e5)
    assert blob.hex().upper() == SAMPLE_POINTS_HEX, blob.hex().upper()
    assert decode_points(blob) == e5
    n += 1
    # 往返误差 ≤ 1e-5°（M6 §8）
    for lat, lon in [(-33.123456789, 151.987654321), (-34.0000049, 150.0000051)]:
        (la, lo), = decode_points(encode_points(to_e5([(lat, lon)])))
        assert abs(la / 1e5 - lat) <= 1e-5 and abs(lo / 1e5 - lon) <= 1e-5
    n += 1
    assert encode_points([]).hex() == '00'
    assert encode_points([(0, 0), (1, -1)]).hex().upper() == '0200000201'
    n += 1
    f = encode_file({'NSN_1a': [(0, 'NSN_1a')], 'NSN_2a': [(1, 'NSN_1a')]}, {'NSN_1a': blob})
    assert f.hex().upper() == SAMPLE_FILE_HEX, f.hex().upper()
    idx, at = decode_index(f)
    assert idx == {'NSN_1a': [(0, 'NSN_1a', 0, 17)], 'NSN_2a': [(1, 'NSN_1a', 0, 17)]}, idx
    assert decode_points(f[at:at + 17]) == e5
    n += 1
    for broken in (b'XXXX' + f[4:], f[:4] + b'\x02' + f[5:], f[:-3]):
        try:
            idx2, at2 = decode_index(broken)
            for entries in idx2.values():
                for _, _, off, ln in entries:
                    decode_points(broken[at2 + off:at2 + off + ln])
        except ValueError:
            pass
        else:
            raise AssertionError('坏文件没有报错：%r' % broken[:8])
    n += 1
    # DP 5 m：中点偏 3 m 删掉，偏 7 m 留下
    m_per_deg = math.pi / 180 * EARTH_R
    for off_m, want in ((3.0, 2), (7.0, 3)):
        line = [(-33.8, 151.0), (-33.8 + off_m / m_per_deg, 151.001), (-33.8, 151.002)]
        assert len(simplify(line)) == want, (off_m, simplify(line))
    n += 1
    # 5 000 点的锯齿线不爆栈，且首末点永远保留
    zig = [(-33.8 + (0.0001 if i % 2 else 0), 151.0 + i * 0.0001) for i in range(5000)]
    s = simplify(zig)
    assert s[0] == zig[0] and s[-1] == zig[-1] and len(s) == 5000
    n += 1
    got = pick_shapes([('R', 0, 'a'), ('R', 0, 'b'), ('R', 0, 'b'), ('R', 1, 'c'), ('R', None, 'z'),
                       ('Q', 0, 'y'), ('Q', 0, 'x'), ('Q', 1, '')])
    assert got == {'Q': [(0, 'x')], 'R': [(0, 'b'), (1, 'c'), (2, 'z')]}, got
    n += 1
    return n


if __name__ == '__main__':
    print('shapes selftest ok，%d 组样例' % selftest())
