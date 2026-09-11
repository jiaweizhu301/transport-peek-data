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
from gtfs_modes import API_BASE, MODES, PUBLISH_MODES


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
    url = API_BASE + MODES[mode]['path']
    req = urllib.request.Request(url, headers={
        'Authorization': 'apikey ' + key,
        'Accept': 'application/octet-stream',
    })
    zip_path = os.path.join(out_dir, mode + '.zip')
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
    with open(os.path.join(out_dir, mode + '.version'), 'w', encoding='utf-8') as f:
        f.write(ver + '\n')
    print('%-13s %8.2f MB  static_version=%s' % (mode, os.path.getsize(zip_path) / 1e6, ver), flush=True)
    return zip_path, ver


if __name__ == '__main__':
    out = sys.argv[1]
    modes = sys.argv[2:] or PUBLISH_MODES
    os.makedirs(out, exist_ok=True)
    k = api_key()
    for m in modes:
        fetch(m, out, k)
