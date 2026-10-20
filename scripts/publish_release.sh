#!/usr/bin/env bash
# D3 · 发 GitHub Release 并上传资产。用 REST API（本机与 CI 都不装 gh CLI）。
#
#   GITHUB_TOKEN=... ./publish_release.sh <build_dir> <tag> [stable|prerelease]
#
# 客户端只认 releases/latest/download/manifest.json：
#   * channel=stable     -> make_latest=true，latest 指到这个 tag
#   * channel=prerelease -> prerelease=true + make_latest=false，**不动 latest**
#     （schema_version 变更时走这条，见 contracts/manifest.schema.json 的 channel 字段）
#
# BE-19 重试策略（每次调用都看显式 HTTP 状态码，失败路径一律打印状态码）：
#   * 可重试 = 网络错误/超时（记为 000）、5xx、429、403 二级限流（body 含 rate limit 或 remaining=0）。
#     其余 4xx 一律不重试，立刻 FATAL。
#   * 次数有界：TP_RETRY_ATTEMPTS 次（默认 4 = 1 + 重试 3），退避 TP_RETRY_DELAY×3^(n-1) 秒（默认 5/15/45），
#     服务端给 Retry-After 时取较大者，封顶 60 秒。
#   * GET / DELETE / PATCH 幂等，直接重试；DELETE 的 404 = 已经删掉，算成功。
#   * 建 draft 的 POST 不幂等（响应丢了但服务端已建 → 盲重试会留两个同 tag 的 draft）：每次尝试前
#     先列表并删掉同 tag 的全部 Release/draft；publish 前再查一遍，删掉晚到的孤儿 draft。
#   * 上传资产 POST 不幂等（盲重试得 422 already_exists）：失败后先列 draft 的资产、删掉同名的
#     （含 state=starter 的半截资产），再重试；publish 前对账，资产必须齐且全部 state=uploaded。
#   * 只有最后的 PATCH 才会动 latest；之前任何一步 FATAL 都只留下 draft，latest 不受影响，
#     下一次运行开头会删掉这个 draft。
#   * 不用 curl --retry：它会盲重试不幂等的 POST，也不认 403 二级限流；重试循环在本脚本里显式写。
set -euo pipefail

BUILD_DIR="${1:?用法: publish_release.sh <build_dir> <tag> [stable|prerelease]}"
TAG="${2:?缺 tag}"
CHANNEL="${3:-stable}"
REPO="${GITHUB_REPOSITORY:-jiaweizhu301/transport-peek-data}"
API="https://api.github.com/repos/${REPO}"
UPLOADS="https://uploads.github.com/repos/${REPO}"
: "${GITHUB_TOKEN:?需要 GITHUB_TOKEN（Actions 里用 secrets.GITHUB_TOKEN，权限 contents: write）}"
ATTEMPTS="${TP_RETRY_ATTEMPTS:-4}"
DELAY="${TP_RETRY_DELAY:-5}"

hdr=(-H "Authorization: Bearer ${GITHUB_TOKEN}"
     -H "Accept: application/vnd.github+json"
     -H "X-GitHub-Api-Version: 2022-11-28")

if [ "$CHANNEL" = "prerelease" ]; then PRE=true; LATEST='"false"'; else PRE=false; LATEST='"true"'; fi

TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT
RESP="$TMP/resp"; HDRS="$TMP/hdrs"
CODE=000
WHAT=""
KEEP="（latest 不受影响）"

die() { echo "FATAL $*" >&2; exit 1; }

# 单次请求：响应体落 $RESP，响应头落 $HDRS，状态码进 $CODE（curl 本身失败/超时 = 000）
api_once() {
  local method="$1" url="$2"; shift 2
  : > "$RESP"; : > "$HDRS"
  CODE=$(curl -sS --connect-timeout 20 --max-time "${TP_MAX_TIME:-300}" -o "$RESP" -D "$HDRS" \
         -w '%{http_code}' -X "$method" "${hdr[@]}" "$@" "$url") || CODE=000
}

