#!/usr/bin/env bash
# D5 · Release 自动清理：删掉超过保留期的 Release 及其 tag。
#
#   GITHUB_TOKEN=... ./cleanup_releases.sh [保留天数，默认 14] [--dry-run]
#
# 安全阀：latest（当前 stable）与最近 3 个 Release 永不删，避免客户端正在下载时被抽掉。
# 只按 created_at 算年龄，不看 tag 名：日期 tag、`*-pre` prerelease tag、草稿一视同仁。
set -euo pipefail

KEEP_DAYS="${1:-14}"
DRY="${2:-}"
REPO="${GITHUB_REPOSITORY:-jiaweizhu301/transport-peek-data}"
API="https://api.github.com/repos/${REPO}"
: "${GITHUB_TOKEN:?需要 GITHUB_TOKEN}"

hdr=(-H "Authorization: Bearer ${GITHUB_TOKEN}"
     -H "Accept: application/vnd.github+json"
     -H "X-GitHub-Api-Version: 2022-11-28")

TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

# 显式看状态码：latest 查不到就不清理（否则 latest_tag 为空，latest 只剩「最近 3 个」这一道保护，
# 最近 3 个若都是 *-pre prerelease，latest 就可能被删）。列表同理。
get() {   # get <url> <out>：200 才算成功，最多 3 次（000/5xx/429 重试）
  local code n
  for n in 1 2 3; do
    code=$(curl -sS --connect-timeout 20 --max-time 60 -o "$2" -w '%{http_code}' "${hdr[@]}" "$1") || code=000
    [ "$code" = "200" ] && return 0
    case "$code" in 000|5??|429) [ "$n" -lt 3 ] && sleep $(( n * ${TP_RETRY_DELAY:-5} )) ;; *) break ;; esac
  done
  echo "FATAL GET $1 失败：HTTP ${code}（本次不清理任何 Release）" >&2
  return 1
}
get "${API}/releases/latest" "$TMP/latest.json" || exit 1
latest_tag=$(python -c 'import json,sys;print(json.load(open(sys.argv[1],encoding="utf-8")).get("tag_name",""))' "$TMP/latest.json")
[ -n "$latest_tag" ] || { echo "FATAL latest 没有 tag_name（本次不清理任何 Release）" >&2; exit 1; }

# 注意：不能写成 `curl ... | python - args <<PY`。`python -` 从 stdin 读脚本，heredoc 也占 stdin，
# heredoc 会盖掉管道 —— Python 拿到脚本后 sys.stdin 已耗尽，json.load 必然炸。这里改为先落盘再传路径。
get "${API}/releases?per_page=100" "$TMP/rels.json" || exit 1
python - "$KEEP_DAYS" "$latest_tag" "$TMP/rels.json" <<'PY' > "$TMP/stale.txt"
import datetime, json, sys
keep_days, latest = int(sys.argv[1]), sys.argv[2]
with open(sys.argv[3], encoding="utf-8") as fh:
    rels = json.load(fh)
# API 出错时返回的是 {"message": ...} 而不是列表；直接 .sort() 会抛看不懂的 AttributeError。
if not isinstance(rels, list):
    msg = rels.get("message", rels) if isinstance(rels, dict) else rels
    sys.stderr.write("FAIL 列出 Release 失败（token 或网络）：%s\n" % msg)
    sys.exit(1)
rels.sort(key=lambda r: r['created_at'], reverse=True)
cutoff = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=keep_days)
for i, r in enumerate(rels):
    if i < 3 or r['tag_name'] == latest:      # 最近 3 个 + latest 永不删
        continue
    created = datetime.datetime.fromisoformat(r['created_at'].replace('Z', '+00:00'))
    if created < cutoff:
        print('%s %s %s' % (r['id'], r['tag_name'], r['created_at']))
PY

n=0
while read -r id tag created; do
  [ -z "${id:-}" ] && continue
  n=$((n+1))
  if [ "$DRY" = "--dry-run" ]; then
    echo "[dry-run] 会删除 Release ${tag} (${created})"
    continue
  fi
  echo "删除 Release ${tag} (${created})"
  curl -sS -X DELETE "${hdr[@]}" "${API}/releases/${id}" >/dev/null
  curl -sS -X DELETE "${hdr[@]}" "${API}/git/refs/tags/${tag}" >/dev/null || true
done < "$TMP/stale.txt"

echo "清理完成：保留 ${KEEP_DAYS} 天，本次处理 ${n} 个（latest=${latest_tag} 与最近 3 个已豁免）"
