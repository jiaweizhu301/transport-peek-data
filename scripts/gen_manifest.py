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
from gtfs_modes import PUBLISH_MODES, self_parent
import build_db

# manifest/config 的 schema_version（契约 const 1）
MANIFEST_SCHEMA_VERSION = 1
CONFIG_SCHEMA_VERSION = 1
# 最低可用 app versionCode。只在破坏性变更时抬高（同时 manifest 与 config 两处）。
#
# ⚠ M4 的 schema 2 是破坏性变更（时刻表库 meta.schema_version 1 → 2，客户端硬切、不双读）。
# 这个常量**必须**在发 M4 的 prerelease 之前抬到 M4 那个 APK 的 versionCode。
# 现在没有抬：M4 APK 的 versionCode 是发版时用 `-Pversion_code` 定的，此刻还不存在，
# 在这里猜一个数就是把一个未验证的假设写死进契约。改用 `--min-app-version` 在发版时传，
# 不传就沿用下面这个值（= M3 的口径，只够让 M3 的包收到 UNSUPPORTED_SCHEMA 而不是一个
# 更清楚的「请升级」提示 —— 功能上安全，提示上不够好）。
# 这条挂在 checklist 4.15 的关账清单上。
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
    """子站 -> 父站 + 站台号。形状与 fixtures/stop_parents.<mode>.json 逐字段一致。

    **自己就是停靠点的父站也要导出一行自映射**（非 self_parent 的 mode）：build_db 按站提父站的
    结果（例如轻轨 innerwest 同一物理站的两个站台并成一个父站，父站是其中一个站台），实时 feed
    里出现的就是它们的
    stop_id。Worker 对 stop_parents 查不到的 stop_id 一律丢掉，不导出这一行 = 这些站实时全丢
    （2026-09-23 查出）。self_parent 的 mode（公交）不导出：Worker 按 MODE_TOPOLOGY 把站当自己，
    导出 3.2 万行自映射首灌要吃约 32% 的免费写入额度。
    """
    db = sqlite3.connect('file:%s?immutable=1' % db_path.replace('\\', '/'), uri=True)
    rows = db.execute(
        'SELECT stop_id, parent_station, platform_code FROM stops '
        'WHERE parent_station IS NOT NULL ORDER BY stop_id').fetchall()
    if not self_parent(mode):
        # 自己就是停靠点的父站：没有子站的（按站提升的单站台），以及同一物理站的几个站台
        # 并组后被选作父站的那个（它有子站，但自己也有车停）。火车 / metro 的父站不停车，0 行。
        rows += db.execute(
            'SELECT p.stop_id, p.stop_id, p.platform_code FROM stops p WHERE p.location_type = 1'
            ' AND EXISTS (SELECT 1 FROM pattern_stops ps WHERE ps.stop_id = p.stop_id)'
            ' ORDER BY p.stop_id').fetchall()
        rows.sort(key=lambda r: r[0])
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


def build_config(min_app_version=MIN_SUPPORTED_APP_VERSION):
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
            # M3-3.20：这份副本此前停在 30，而线上（data 仓）跑的是 60 —— 任何人从主仓
            # 重拷一次就会把线上静默打回 30。现已收口，两边逐字一致。
            'widget_min_refresh_minutes': 60,
            'stale_after_seconds': 120,
            # 必须 > widget_min_refresh_minutes，否则每个正常刷新周期里都有一段时间
            # 把好数据显示成「已过时」。75 = 60 + 15 余量，取最小合理值。
            # 这条约束由 validate_assets.py 断言（M3-3.20），不再只写在 schema 的描述文字里。
            'widget_stale_after_minutes': 75,
        },
        'force_update': {
            'min_supported_app_version': min_app_version,
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
    ap.add_argument('--min-app-version', type=int, default=MIN_SUPPORTED_APP_VERSION,
                    help='最低可用 app versionCode。M4 schema 2 发版时必须传 M4 APK 的 versionCode')
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
        'min_supported_app_version': a.min_app_version,
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
        json.dump(build_config(a.min_app_version), f, ensure_ascii=False, indent=1)
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