# 可重试？只有 000 / 5xx / 429 / 403 二级限流
retryable() {
  case "$CODE" in
    000|5??|429) return 0 ;;
    403) grep -qi 'rate limit' "$RESP" && return 0
         tr -d '\r' < "$HDRS" | grep -qi '^x-ratelimit-remaining: *0$' && return 0
         return 1 ;;
    *) return 1 ;;
  esac
}

# 第 n 次失败后的退避
backoff() {
  local n="$1" s ra
  s=$(( DELAY * 3 ** (n - 1) ))
  ra=$(tr -d '\r' < "$HDRS" | sed -n 's/^[Rr]etry-[Aa]fter: *\([0-9][0-9]*\)$/\1/p' | head -n1)
  if [ -n "$ra" ] && [ "$ra" -gt "$s" ]; then s=$ra; fi
  if [ "$s" -gt 60 ]; then s=60; fi
  echo "  WARN ${WHAT}：HTTP ${CODE}（第 ${n}/${ATTEMPTS} 次），${s} 秒后重试" >&2
  sleep "$s"
}

errbody() { head -c 300 "$RESP" | tr '\n' ' '; }

# 幂等调用：api <描述> <可接受状态码，空格分隔> <METHOD> <URL> [curl 参数…]
# 成功返回 0（响应体在 $RESP）；不可重试或次数耗尽返回 1（$CODE 是最后一次的状态码）
api() {
  WHAT="$1"; local ok=" $2 " method="$3" url="$4" n; shift 4
  for (( n = 1; n <= ATTEMPTS; n++ )); do
    api_once "$method" "$url" "$@"
    case "$ok" in *" $CODE "*) return 0 ;; esac
    if ! retryable; then
      echo "  ${WHAT}：HTTP ${CODE}，不可重试：$(errbody)" >&2
      return 1
    fi
    if [ "$n" -lt "$ATTEMPTS" ]; then backoff "$n"; fi
  done
  echo "  ${WHAT}：HTTP ${CODE}，${ATTEMPTS} 次都失败：$(errbody)" >&2
  return 1
}

# 同 tag 的 Release/draft id -> $TAG_IDS（/releases/tags/{tag} 查不到 draft，所以从列表里找）
TAG_IDS=""
ids_for_tag() {
  api "列 Release" 200 GET "${API}/releases?per_page=100" || return 1
  TAG_IDS=$(python -c 'import json,sys
d=json.load(open(sys.argv[1],encoding="utf-8"))
if not isinstance(d,list): sys.exit("列 Release 返回的不是列表")
print(" ".join(str(r["id"]) for r in d if r.get("tag_name")==sys.argv[2]))' "$RESP" "$TAG")
}

delete_release() {
  api "删 Release id=$1" "204 404" DELETE "${API}/releases/$1"
}

# 已存在同名 tag 的 Release 就先删掉（同一天重跑 / 手动 dispatch 重试 / 上次跑到一半留下的 draft）
clean_tag() {
  local id
  ids_for_tag || die "列 Release 失败：HTTP ${CODE}${KEEP}"
  for id in $TAG_IDS; do
    echo "已存在同 tag 的 Release/draft ${TAG}（id=${id}），先删除后重发"
    delete_release "$id" || die "删 Release id=${id} 失败：HTTP ${CODE}${KEEP}"
  done
}

python - "$BUILD_DIR" "$TAG" "$CHANNEL" "$PRE" > "$TMP/create.json" <<'PY'
import json, os, sys
d, tag, channel, pre = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]
m = json.load(open(os.path.join(d, 'manifest.json'), encoding='utf-8'))
lines = ['TransportPeek 时刻表数据 · %s（channel=%s）' % (tag, channel), '',
         '| mode | static_version | calendar | size |', '|---|---|---|---:|']
