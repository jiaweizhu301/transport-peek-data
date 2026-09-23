#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""D3/D4 · 校验 Release 资产是否符合契约 schema，并与 SQLite 产物交叉对账。

  python validate_assets.py <build_dir>

schema 副本在 `pipeline/schemas/`（逐字拷自 `docs/scoping/design/contracts/`，只读，不许改）。
另外对账两份导出资产（`non_revenue_headsigns.json` / `parsing_rules.json`）必须与 `build_db`
里的常量逐条/逐字相等 —— 挡的是「有人手改了资产」。
交叉对账：manifest.feeds[mode] 的 sha256 / size_bytes / static_version / calendar_* 必须与
实际的 .sqlite.gz 和 meta 表一致；stop_parents 的每个 parent_stop_id 必须在 stops 里存在。
"""
import hashlib
import json
import os
import sqlite3
import sys

import jsonschema

HERE = os.path.dirname(os.path.abspath(__file__))
SCHEMAS = os.path.join(os.path.dirname(HERE), 'schemas')
sys.path.insert(0, HERE)
import build_db  # noqa: E402  非营运词表的唯一真相
from gtfs_modes import self_parent  # noqa: E402  与 Worker MODE_TOPOLOGY 同一个标记


def sha256(p):
    h = hashlib.sha256()
    with open(p, 'rb') as f:
        for b in iter(lambda: f.read(1 << 20), b''):
            h.update(b)
    return h.hexdigest()


def main():
    d = sys.argv[1]
    fails = []
    manifest = json.load(open(os.path.join(d, 'manifest.json'), encoding='utf-8'))
    config = json.load(open(os.path.join(d, 'config.json'), encoding='utf-8'))
    for name, doc in (('manifest.json', manifest), ('config.json', config)):
        schema = json.load(open(os.path.join(SCHEMAS, name.replace('.json', '.schema.json')),
                                encoding='utf-8'))
        try:
            jsonschema.Draft7Validator(schema).validate(doc)
            print('OK   %s 符合契约 schema' % name)
        except jsonschema.ValidationError as e:
            fails.append('%s schema 校验失败：%s @ %s' % (name, e.message, list(e.absolute_path)))

    if config['ads']['enabled'] is not False:
        fails.append('config.ads.enabled 必须为 false（首版）')
    if config['force_update']['min_supported_app_version'] != manifest['min_supported_app_version']:
        fails.append('config 与 manifest 的 min_supported_app_version 不一致')

    # M3-3.20：这条约束此前只写在 schema 的 description 文字里，**没有任何东西会红**。
    # 破掉它的后果不是报错而是显示错：每个刷新周期里有一段时间把好数据标成「已过时」，
    # 正砸在 M2-5.6 第 4 步那条硬前置上。
    rt = config['realtime']
    if not rt['widget_stale_after_minutes'] > rt['widget_min_refresh_minutes']:
        fails.append(
            'widget_stale_after_minutes(%s) 必须 > widget_min_refresh_minutes(%s)'
            % (rt['widget_stale_after_minutes'], rt['widget_min_refresh_minutes'])
        )

    for mode, feed in manifest['feeds'].items():
        gz = os.path.join(d, mode + '.sqlite.gz')
        db_path = os.path.join(d, mode + '.sqlite')
        if not os.path.exists(gz):
            fails.append('%s: 缺 %s.sqlite.gz' % (mode, mode))
            continue
        if feed['size_bytes'] != os.path.getsize(gz):
            fails.append('%s: size_bytes 与实际不符' % mode)
        if feed['sha256'] != sha256(gz):
            fails.append('%s: sha256 与实际不符' % mode)
        db = sqlite3.connect('file:%s?immutable=1' % db_path.replace('\\', '/'), uri=True)
        sv, mmode, static_version, lo, hi = db.execute(
            'SELECT schema_version, mode, static_version, calendar_start, calendar_end '
            'FROM meta').fetchone()
        if (feed['schema_version'], feed['static_version'], feed['calendar_start'],
                feed['calendar_end']) != (sv, static_version, lo, hi):
            fails.append('%s: manifest 与 meta 表对不上' % mode)
        if mmode != mode:
            fails.append('%s: meta.mode=%s' % (mode, mmode))
        # stop_parents 交叉对账
        spp = os.path.join(d, 'stop_parents.%s.json' % mode)
        if not os.path.exists(spp):
            fails.append('%s: 缺 stop_parents.%s.json' % (mode, mode))
        else:
            sp = json.load(open(spp, encoding='utf-8'))
            if sp['mode'] != mode or sp['static_version'] != static_version:
                fails.append('%s: stop_parents 的 mode/static_version 对不上' % mode)
            known = {r[0] for r in db.execute('SELECT stop_id FROM stops')}
            parents = {r[0] for r in db.execute(
                'SELECT stop_id FROM stops WHERE location_type = 1')}
            expect = db.execute(
                'SELECT count(*) FROM stops WHERE parent_station IS NOT NULL').fetchone()[0]
            if not self_parent(mode):
                # 没有子站的父站各有一行自映射（gen_manifest.stop_parents 的 docstring）
                expect += db.execute(
                    'SELECT count(*) FROM stops p WHERE p.location_type = 1 AND NOT EXISTS'
                    ' (SELECT 1 FROM stops c WHERE c.parent_station = p.stop_id)').fetchone()[0]
            if len(sp['stops']) != expect:
                fails.append('%s: stop_parents 条数 %d != 应有 %d（子站 + 非 self_parent 的无子站父站）'
                             % (mode, len(sp['stops']), expect))
            # 覆盖：每个有车停的站，Worker 都要映射得上 —— 在 stop_parents 里，或 mode 是 self_parent。
            # 这是「实时全丢」那一类缺陷在资产层的判据（公交一次、轻轨 L1 一次）。
            if not self_parent(mode):
                mapped = {x['stop_id'] for x in sp['stops']}
                unmapped = sorted(r[0] for r in db.execute('SELECT DISTINCT stop_id FROM pattern_stops')
                                  if r[0] not in mapped)
                if unmapped:
                    fails.append('%s: 有车停、却不在 stop_parents 里的站 %d 个（Worker 会丢掉它们的实时）：%s'
                                 % (mode, len(unmapped), ', '.join(unmapped[:5])))
            bad = [s for s in sp['stops']
                   if s['stop_id'] not in known or s['parent_stop_id'] not in parents]
            if bad:
                fails.append('%s: stop_parents 有 %d 条指向库里不存在的站' % (mode, len(bad)))
            # 自成父站的 mode（公交）没有子站，stop_parents 按定义就是 0 条、也没有站台号。
            # 豁免条件读 gtfs_modes.self_parent —— 与 Worker 的 MODE_TOPOLOGY 同一个标记。
            # 反过来也要核：标记与库里的真实形状必须一致，否则 Worker 会按错的规则映射。
            if self_parent(mode):
                if sp['stops']:
                    fails.append('%s: 标记为 self_parent，但 stop_parents 有 %d 条子站'
                                 % (mode, len(sp['stops'])))
            else:
                if not sp['stops']:
                    fails.append('%s: 没有标记 self_parent，但 stop_parents 是 0 条 —— '
                                 'Worker 会把这个 mode 的实时全部丢掉' % mode)
                elif not any(s['platform_code'] for s in sp['stops']):
                    fails.append('%s: stop_parents 全部 platform_code 为 null' % mode)
        db.close()
        print('OK   %s: gz %.2f MB / sha256 / meta / stop_parents 全部对账一致'
              % (mode, feed['size_bytes'] / 1e6))

    # non_revenue_headsigns.json：必须存在，且与 build_db 的常量逐条相等（挡「有人手改了资产」）
    wlp = os.path.join(d, build_db.NON_REVENUE_HEADSIGNS_FILENAME)
    if not os.path.exists(wlp):
        fails.append('缺 %s（gen_manifest.py 没跑？）' % build_db.NON_REVENUE_HEADSIGNS_FILENAME)
    else:
        wl = json.load(open(wlp, encoding='utf-8'))
        want = sorted(build_db.NON_REVENUE_HEADSIGNS)
        if wl.get('patterns') != want:
            fails.append('%s 的 patterns 与 build_db.NON_REVENUE_HEADSIGNS 不一致'
                         % build_db.NON_REVENUE_HEADSIGNS_FILENAME)
        elif wl.get('match', {}).get('rule') != 'substring_contains':
            fails.append('%s 的 match.rule 被改过' % build_db.NON_REVENUE_HEADSIGNS_FILENAME)
        else:
            print('OK   %s: %d 条词与 build_db 常量逐条一致'
                  % (build_db.NON_REVENUE_HEADSIGNS_FILENAME, len(want)))

    # parsing_rules.json：必须存在，且正则与 build_db 的常量逐字相等
    prp = os.path.join(d, build_db.PARSING_RULES_FILENAME)
    if not os.path.exists(prp):
        fails.append('缺 %s（gen_manifest.py 没跑？）' % build_db.PARSING_RULES_FILENAME)
    else:
        pr = json.load(open(prp, encoding='utf-8')).get('rules', {})
        want = [('platform_code', ('platform_code', 'regex'), build_db.PLATFORM_RE.pattern),
                ('name_normalized punct', ('name_normalized', 1), build_db.PUNCT_RE.pattern),
                ('name_normalized station', ('name_normalized', 2),
                 build_db.STATION_WORD_RE.pattern),
                ('direction_key via', ('direction_key', 0), build_db.VIA_RE.pattern)]
        bad = []
        for label, (rule, key), expect in want:
            got = (pr.get(rule, {}).get('regex') if key == 'regex'
                   else pr.get(rule, {}).get('steps', [{}] * (key + 1))[key].get('regex'))
            if got != expect:
                bad.append('%s: %r != %r' % (label, got, expect))
        if bad:
            fails.append('%s 与 build_db 的正则不一致：%s'
                         % (build_db.PARSING_RULES_FILENAME, '; '.join(bad)))
        elif pr.get('platform_code', {}).get('regex_inline_flags') != \
                '(?i)' + build_db.PLATFORM_RE.pattern:
            fails.append('%s 的 platform_code.regex_inline_flags 与 regex 对不上'
                         % build_db.PARSING_RULES_FILENAME)
        else:
            print('OK   %s: %d 条规则的正则与 build_db 常量逐字一致'
                  % (build_db.PARSING_RULES_FILENAME, len(pr)))

    for f in fails:
        print('FAIL ' + f)
    if fails:
        print('资产校验失败：%d 处 -> 不发 Release' % len(fails))
        return 1
    print('资产校验通过')
    return 0


if __name__ == '__main__':
    sys.exit(main())
