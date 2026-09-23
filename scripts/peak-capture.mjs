// M4-4.1 · 早高峰原始快照采集器（只抓不算）
//
// 为什么在这个仓：4.1 的判据要求「工作日 07:30–09:00 内 ≥ 10 个连续刷新窗口」的样本。
// 2026-09-22 在本地机器上试过一次，机器 02:18 进 S3 睡眠、10:59 才醒，整个早高峰睡过去了。
// 早高峰一周只有 5 次，不能押在一台会睡觉的机器上。公开仓的 Actions 分钟数无限、
// 24 小时在线，是这件事最省的载体 —— 和 peak-sample.yml 当年的理由一模一样。
//
// 为什么只抓不算：解码与签名口径住在主仓（worker/src/decode.js、pbparse.js）。
// 把它们复制一份到这里就会有两份实现，这个项目已经为「两份 gen_manifest.py」付过学费。
// 这里只负责把原始 protobuf 落盘成 artifact，分析在主仓用
// worker/scripts/peak-sample-analyse.mjs 离线跑 —— 那个分析器本来就是吃原始快照的。
//
// 用法：node scripts/peak-capture.mjs --out <dir> [--windows N] [--period-ms N]
// key 从环境变量 TFNSW_API_KEY 取（仓库 secret，daily.yml 已经在用同一个）。

import { writeFileSync, appendFileSync, mkdirSync } from 'node:fs';
import { gzipSync } from 'node:zlib';
import { join } from 'node:path';

const argv = process.argv.slice(2);
const arg = (k, d) => {
  const i = argv.indexOf(k);
  return i >= 0 && argv[i + 1] !== undefined ? argv[i + 1] : d;
};

const OUT = arg('--out', 'peak-raw');
const WINDOWS = +arg('--windows', '80');
const PERIOD_MS = +arg('--period-ms', '65000');   // 线上 TTL 60 s，65 s 才保证是一次新刷新
const ALERTS_EVERY = +arg('--alerts-every', '10');
// 悉尼当地的 HH:MM，到点才开采。空 = 立刻开采（手动触发走这条）。
const WAIT_UNTIL = arg('--wait-until', '');

const KEY = (process.env.TFNSW_API_KEY || '').trim();
if (!KEY) throw new Error('缺 TFNSW_API_KEY');

// 落盘布局与本地采样器（主仓 worker/scripts/peak-sample-writes.mjs）逐字一致：
// 快照进 <out>/raw/，这样主仓的 peak-sample-analyse.mjs 解开 artifact 就能直接吃，
// 不用为「云上采的」再写一个分支。
mkdirSync(join(OUT, 'raw'), { recursive: true });

// feed 拓扑照主仓 M4/4.1-d1-write-measure.md §2 的实测表：
// buses 只有 v1；lightrail 是两条子 feed 跨两个版本号；聚合端点是空的，不能用。
const BASE = 'https://api.transport.nsw.gov.au/';
const FEEDS = [
  ['sydneytrains', 'v2/gtfs/realtime/sydneytrains'],
  ['metro', 'v2/gtfs/realtime/metro'],
  ['buses', 'v1/gtfs/realtime/buses'],
  ['lightrail_innerwest', 'v2/gtfs/realtime/lightrail/innerwest'],
  ['lightrail_cbdandsoutheast', 'v1/gtfs/realtime/lightrail/cbdandsoutheast'],
];
const ALERTS = [
  ['alerts_sydneytrains', 'v2/gtfs/alerts/sydneytrains'],
  ['alerts_metro', 'v2/gtfs/alerts/metro'],
  ['alerts_buses', 'v2/gtfs/alerts/buses'],
];

const SYD = new Intl.DateTimeFormat('en-GB', {
  timeZone: 'Australia/Sydney', hour12: false,
  year: 'numeric', month: '2-digit', day: '2-digit',
  hour: '2-digit', minute: '2-digit', second: '2-digit',
});
const stamp = () => {
  const p = Object.fromEntries(SYD.formatToParts(new Date()).map((x) => [x.type, x.value]));
  return `${p.year}-${p.month}-${p.day} ${p.hour}:${p.minute}:${p.second} Sydney`;
};
const LOG = join(OUT, 'capture.log');
const say = (s) => {
  const line = `[${stamp()}] ${s}`;
  console.log(line);
  appendFileSync(LOG, line + '\n');
};
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function grab(url) {
  const res = await fetch(url, {
    headers: { Authorization: 'apikey ' + KEY, 'user-agent': 'TransportPeek/m4-4.1-capture' },
    signal: AbortSignal.timeout(20_000),
  });
  const buf = new Uint8Array(await res.arrayBuffer());
  return { status: res.status, buf };
}

