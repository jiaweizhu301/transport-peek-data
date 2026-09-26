#!/usr/bin/env bash
# 本地端到端跑一遍（不发 Release）。CI 里的等价流程见 .github/workflows/daily.yml。
#
#   ./run_all.sh [build_dir] [days_spec]
#
# days_spec 留空 = 用每模式缺省窗口（gtfs_modes.MODES[mode]["days"]：sydneytrains=16 / metro=120）。
# 也可写 "sydneytrains=14,metro=120" 逐模式覆盖，或写单个数字全局覆盖。
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OUT="${1:-$HERE/../build}"
DAYS="${2:-}"
TAG="$(TZ=Australia/Sydney date +%Y-%m-%d)"   # 悉尼当地日，与 daily.yml 的 `定 tag` 一致

python "$HERE/golden_check.py"
python "$HERE/tz_check.py"
python "$HERE/shapes.py"
python "$HERE/check_schematic.py" --selftest
python "$HERE/build_shapes.py" --selftest
python "$HERE/suburbs.py" --selftest
python "$HERE/fetch_gtfs.py"   "$OUT/gtfs"
python "$HERE/build_db.py"     "$OUT/gtfs" "$OUT" --days "$DAYS"
# M6：区表改了 buses 库并重新 gzip，必须在 invariants / gen_manifest 之前
python "$HERE/suburbs.py"        "$OUT" "$HERE/../geo/sal2021_nsw.json.gz"
python "$HERE/build_shapes.py"   "$OUT/gtfs" "$OUT"
python "$HERE/invariants.py"   "$OUT"
# M6 §6.2：只告警（$HERE/../schematic/ 是 data 仓布局；主仓先把 app asset 拷到 pipeline/schematic/，已 gitignore）
python "$HERE/check_schematic.py" "$HERE/../schematic/sydney-rail.json" "$OUT" || true
python "$HERE/gen_manifest.py" "$OUT" --tag "$TAG"
python "$HERE/validate_assets.py" "$OUT"
echo
echo "本地全绿。要发布：GITHUB_TOKEN=... bash $HERE/publish_release.sh $OUT $TAG stable"
