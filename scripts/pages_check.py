#!/usr/bin/env python3
"""M5-11 / M5-05 本地守门：公开页面与代码对得上。纯离线、只读文件。

断言：
  1. attribution.html 的英文部分与中文部分（<section class="zh">）都列出每一条在用的 TfNSW feed：
     * 时刻表 = gtfs_modes.feeds_for(m)，m 取 PUBLISH_MODES（L4 parramatta 也在里面）；
     * 实时 / 车辆位置 / 告警 = 下面 WORKER_FEEDS（抄自 Worker：worker/src/index.js 的 FEEDS / ALERT_FEEDS、
       worker/src/vehicles.js 的 VEH_FEEDS、worker/src/busops.js 的分运营商子 feed）。
       主仓布局（pipeline/scripts）能看到 ../worker/src 时，再从 JS 里直接抽 `RT + '...'`，
       抽出来的每一条也必须在页上，且必须在 WORKER_FEEDS 里（Worker 加了 feed 而这里没跟 → 红）。
  2. attribution.html 有 CC BY 4.0 与 Google 地图署名。
  3. privacy.html 不再有已证伪的两句承诺（「never sent … Google」「only when you tap」及其中文）。
  4. privacy.html 中英两部分都披露 Worker 按 IP 的内存限流（worker/src/index.js rateLimited / RATE：
     cf-connecting-ip 作 key、60 s 窗口、超 5000 条才清过期项）。

两种布局都认：data 仓（docs/、scripts/）与主仓 pipeline/（pages/、scripts/）。
"""
import html
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
from gtfs_modes import PUBLISH_MODES, feeds_for  # noqa: E402

PAGES = next((os.path.join(ROOT, d) for d in ("docs", "pages") if os.path.isdir(os.path.join(ROOT, d))), None)

# Worker 侧在用的上游端点（不含 api.transport.nsw.gov.au/ 前缀）。改 Worker 的 feed 必须同步改这里和 attribution.html。
WORKER_FEEDS = [
    # trip updates（index.js FEEDS）
    "v2/gtfs/realtime/sydneytrains",
    "v2/gtfs/realtime/metro",
    "v1/gtfs/realtime/buses",
    "v1/gtfs/realtime/buses/<OperatorID>",          # busops.js：按运营商的子 feed
    "v2/gtfs/realtime/lightrail/innerwest",
    "v1/gtfs/realtime/lightrail/cbdandsoutheast",
    # vehicle positions（vehicles.js VEH_FEEDS）
    "v2/gtfs/vehiclepos/sydneytrains",
    "v2/gtfs/vehiclepos/metro",
    "v1/gtfs/vehiclepos/buses",
    "v2/gtfs/vehiclepos/lightrail/innerwest",
    "v1/gtfs/vehiclepos/lightrail/cbdandsoutheast",
    # alerts（index.js ALERT_FEEDS）
    "v2/gtfs/alerts/sydneytrains",
    "v2/gtfs/alerts/metro",
    "v2/gtfs/alerts/buses",
]

BANNED_PRIVACY = ["never sent", "only when you tap", "only asks for permission when you tap",
                  "不会发给 Google", "只有你点了"]

# 限流披露：每种语言要同时出现的词组
RATE_LIMIT_EN = ["per-IP-address request counter", "in memory only", "60 per minute"]
RATE_LIMIT_ZH = ["按 IP 地址记一个请求计数", "只放在内存里", "每分钟 60 次"]

RT_LITERAL = re.compile(r"RT\s*\+\s*'([^']+)'")


def worker_feeds_from_js(worker_src):
    out = []
    for name in ("index.js", "vehicles.js"):
        p = os.path.join(worker_src, name)
        if os.path.exists(p):
            out += RT_LITERAL.findall(open(p, encoding="utf-8").read())
    return out


def text_of(fragment):
    # 去标签、反转义、折叠空白（源文件里一句话常被换行拆开）
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", fragment)))


def split_en_zh(page):
    i = page.find('<section class="zh">')
    if i < 0:
        return text_of(page), ""
    return text_of(page[:i]), text_of(page[i:])


def check(pages_dir, worker_src=None):
    errors = []
    if not pages_dir:
        return ["没找到 docs/ 或 pages/ 目录"]
    attr = open(os.path.join(pages_dir, "attribution.html"), encoding="utf-8").read()
    en, zh = split_en_zh(attr)
    schedule = [f.lstrip("/") for m in PUBLISH_MODES for f in feeds_for(m)]
    wanted = schedule + WORKER_FEEDS
    if worker_src and os.path.isdir(worker_src):
        for f in worker_feeds_from_js(worker_src):
            if f not in WORKER_FEEDS:
                errors.append("Worker 用了 %s，但 pages_check.WORKER_FEEDS 里没有" % f)
            if f not in wanted:
                wanted.append(f)
    for f in wanted:
        if f not in en:
            errors.append("attribution.html 英文部分缺 feed %s" % f)
        if f not in zh:
            errors.append("attribution.html 中文部分缺 feed %s" % f)
    for needle in ("CC BY 4.0", "Google"):
        if needle not in en or needle not in zh:
            errors.append("attribution.html 中英两部分都要提到 %r" % needle)
    priv_raw = open(os.path.join(pages_dir, "privacy.html"), encoding="utf-8").read()
    priv = text_of(priv_raw)
    p_en, p_zh = split_en_zh(priv_raw)
    for phrase in RATE_LIMIT_EN:
        if phrase not in p_en:
            errors.append("privacy.html 英文部分缺 IP 限流披露 %r" % phrase)
    for phrase in RATE_LIMIT_ZH:
        if phrase not in p_zh:
            errors.append("privacy.html 中文部分缺 IP 限流披露 %r" % phrase)
    for b in BANNED_PRIVACY:
        if b in priv:
            errors.append("privacy.html 仍含已证伪的说法 %r" % b)
    return errors


if __name__ == "__main__":
    errs = check(PAGES, os.path.join(os.path.dirname(ROOT), "worker", "src"))
    for e in errs:
        print("FAIL:", e)
    if errs:
        sys.exit(1)
    print("pages_check OK")
