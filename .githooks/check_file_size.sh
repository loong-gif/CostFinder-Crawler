#!/bin/sh
# 暂存的新增或修改文件超过 5,000 token（字节数 / 4，即 20,000 字节）就拒绝提交
# 跳过 agent 不会阅读的文件：lockfile、自动生成的代码、构建产物、归档、迁移、JSON 数据
set -eu
cd "$(git rev-parse --show-toplevel)"
LIMIT_BYTES=20000
status=0
for path in $(git diff --cached --name-only --diff-filter=ACM); do
  case "$path" in
    archive/*|supabase/migrations/*|.pnpm-store/*|node_modules/*|dist/*|build/*) continue ;;
    *.json|*.lock|*-lock.yaml|*-lock.json|*.min.js|*.map|*.csv|*.png|*.jpg|*.jpeg|*.gif|*.pdf|*.tgz|*.db) continue ;;
  esac
  size=$(git cat-file -s ":$path")
  if [ "$size" -gt "$LIMIT_BYTES" ]; then
    echo "拒绝提交: $path 大小 $size 字节（约 $((size / 4)) token），超过 $LIMIT_BYTES 字节（5,000 token）的上限。请按功能拆成几个文件后再提交。" >&2
    status=1
  fi
done
exit $status