const index = [];

async function one(name, path, w) {
  const t0 = Date.now();
  try {
    const { status, buf } = await grab(BASE + path);
    if (status !== 200) {
      say(`  ${name} HTTP ${status}`);
      index.push({ w, feed: name, http: status });
      return;
    }
    const file = `w${String(w).padStart(3, '0')}.${name}.pb.gz`;
    writeFileSync(join(OUT, 'raw', file), gzipSync(buf));
    index.push({ w, feed: name, http: 200, bytes: buf.length, file, ms: Date.now() - t0 });
  } catch (e) {
    const msg = String((e && e.message) || e).slice(0, 120);
    say(`  ${name} 抓取异常：${msg}`);
    index.push({ w, feed: name, error: msg });
  }
}

// ---------- 在 runner 里等到点再开采（M4-4.1 · 2026-09-23）----------
//
// 为什么需要它：GitHub 的 schedule **不准时**。2026-09-22 那天两条 cron
// （UTC 20:25 / 21:25 = 悉尼 07:25）实际在 UTC 23:01 / 23:40 才起跑 ——
// 延迟 2h36m 与 2h15m，落到悉尼 09:01 / 09:40，guard 按「小时 = 7」正确跳过，
// **两个 run 都是绿的、零 artifact，早高峰整个丢掉**。
//
// 把 guard 拓宽到 7–8 点仍然是在赌延迟落进那个窗口，而那天连 8 点都没落进。
// 所以改机制：**cron 往前排、起来之后在 runner 里 sleep 到点**。
// 准时起的 run 等一会儿；延迟两小时的那条正好落在开采时刻。
async function waitUntilSydney(hhmm) {
  const [hh, mm] = hhmm.split(':').map((x) => +x);
  if (!Number.isFinite(hh) || !Number.isFinite(mm)) throw new Error(`--wait-until 形如 07:25，实得 ${hhmm}`);
  const nowParts = Object.fromEntries(SYD.formatToParts(new Date()).map((x) => [x.type, x.value]));
  const nowMin = +nowParts.hour * 60 + +nowParts.minute;
  const targetMin = hh * 60 + mm;
  if (nowMin >= targetMin) {
    say(`已过悉尼 ${hhmm}（现在 ${nowParts.hour}:${nowParts.minute}），立刻开采`);
    return;
  }
  // 按分钟差算，不构造带时区的 Date —— 夏令时边界上自己算时差是这个仓踩过的坑。
  const waitMs = (targetMin - nowMin) * 60_000 - (+nowParts.second) * 1000;
  say(`等到悉尼 ${hhmm} 再开采：sleep ${Math.round(waitMs / 60000)} 分钟`);
  await sleep(waitMs);
  say(`到点，开采`);
}
if (WAIT_UNTIL) await waitUntilSydney(WAIT_UNTIL);

say(`采集启动：${WINDOWS} 个窗口 × ${PERIOD_MS} ms，feed ${FEEDS.map((f) => f[0]).join(', ')}`);
for (let w = 0; w < WINDOWS; w++) {
  const t0 = Date.now();
  await Promise.all(FEEDS.map(([n, p]) => one(n, p, w)));
  if (ALERTS_EVERY > 0 && w % ALERTS_EVERY === 0) {
    for (const [n, p] of ALERTS) await one(n, p, w);
  }
  const ok = index.filter((r) => r.w === w && r.http === 200).length;
  say(`w${w}/${WINDOWS} 成功 ${ok} 条`);
  writeFileSync(join(OUT, 'index.json'), JSON.stringify(index));
  const rest = PERIOD_MS - (Date.now() - t0);
  if (w < WINDOWS - 1 && rest > 0) await sleep(rest);
}
const ok = index.filter((r) => r.http === 200).length;
say(`采集完成：${ok} 个快照落盘，失败 ${index.length - ok} 次`);
if (ok === 0) {
  console.error('::error::一个快照都没抓到');
  process.exit(1);
}