for mode, f in m['feeds'].items():
    lines.append('| %s | %s | %d–%d | %.2f MB |' % (
        mode, f['static_version'], f['calendar_start'], f['calendar_end'],
        f['size_bytes'] / 1e6))
lines += ['', 'Data © Transport for NSW，按 CC BY 4.0 使用。归属说明见 ' + m['attribution_url']]
print(json.dumps({'tag_name': tag, 'name': tag, 'body': '\n'.join(lines),
                  'draft': True, 'prerelease': pre == 'true'}))
PY

# 先建成 draft。若一上来就 make_latest=true，Release 建好的那一瞬间 latest 就指过来了，
# 而 sydneytrains.sqlite.gz 还要再传几十秒 —— 这段窗口里客户端打开
# releases/latest/download/manifest.json 会拿到 404。资产传齐再 publish。
rel_id=""
for (( n = 1; n <= ATTEMPTS; n++ )); do
  clean_tag   # 每次尝试前都清：上一次 POST 若服务端已建、只是响应丢了，这里把它删掉
  WHAT="建 draft"
  api_once POST "${API}/releases" -H "Content-Type: application/json" --data-binary @"$TMP/create.json"
  if [ "$CODE" = "201" ]; then
    rel_id=$(python -c 'import json,sys;print(json.load(open(sys.argv[1],encoding="utf-8"))["id"])' "$RESP")
    break
  fi
  retryable || die "建 draft 失败：HTTP ${CODE}，不可重试：$(errbody)${KEEP}"
  if [ "$n" -lt "$ATTEMPTS" ]; then backoff "$n"; fi
done
[ -n "$rel_id" ] || die "建 draft 失败：HTTP ${CODE}，${ATTEMPTS} 次都失败${KEEP}"
echo "Release ${TAG} 草稿已建 id=${rel_id} prerelease=${PRE}"
KEEP="（草稿 ${TAG} 未 publish，latest 不受影响）"

# 删掉 draft 里同名资产（上一次上传写了一半 / 响应丢了但其实已传上）
drop_asset() {
  local name="$1" ids id
  api "列资产" 200 GET "${API}/releases/${rel_id}/assets?per_page=100" || return 1
  ids=$(python -c 'import json,sys
d=json.load(open(sys.argv[1],encoding="utf-8"))
print(" ".join(str(a["id"]) for a in d if a.get("name")==sys.argv[2]))' "$RESP" "$name")
  for id in $ids; do
    echo "  删除半截/重复资产 ${name}（asset id=${id}）"
    api "删资产 ${name}" "204 404" DELETE "${API}/releases/assets/${id}" || return 1
  done
}

uploaded=0
: > "$TMP/names"
upload() {
  local f="$1" name ctype n c
  name=$(basename "$f")
  case "$name" in *.gz) ctype=application/gzip ;; *.json) ctype=application/json ;;
                  *) ctype=application/octet-stream ;; esac
  echo "  上传 ${name} ($(python -c 'import os,sys;print("%.2f MB"%(os.path.getsize(sys.argv[1])/1e6))' "$f"))"
  # curl 遇到 4xx/5xx 默认仍然 exit 0。不看状态码的话，资产上传失败会被当成发布成功，
  # 客户端就会拿到一个缺资产的 latest。显式检查 HTTP 201。
  for (( n = 1; n <= ATTEMPTS; n++ )); do
    WHAT="上传 ${name}"
    TP_MAX_TIME="${TP_UPLOAD_MAX_TIME:-600}" api_once POST "${UPLOADS}/releases/${rel_id}/assets?name=${name}" \
      -H "Content-Type: ${ctype}" --data-binary @"$f"
    if [ "$CODE" = "201" ]; then
      uploaded=$((uploaded+1)); echo "$name" >> "$TMP/names"; return 0
    fi
    # 422 already_exists = 同名资产已在（之前某次上传其实成功了/写了一半）：删掉再传，不算盲重试
    if retryable || { [ "$CODE" = "422" ] && grep -q already_exists "$RESP"; }; then
      [ "$n" -lt "$ATTEMPTS" ] || break
      c="$CODE"
      drop_asset "$name" || die "上传 ${name} 后清理同名资产失败：HTTP ${CODE}${KEEP}"
      CODE="$c"; WHAT="上传 ${name}"
      backoff "$n"
    else
      die "上传 ${name} 失败：HTTP ${CODE}，不可重试：$(errbody)${KEEP}"
    fi
  done
  die "上传 ${name} 失败：HTTP ${CODE}，${ATTEMPTS} 次都失败${KEEP}"
}

