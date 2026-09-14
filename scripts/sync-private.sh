#!/usr/bin/env bash
# 把 private/tdxtap-source 与上游同步。用 merge 而非 rebase：历史保留、
# 同一冲突只解一次、不改写已推送的 commit。这条分支不提 PR，线性历史无意义。
set -euo pipefail
cd "$(dirname "$0")/.."

git checkout private/tdxtap-source
git fetch upstream
git merge upstream/main

# 版本号读的是 importlib.metadata，安装时冻结；光 pull 会让它停在旧值
VIRTUAL_ENV=.venv uv pip install -e . --no-deps

cd agent && ../.venv/bin/python -m pytest tests/test_tdxtap_loader.py -q
cd .. && PATH="$PWD/.venv/bin:$PATH" bash tools/ci_grep_gates.sh

echo "同步完成。自有改动分两类："
echo "  1) 已提上游 PR #1441、合并后应从本分支撤掉的部分："
echo "     agent/backtest/runner.py 与 agent/backtest/loaders/registry.py 里的"
echo "     _NO_NETWORK_FALLBACK_SOURCES 按标的守卫。"
echo "  2) 长期留在本分支、不打算上游化的 tdxtap 专属改动："
echo "     agent/backtest/loaders/registry.py 的四处 PRIVATE 标记、"
echo "     agent/backtest/loaders/tdxtap_loader.py、"
echo "     六份 README（README.md / _zh / _ja / _ko / _ar / _es）的数据源表、"
echo "     加载器树行与散文计数、agent/backtest/metrics.py 的年化换算表、"
echo "     agent/SKILL.md 的数据源计数。"
echo "合并冲突若落在这两类文件里，对照上表判断该保留私有版本还是让位给上游。"
