# -*- coding: utf-8 -*-
"""`trips.offsets` 的编解码（schema 2）。契约在 `schema.sql` 的「offsets 的字节契约」一段。

**这是唯一的 Python 实现**：build_db.py 编、invariants.py 与 golden_check.py 解、
fixtures/tools 两者都用。Kotlin 侧（core-gtfs）必须逐字节跑出同样的结果 ——
两个解码器不一致的失败形态是「时刻悄悄偏了几秒」，不会有任何报错，所以这里的
`selftest()` 与 core-gtfs 的对应单测用的是同一组样例。

格式（按 stop_sequence 升序，每站两个 zigzag + unsigned LEB128 varint）：
    v1 = 本站 arrival   − 上一站 departure     （首站的「上一站 departure」= trips.start_secs）
    v2 = 本站 departure − 本站 arrival
递推：arr = prev_dep + v1；dep = arr + v2；prev_dep = dep。

zigzag 是防线不是优化：上游偶有 arrival > departure 的脏数据，负数不能当 unsigned 编。
"""


def _zigzag(n):
    return (n << 1) ^ (n >> 63) if n < 0 else (n << 1)


def _unzigzag(u):
    return (u >> 1) ^ -(u & 1)


def _put_uvarint(out, n):
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out.append(b | 0x80)
        else:
            out.append(b)
            return


def encode(start_secs, stop_times):
    """stop_times = [(arrival_secs, departure_secs), ...]，按 stop_sequence 升序。→ bytes"""
    out = bytearray()
    prev_dep = start_secs
    for arr, dep in stop_times:
        _put_uvarint(out, _zigzag(arr - prev_dep))
        _put_uvarint(out, _zigzag(dep - arr))
        prev_dep = dep
    return bytes(out)


def decode(start_secs, blob, n_stops=None):
    """→ [(arrival_secs, departure_secs), ...]。

    `n_stops` 给了就校验站数 —— blob 与 pattern 对不上时必须**响亮**地炸，
    不能返回一个短了几站的列表让上层拿去当结果。
    """
    out = []
    prev_dep = start_secs
    i = 0
    n = len(blob)
    while i < n:
        vals = []
        for _ in range(2):
            shift = 0
            u = 0
            while True:
                if i >= n:
                    raise ValueError('offsets blob 在 varint 中间就结束了（偏移 %d/%d）' % (i, n))
                b = blob[i]
                i += 1
                u |= (b & 0x7F) << shift
                if not (b & 0x80):
                    break
                shift += 7
                if shift > 63:
                    raise ValueError('offsets varint 超过 64 位')
            vals.append(_unzigzag(u))
        arr = prev_dep + vals[0]
        dep = arr + vals[1]
        out.append((arr, dep))
        prev_dep = dep
    if n_stops is not None and len(out) != n_stops:
        raise ValueError('offsets 解出 %d 站，stop_patterns.n_stops 是 %d' % (len(out), n_stops))
    return out


def selftest():
    """与 core-gtfs 的 TripOffsetsTest 用同一组样例。改这里就要同步改那边。"""
    cases = [
        # (start_secs, [(arr, dep), ...])
        (28800, [(28800, 28800), (28920, 28950), (29100, 29100)]),   # 常规：站间 2 min、停 30 s
        (0, [(0, 0)]),                                               # 单站
        (86400, [(86400, 86400), (90000, 90000)]),                   # 24:00+ 仍是递增秒数
        (3600, [(3600, 3590), (3700, 3700)]),                        # 脏数据：arrival > departure → 负偏移
        (0, [(0, 0), (100000, 100000)]),                             # 大跨度，逼出多字节 varint
    ]
    for start, st in cases:
        blob = encode(start, st)
        got = decode(start, blob, n_stops=len(st))
        assert got == st, (start, st, got)
    # 站数不符必须炸
    blob = encode(28800, [(28800, 28800), (28900, 28900)])
    try:
        decode(28800, blob, n_stops=3)
    except ValueError:
        pass
    else:
        raise AssertionError('站数不符时 decode 没有报错')
    return len(cases)


if __name__ == '__main__':
    print('tripoffsets selftest ok，%d 组样例' % selftest())
