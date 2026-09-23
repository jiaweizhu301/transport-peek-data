// M4-4.15 · 发家人之前的闸门：核线上 latest manifest。任何一条不满足 → exit 1 并打印是哪条。
//
//   node pipeline/scripts/release_gate.mjs --version-code 2026092301
//   node pipeline/scripts/release_gate.mjs --version-code 2026091702 --manifest-url http://127.0.0.1:18777/manifest.json
//
// 核的三件事（orchestrator 2026-09-23 定）：
//   1. 四个 mode（sydneytrains / metro / buses / lightrail）都在，每个 feed 的 schema_version 都是 2；
//   2. min_supported_app_version 等于 --version-code（新 app 的 versionCode）；
//   3. 每个资产 URL 能 HEAD 到（跟随重定向），Content-Length 与 manifest 的 size_bytes 一致。
// 只读：只发 GET / HEAD，不改任何东西。curl 在本沙箱坏了，一律 node fetch。

const argv = process.argv.slice(2);
const arg = (k, d) => { const i = argv.indexOf(k); return i >= 0 ? argv[i + 1] : d; };
const MANIFEST_URL = arg('--manifest-url',
  'https://github.com/jiaweizhu301/transport-peek-data/releases/latest/download/manifest.json');
const VERSION_CODE = arg('--version-code');
const MODES = ['sydneytrains', 'metro', 'buses', 'lightrail'];
const SCHEMA = 2;
if (!VERSION_CODE || !/^\d+$/.test(VERSION_CODE)) {
  console.error('用法：--version-code <新 app 的 versionCode> [--manifest-url <url>]');
  process.exit(2);
}

const failures = [];
const fail = (msg) => { failures.push(msg); console.log('  ✗ ' + msg); };
const pass = (msg) => console.log('  ✓ ' + msg);

console.log(`manifest: ${MANIFEST_URL}`);
const res = await fetch(MANIFEST_URL);
if (!res.ok) {
  console.log(`  ✗ 取 manifest 失败：HTTP ${res.status}`);
  process.exit(1);
}
const m = await res.json();
console.log(`generated_at ${m.generated_at} · manifest schema_version ${m.schema_version} · min_supported_app_version ${m.min_supported_app_version}`);

// 1. 四个 mode、schema 2
const feeds = m.feeds || {};
for (const mode of MODES) {
  const f = feeds[mode];
  if (!f) { fail(`${mode}：manifest 里没有`); continue; }
  if (f.schema_version !== SCHEMA) fail(`${mode}：schema_version = ${f.schema_version}，应为 ${SCHEMA}`);
  else pass(`${mode}：schema_version ${SCHEMA}，static_version ${f.static_version}`);
}

// 2. min_supported_app_version
if (String(m.min_supported_app_version) !== VERSION_CODE) {
  fail(`min_supported_app_version = ${m.min_supported_app_version}，应为 ${VERSION_CODE}`);
} else pass(`min_supported_app_version = ${VERSION_CODE}`);

// 3. 资产能 HEAD 到、大小一致（只查 manifest 里实际列出的 feed，缺的已在第 1 条报过）
for (const [mode, f] of Object.entries(feeds)) {
  try {
    const h = await fetch(f.url, { method: 'HEAD', redirect: 'follow' });
    const len = h.headers.get('content-length');
    if (!h.ok) fail(`${mode}：HEAD ${f.url} → HTTP ${h.status}`);
    else if (len == null) fail(`${mode}：HEAD ${f.url} 没有 Content-Length`);
    else if (+len !== f.size_bytes) fail(`${mode}：大小 ${len} ≠ manifest size_bytes ${f.size_bytes}`);
    else pass(`${mode}：资产可达，${len} 字节与 manifest 一致`);
  } catch (e) {
    fail(`${mode}：HEAD ${f.url} 失败：${e.message}`);
  }
}

if (failures.length) {
  console.log(`\n闸门不通过：${failures.length} 条`);
  process.exit(1);
}
console.log('\n闸门通过');