upload "${BUILD_DIR}/manifest.json"
upload "${BUILD_DIR}/config.json"
# 非营运 headsign 词表：与 fixtures/non_revenue_headsigns.json 同名同源（build_db 的常量），
# 客户端拿真库时按 stop_parents 同样的 base URL 取它
upload "${BUILD_DIR}/non_revenue_headsigns.json"
# 解析规则（PLATFORM_RE 等）：同上，客户端 Regex(读到的字符串) 直接用
upload "${BUILD_DIR}/parsing_rules.json"
# XBE-02：stop_ops.json（公交 STOP_OPS）+ valid_stops.<mode>.json（有效站表），WP-06 的 Worker 按同源 URL 取
for f in "${BUILD_DIR}"/*.sqlite.gz "${BUILD_DIR}"/stop_parents.*.json "${BUILD_DIR}"/shapes.*.bin.gz \
         "${BUILD_DIR}"/stop_ops.json "${BUILD_DIR}"/valid_stops.*.json; do
  if [ -e "$f" ]; then upload "$f"; fi
done
if [ "$uploaded" -lt 7 ]; then
  echo "FATAL 只传了 ${uploaded} 个资产（至少应有 manifest/config/词表/解析规则/1 个库/1 个 stop_parents/1 个 valid_stops）" >&2
  exit 1
fi

# publish 前对账：draft 上的资产必须正好是这次传的这些、且全部 state=uploaded（没有半截资产）
api "对账资产" 200 GET "${API}/releases/${rel_id}/assets?per_page=100" \
  || die "对账资产失败：HTTP ${CODE}${KEEP}"
python -c 'import json,sys
d=json.load(open(sys.argv[1],encoding="utf-8"))
want=sorted(open(sys.argv[2],encoding="utf-8").read().split())
got=sorted(a["name"] for a in d)
bad=[a["name"] for a in d if a.get("state")!="uploaded"]
if got!=want or bad:
    sys.exit("资产对账不符：应有 %d 个，实有 %d 个，未完成 %s" % (len(want),len(got),bad))' "$RESP" "$TMP/names" \
  || die "资产对账不符${KEEP}"

# 晚到的孤儿 draft（某次建 draft 超时、服务端稍后才建成）：publish 前删掉，只留本次这一个
ids_for_tag || die "publish 前复查 Release 失败：HTTP ${CODE}${KEEP}"
for id in $TAG_IDS; do
  [ "$id" = "$rel_id" ] && continue
  echo "删除同 tag 的孤儿 draft id=${id}"
  delete_release "$id" || die "删孤儿 draft id=${id} 失败：HTTP ${CODE}${KEEP}"
done

# 资产齐了才 publish，此时才按 channel 决定动不动 latest（同一 body 重放 PATCH 是幂等的）
api "publish" 200 PATCH "${API}/releases/${rel_id}" -H "Content-Type: application/json" \
  --data-binary "{\"draft\": false, \"prerelease\": ${PRE}, \"make_latest\": ${LATEST}}" \
  || die "publish 失败：HTTP ${CODE}（资产已齐；若响应丢失，Release 可能其实已发布，看 Releases 页确认）"
echo "Release ${TAG} 已发布：${uploaded} 个资产，prerelease=${PRE} make_latest=${LATEST}"

echo "完成。客户端入口：https://github.com/${REPO}/releases/latest/download/manifest.json"
