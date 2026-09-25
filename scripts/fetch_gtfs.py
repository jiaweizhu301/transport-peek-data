#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""下载 TfNSW GTFS 静态 zip。

  python fetch_gtfs.py <out_dir> [mode ...]

API key 取自（按优先级）：环境变量 TFNSW_API_KEY，或 ~/.transportpeek/tfnsw.key。
key 永不打印、永不写盘。static_version 取 zip 的 Last-Modified → YYYYMMDD-hhmm，写在 <mode>.version。
"""
import os
import sys
import email.utils
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from gtfs_modes import API_BASE, MODES, PUBLISH_MODES, feeds_for


def api_key():
    k = os.environ.get('TFNSW_API_KEY')
    if k:
        return k.strip()
    p = os.path.expanduser('~/.transportpeek/tfnsw.key')
    if os.path.exists(p):
        return open(p, encoding='utf-8').read().strip()
    raise SystemExit('FATAL: no TFNSW_API_KEY (env or ~/.transportpeek/tfnsw.key)')


def static_version(last_modified):
    """Last-Modified (RFC 2822) -> YYYYMMDD-hhmm（契约 manifest.schema.json 的 pattern）。"""
    if not last_modified:
        return None
    dt = email.utils.parsedate_to_datetime(last_modified)
    return dt.strftime('%Y%m%d-%H%M')


def fetch(mode, out_dir, key):
    """下该 mode 的全部上游静态 feed。多 feed 的 mode（lightrail）落成
    `<mode>.0.zip / <mode>.1.zip`，并把各自的 Last-Modified 取**最新**那个当 static_version。
    取最新而不是拼接：static_version 要塞进 manifest 的 pattern，且客户端只用它做「变没变」的比对。"""
    feeds = feeds_for(mode)
    if len(feeds) > 1:
        zips, vers = [], []
        for i, sub in enumerate(feeds):
            zp, v = _fetch_one(mode, sub, os.path.join(out_dir, '%s.%d.zip' % (mode, i)), key)
            zips.append(zp)
            vers.append(v)
        ver = max(vers)
        with open(os.path.join(out_dir, mode + '.version'), 'w', encoding='utf-8') as f:
            f.write(ver + chr(10))
        print('%-13s %d 个子 feed 合计 %8.2f MB  static_version=%s'
              % (mode, len(zips), sum(os.path.getsize(z) for z in zips) / 1e6, ver), flush=True)
        return zips, ver
    zp, ver = _fetch_one(mode, feeds[0], os.path.join(out_dir, mode + '.zip'), key)
    with open(os.path.join(out_dir, mode + '.version'), 'w', encoding='utf-8') as f:
        f.write(ver + chr(10))
    print('%-13s %8.2f MB  static_version=%s' % (mode, os.path.getsize(zp) / 1e6, ver), flush=True)
    return [zp], ver


def _fetch_one(mode, path, zip_path, key):
    url = API_BASE + path
    req = urllib.request.Request(url, headers={
        'Authorization': 'apikey ' + key,
        'Accept': 'application/octet-stream',
    })
    with urllib.request.urlopen(req, timeout=300) as r:
        lm = r.headers.get('Last-Modified')
        with open(zip_path, 'wb') as f:
            while True:
                chunk = r.read(1 << 20)
                if not chunk:
                    break
                f.write(chunk)
    ver = static_version(lm)
    if not ver:
        # 上游没给 Last-Modified 时退回下载日（仍满足 schema pattern）
        import datetime
        ver = datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%d-%H%M')
    # ⚠ 这里**不写 .version、也不打印**：一个 mode 可能有多条子 feed，版本号由 fetch()
    # 取最新那个之后统一写一次。M4-4.3 的重构（4c633ef）把原来的 fetch() 拆成
    # fetch() + _fetch_one()，旧函数尾部这两件事被留在了 _fetch_one 里，而 out_dir
    # 不是它的参数 —— 于是**每次下载完都 NameError**，zip 落了盘、.version 没生成。
    # 实测形态（2026-09-22 拉 buses）：97,985,387 字节的 buses.zip 在，buses.version 不在，
    # 而 `| tail` 之后 shell 报的还是 exit 0，所以不看 stderr 根本发现不了。
    # data 仓 scripts/fetch_gtfs.py 是重构**之前**的单 feed 版本，所以日常 run 没被打挂 ——
    # 但 4.3 的管线改动同步过去的那天会一起带过去，届时每天的 run 都会失败。
    return zip_path, ver


if __name__ == '__main__':
    out = sys.argv[1]
    modes = sys.argv[2:] or PUBLISH_MODES
    os.makedirs(out, exist_ok=True)
    k = api_key()
    for m in modes:
        fetch(m, out, k)
