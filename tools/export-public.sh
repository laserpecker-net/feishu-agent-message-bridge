#!/usr/bin/env bash
# 打一个**可以公开**的仓库快照：去私有材料 → 扫敏感词 → 单提交初始化。
#
# 为什么不直接 push 现有仓库：历史 commit 里必然留着早期真名/内网地址/机器名，
# 只清当前文件挡不住 `git log -p`。公开版用**全新单提交**，私有历史留在本机。
#
# 用法：bash tools/export-public.sh [--out <目录>]
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT="/tmp/feishu-bridge-public"
if [ "${1:-}" = "--out" ] && [ -n "${2:-}" ]; then
  OUT="$2"
elif [ "$#" -ne 0 ]; then
  echo "用法：bash tools/export-public.sh [--out <目录>]" >&2
  exit 2
fi

echo "== 1. 复制工作树（排除私有材料）=="
rm -rf "$OUT"
mkdir -p "$OUT"
rsync -a "$REPO_ROOT/" "$OUT/" \
  --exclude '.git/' --exclude 'archive/' --exclude 'dist/' \
  --exclude 'coordination/' --exclude 'research/' --exclude 'reviews/' \
  --exclude 'draft_*' --exclude '__pycache__/' --exclude '.pytest_cache/' \
  --exclude '.codegraph/' --exclude '.DS_Store' \
  --exclude 'docs/windows-hosts.csv' --exclude 'docs/windows-hosts.csv.*' \
  --exclude '*.sqlite3' --exclude '*.sqlite3-*'

echo "== 2. 扫敏感词（与脱敏门禁同一份词表）=="
cd "$OUT"
python3 "$REPO_ROOT/tools/scan-private.py"

echo "== 3. 单提交初始化 =="
git init -q -b main
git add -A
git -c user.name='feishu-bridge' -c user.email='feishu-bridge@example.com' \
  commit -q -m "飞书个人助手：公开初始版本"
echo
echo "完成：$OUT"
echo "  commit: $(git rev-parse --short HEAD)"
echo "  文件数: $(git ls-files | wc -l | tr -d ' ')"
echo "下一步：在 GitHub 建空仓库 → git remote add origin <url> → git push -u origin main"
