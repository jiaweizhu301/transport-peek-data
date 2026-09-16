#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""D3 · 生成 Release 资产：manifest.json / config.json / stop_parents.<mode>.json /
non_revenue_headsigns.json / parsing_rules.json。

  python gen_manifest.py <build_dir> --tag <tag> [--repo owner/name] [--channel stable|prerelease]

契约：`contracts/manifest.schema.json`（4a）与 `contracts/config.schema.json`（4b）。
客户端只认 `releases/latest/download/manifest.json`；`stop_parents.<mode>.json` 的形状对齐
`fixtures/stop_parents.sydneytrains.json`（E 流 Worker 直接吃这个文件）。

schema_version 变更 = 破坏性：`--channel prerelease` 发布时 GitHub 不动 `latest`（见 publish_release.sh）。
"""
import argparse
import datetime
import hashlib
import json
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from gtfs_modes import PUBLISH_MODES
import build_db

# manifest/config 的 schema_version（契约 const 1）
MANIFEST_SCHEMA_VERSION = 1
CONFIG_SCHEMA_VERSION = 1
# 最低可用 app versionCode。只在破坏性变更时抬高（同时 manifest 与 config 两处）。
MIN_SUPPORTED_APP_VERSION = 1

PAGES_BASE_DEFAULT = 'https://jiaweizhu301.github.io/transport-peek-data'


def sha256(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for b in iter(lambda: f.read(1 << 20), b''):
            h.update(b)
    return h.hexdigest()


def utcnow():
    return datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0) \
        .isoformat().replace('+00:00', 'Z')


def stop_parents(db_path, mode, static_version):
    """子站 -> 父站 + 站台号。形状与 fixtures/stop_parents.<mode>.json 逐字段一致。"""
    db = sqlite3.connect('file:%s?immutable=1' % db_path.replace('\\', '/'), uri=True)
    rows = db.execute(
        'SELECT stop_id, parent_station, platform_code FROM stops '
        'WHERE parent_station IS NOT NULL ORDER BY stop_id').fetchall()
    db.close()
    return {
        'mode': mode,
        'static_version': static_version,
        'generated_at': utcnow(),
        'note': 'sydneytrains 上游 stops.txt 无 platform_code 列，站台号由 pipeline 从 stop_name '
                '尾部 "Platform N" 解析。',
        'stops': [{'stop_id': a, 'parent_stop_id': b, 'platform_code': c} for a, b, c in rows],
    }


def feed_entry(build_dir, mode, base_url):
    gz = os.path.join(build_dir, mode + '.sqlite.gz')
    db = sqlite3.connect('file:%s?immutable=1'
                         % os.path.join(build_dir, mode + '.sqlite').replace('\\', '/'), uri=True)
    schema_version, _, sv, cal_lo, cal_hi, _ = db.execute(
        'SELECT schema_version, mode, static_version, calendar_start, calendar_end, generated_at '
        'FROM meta').fetchone()
    db.close()
    return sv, {
        'url': '%s/%s.sqlite.gz' % (base_url, mode),
        'sha256': sha256(gz),
        'size_bytes': os.path.getsize(gz),
        'static_version': sv,
        'schema_version': schema_version,
        'calendar_start': cal_lo,
        'calendar_end': cal_hi,
        'compression': 'gzip',
    }


def build_config():
    """契约 4b。首版广告全关；realtime 按 M0-0.14 的定稿取 60 s（不是区间取中的 45）。"""
    return {
        'schema_version': CONFIG_SCHEMA_VERSION,
        'ads': {
            'enabled': False,
            'app_open': {'probability': 0.0, 'min_interval_minutes': 240,
                         'exclude_deeplink_launch': True},
            'banners': {'pages': []},
            'grace': {'min_uses': 20, 'min_days': 7},
        },
        'realtime': {
            # M0-0.14 定稿：前台轮询 60 秒（设置里可切 30），**不是** 30–60 区间取中的 45。
            # 60 是按账④ 选的，不是随手取的中位数：Worker 请求数实测 ≈10.2 万/天已经贴死
            # 10 万上限，且请求数只受前台轮询间隔支配。发 45 会把它再抬 60/45 ≈ 1.33 倍
            # （≈13.6 万/天），把一本已经超了的账又放大三分之一。
            # 客户端 Manifest.kt 的默认值也是 60，发 45 等于用远端配置把对的默认值改错。
            'poll_seconds': 60,
            # M2-4.3（2026-09-16 裁定）：Widget 后台周期刷新 = DP4-A 的 60 分钟。
            # 30 分钟在 M2 的四本账里过不了：Widget 后台是账④ 模型里原本不存在的第二个
            # 请求源，30 分钟 ≈ 每设备每天 48 次，把已经贴线的请求数直接打穿。
            'widget_min_refresh_minutes': 60,
            'stale_after_seconds': 120,
            # 必须 > widget_min_refresh_minutes，否则每个正常刷新周期里都有一段时间
            # 把好数据显示成「已过时」。75 = 60 + 15 余量，取最小合理值。
            # 该约束仅在 DP4-A/B（Periodic）档成立；DP4-C'/D 下刷新本就稀疏，
            # 灰显是预期行为而非配置错误。
            'widget_stale_after_minutes': 75,
        },
        'force_update': {
            'min_supported_app_version': MIN_SUPPORTED_APP_VERSION,
            'message_key': 'force_update_generic',
        },
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('build_dir')
    ap.add_argument('--tag', required=True, help='Release tag，如 2026-09-07')
    ap.add_argument('--repo', default='jiaweizhu301/transport-peek-data')
    ap.add_argument('--channel', default='stable', choices=['stable', 'prerelease'])
    ap.add_argument('--pages-base', default=PAGES_BASE_DEFAULT)
    ap.add_argument('--modes', nargs='*', default=None)
    a = ap.parse_args()

    base = 'https://github.com/%s/releases/download/%s' % (a.repo, a.tag)
    modes = a.modes or PUBLISH_MODES

    feeds = {}
    for mode in modes:
        sv, feeds[mode] = feed_entry(a.build_dir, mode, base)
        sp = stop_parents(os.path.join(a.build_dir, mode + '.sqlite'), mode, sv)
        with open(os.path.join(a.build_dir, 'stop_parents.%s.json' % mode), 'w',
                  encoding='utf-8') as f:
            json.dump(sp, f, ensure_ascii=False, indent=1)
        print('stop_parents.%s.json  %d 条' % (mode, len(sp['stops'])), flush=True)

    manifest = {
        'schema_version': MANIFEST_SCHEMA_VERSION,
        'generated_at': utcnow(),
        'min_supported_app_version': MIN_SUPPORTED_APP_VERSION,
        'feeds': feeds,
        'config_url': base + '/config.json',
        'privacy_policy_url': a.pages_base + '/privacy.html',
        'attribution_url': a.pages_base + '/attribution.html',
        'stop_parents_url_template': base + '/stop_parents.{mode}.json',
        'channel': a.channel,
    }
    with open(os.path.join(a.build_dir, 'manifest.json'), 'w', encoding='utf-8') as f:
        json.dump(manifest, f, ensure_ascii=False, indent=1)
    with open(os.path.join(a.build_dir, 'config.json'), 'w', encoding='utf-8') as f:
        json.dump(build_config(), f, ensure_ascii=False, indent=1)
    print('manifest.json  feeds=%s channel=%s' % (','.join(feeds), a.channel), flush=True)

    # 非营运 headsign 词表：与 fixtures/non_revenue_headsigns.json 同一份真相（build_db 的常量），
    # 文件名也一样。客户端拿真库时按 stop_parents 同样的 base URL 取它。
    # **没有往 manifest 里加 URL 字段**：contracts/manifest.schema.json 是 additionalProperties=false
    # 的冻结契约，加字段要走契约变更流程；在那之前 URL 与 stop_parents_url_template 同源可推导。
    wl = build_db.export_non_revenue_headsigns(a.build_dir)
    print('%s  %d 条词  %d 字节'
          % (os.path.basename(wl), len(build_db.NON_REVENUE_HEADSIGNS), os.path.getsize(wl)),
          flush=True)
    # 解析规则（PLATFORM_RE 等）：同上，与 fixtures/parsing_rules.json 同名同源
    pr = build_db.export_parsing_rules(a.build_dir)
    print('%s  %d 条规则  %d 字节'
          % (os.path.basename(pr), len(build_db.parsing_rules_doc()['rules']),
             os.path.getsize(pr)), flush=True)


if __name__ == '__main__':
    main()
