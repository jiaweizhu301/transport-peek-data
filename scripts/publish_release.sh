#!/usr/bin/env bash
# D3 · 发 GitHub Release 并上传资产。用 REST API（本机与 CI 都不装 gh CLI）。
#
#   GITHUB_TOKEN=... ./publish_release.sh <build_dir> <tag> [stable|prerelease]
#
# 客户端只认 releases/latest/download/manifest.json：
#   * channel=stable     -> make_latest=true，latest 指到这个 tag
#   * channel=prerelease -> prerelease=true + make_latest=false，**不动 latest**
#     （schema_version 变更时走这条，见 contracts/manifest.schema.json 的 channel 字段）
set -euo pipefail

BUILD_DIR="${1:?用法: publish_release.sh <build_dir> <tag> [stable|prerelease]}"
TAG="${2:?缺 tag}"
CHANNEL="${3:-stable}"
REPO="${GITHUB_REPOSITORY:-jiaweizhu301/transport-peek-data}"
API="https://api.github.com/repos/${REPO}"
UPLOADS="https://uploads.github.com/repos/${REPO}"
: "${GITHUB_TOKEN:?需要 GITHUB_TOKEN（Actions 里用 secrets.GITHUB_TOKEN，权限 contents: write）}"

hdr=(-H "Authorization: Bearer ${GITHUB_TOKEN}"
     -H "Accept: application/vnd.github+json"
     -H "X-GitHub-Api-Version: 2022-11-28")

if [ "$CHANNEL" = "prerelease" ]; then PRE=true; LATEST='"false"'; else PRE=false; LATEST='"true"'; fi

# 已存在同名 tag 的 Release 就先删掉（同一天重跑 / 手动 dispatch 重试）
# /releases/tags/{tag} 查不到 draft，所以 draft 要从列表里找（上一次跑到一半失败会留下 draft）
existing=$(curl -sS "${hdr[@]}" "${API}/releases?per_page=100" | python -c \
  'import json,sys
d=json.load(sys.stdin)
print(" ".join(str(r["id"]) for r in (d if isinstance(d,list) else []) if r.get("tag_name")==sys.argv[1]))' "$TAG" || true)
for id in $existing; do
  echo "已存在同 tag 的 Release/draft ${TAG}（id=${id}），先删除后重发"
  curl -sS -X DELETE "${hdr[@]}" "${API}/releases/${id}" >/dev/null
done

body=$(python - "$BUILD_DIR" "$TAG" "$CHANNEL" <<'PY'
import json, os, sys
d, tag, channel = sys.argv[1], sys.argv[2], sys.argv[3]
m = json.load(open(os.path.join(d, 'manifest.json'), encoding='utf-8'))
lines = ['TransportPeek 时刻表数据 · %s（channel=%s）' % (tag, channel), '',
         '| mode | static_version | calendar | size |', '|---|---|---|---:|']
for mode, f in m['feeds'].items():
    lines.append('| %s | %s | %d–%d | %.2f MB |' % (
        mode, f['static_version'], f['calendar_start'], f['calendar_end'],
        f['size_bytes'] / 1e6))
lines += ['', 'Data © Transport for NSW，按 CC BY 4.0 使用。归属说明见 ' + m['attribution_url']]
print(json.dumps('\n'.join(lines)))
PY
)

# 先建成 draft。若一上来就 make_latest=true，Release 建好的那一瞬间 latest 就指过来了，
# 而 sydneytrains.sqlite.gz 还要再传几十秒 —— 这段窗口里客户端打开
# releases/latest/download/manifest.json 会拿到 404。资产传齐再 publish。
rel=$(curl -sS -X POST "${hdr[@]}" "${API}/releases" -d @- <<JSON
{"tag_name": "${TAG}", "name": "${TAG}", "body": ${body},
 "draft": true, "prerelease": ${PRE}}
JSON
)
rel_id=$(printf '%s' "$rel" | python -c 'import json,sys;print(json.load(sys.stdin)["id"])')
echo "Release ${TAG} 草稿已建 id=${rel_id} prerelease=${PRE}"

uploaded=0
upload() {
  local f="$1" name ctype code
  name=$(basename "$f")
  case "$name" in *.gz) ctype=application/gzip ;; *.json) ctype=application/json ;;
                  *) ctype=application/octet-stream ;; esac
  echo "  上传 ${name} ($(python -c "import os;print('%.2f MB'%(os.path.getsize('$f')/1e6))"))"
  # curl 遇到 4xx/5xx 默认仍然 exit 0。不看状态码的话，资产上传失败会被当成发布成功，
  # 客户端就会拿到一个缺资产的 latest。显式检查 HTTP 201。
  code=$(curl -sS -o /dev/null -w '%{http_code}' -X POST "${hdr[@]}" -H "Content-Type: ${ctype}" --data-binary @"$f" "${UPLOADS}/releases/${rel_id}/assets?name=${name}")
  if [ "$code" != "201" ]; then
    echo "FATAL 上传 ${name} 失败：HTTP ${code}（草稿 ${TAG} 未 publish，latest 不受影响）" >&2
    exit 1
  fi
  uploaded=$((uploaded+1))
}

upload "${BUILD_DIR}/manifest.json"
upload "${BUILD_DIR}/config.json"
# 非营运 headsign 词表：与 fixtures/non_revenue_headsigns.json 同名同源（build_db 的常量），
# 客户端拿真库时按 stop_parents 同样的 base URL 取它
upload "${BUILD_DIR}/non_revenue_headsigns.json"
# 解析规则（PLATFORM_RE 等）：同上，客户端 Regex(读到的字符串) 直接用
upload "${BUILD_DIR}/parsing_rules.json"
for f in "${BUILD_DIR}"/*.sqlite.gz "${BUILD_DIR}"/stop_parents.*.json "${BUILD_DIR}"/shapes.*.bin.gz; do
  [ -e "$f" ] && upload "$f"
done
if [ "$uploaded" -lt 6 ]; then
  echo "FATAL 只传了 ${uploaded} 个资产（至少应有 manifest/config/词表/解析规则/1 个库/1 个 stop_parents）" >&2
  exit 1
fi

# 资产齐了才 publish，此时才按 channel 决定动不动 latest
code=$(curl -sS -o /dev/null -w '%{http_code}' -X PATCH "${hdr[@]}" "${API}/releases/${rel_id}" -d "{\"draft\": false, \"prerelease\": ${PRE}, \"make_latest\": ${LATEST}}")
if [ "$code" != "200" ]; then
  echo "FATAL publish 失败：HTTP ${code}" >&2
  exit 1
fi
echo "Release ${TAG} 已发布：${uploaded} 个资产，prerelease=${PRE} make_latest=${LATEST}"

echo "完成。客户端入口：https://github.com/${REPO}/releases/latest/download/manifest.json"
