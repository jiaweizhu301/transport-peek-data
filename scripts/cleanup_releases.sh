#!/usr/bin/env bash
# D5 · Release 自动清理：删掉超过保留期的 Release 及其 tag。
#
#   GITHUB_TOKEN=... ./cleanup_releases.sh [保留天数，默认 14] [--dry-run]
#
# 安全阀：latest（当前 stable）与最近 3 个 Release 永不删，避免客户端正在下载时被抽掉。
set -euo pipefail

KEEP_DAYS="${1:-14}"
DRY="${2:-}"
REPO="${GITHUB_REPOSITORY:-jiaweizhu301/transport-peek-data}"
API="https://api.github.com/repos/${REPO}"
: "${GITHUB_TOKEN:?需要 GITHUB_TOKEN}"

hdr=(-H "Authorization: Bearer ${GITHUB_TOKEN}"
     -H "Accept: application/vnd.github+json"
     -H "X-GitHub-Api-Version: 2022-11-28")

latest_tag=$(curl -sS "${hdr[@]}" "${API}/releases/latest" | python -c \
  'import json,sys;print(json.load(sys.stdin).get("tag_name",""))' || true)

curl -sS "${hdr[@]}" "${API}/releases?per_page=100" | python - "$KEEP_DAYS" "$latest_tag" <<'PY' > /tmp/tp-stale.txt
import datetime, json, sys
keep_days, latest = int(sys.argv[1]), sys.argv[2]
rels = json.load(sys.stdin)
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
done < /tmp/tp-stale.txt

echo "清理完成：保留 ${KEEP_DAYS} 天，本次处理 ${n} 个（latest=${latest_tag} 与最近 3 个已豁免）"
