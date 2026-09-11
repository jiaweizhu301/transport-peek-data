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
TAG="$(date -u +%Y-%m-%d)"

python "$HERE/golden_check.py"
python "$HERE/fetch_gtfs.py"   "$OUT/gtfs"
python "$HERE/build_db.py"     "$OUT/gtfs" "$OUT" --days "$DAYS"
python "$HERE/invariants.py"   "$OUT"
python "$HERE/gen_manifest.py" "$OUT" --tag "$TAG"
python "$HERE/validate_assets.py" "$OUT"
echo
echo "本地全绿。要发布：GITHUB_TOKEN=... bash $HERE/publish_release.sh $OUT $TAG stable"
