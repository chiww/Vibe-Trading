#!/usr/bin/env bash
# 把 private/tdxtap-source 与上游同步。用 merge 而非 rebase：历史保留、
# 同一冲突只解一次、不改写已推送的 commit。这条分支不提 PR，线性历史无意义。
set -euo pipefail
cd "$(dirname "$0")/.."

# 脏树守卫：下面的 git checkout 会切分支，工作区里未提交的改动会被一起带进
# merge，事后很难分清哪些行是上游的、哪些是自己没提交完的。宁可在这里停。
if [ -n "$(git status --porcelain)" ]; then
  echo "工作区不干净，先提交或 stash 再同步：" >&2
  git status --short >&2
  exit 1
fi

git checkout private/tdxtap-source
git fetch upstream
git merge upstream/main

# 版本号读的是 importlib.metadata，安装时冻结；光 pull 会让它停在旧值
VIRTUAL_ENV=.venv uv pip install -e . --no-deps

# 跑的是「上游 merge 最可能打破的那些」，不是「我们自己写的那个」。
# 私有改动全部寄生在上游的结构上：文档守卫按注册源集合断言、路由与口径表
# 按源名断言、年化表从 local 派生。上游动了这些结构，先红的是这几个文件，
# 而 test_tdxtap_loader.py 反而可能照样绿。
cd agent && ../.venv/bin/python -m pytest -q \
  tests/test_tdxtap_loader.py \
  tests/test_readme_counts.py \
  tests/test_local_source_routing.py \
  tests/test_registry.py \
  tests/test_price_caliber.py \
  tests/test_metrics.py \
  tests/test_distribution_skill_manifest.py
cd .. && PATH="$PWD/.venv/bin:$PATH" bash tools/ci_grep_gates.sh

cat <<'NAV'
同步完成。自有改动的导航图（grep -rn "PRIVATE" 可复查）：

  agent/backtest/loaders/tdxtap_loader.py   整份新文件，永不冲突
  agent/backtest/loaders/registry.py        VALID_SOURCES / _loader_modules /
                                            _NO_NETWORK_FALLBACK_SOURCES（含 hint）/
                                            PRICE_CALIBER_BY_SOURCE_MARKET /
                                            _PRIVATE_SOURCES（本分支自有的常量）
  agent/backtest/metrics.py                 尾部一个派生块（_TRADING_DAYS /
                                            _BARS_PER_DAY 比照 local 取值）
  agent/backtest/runner.py                  _get_loader 的重抛、fetch_data_map 的
                                            按标的守卫、main() 的 JSON 信封
  agent/backtest/benchmark.py               offline 判据改问 registry 谓词、
                                            _infer_market 认 tdxtap 为 A 股源
  agent/tests/test_readme_counts.py         三个检查减去 _PRIVATE_SOURCES
  agent/tests/test_distribution_skill_manifest.py  数据源计数减去 _PRIVATE_SOURCES
  agent/tests/test_tdxtap_loader.py         整份新文件
  agent/tests/test_local_source_routing.py  上游 PR #1441 配套的回归用例

六份 README 与 agent/SKILL.md **不是**自有改动点：它们的计数守卫改成了对
上游源集合做减集，所以文档本身与上游逐字一致，永远不会在这里冲突。

冲突处理：一律取上游版本，再把上表里对应的私有改动重新叠上去。
上游合并 #1441 之后**不要**手工 revert 本分支的 commit —— 三方 merge 会自然
识别共同祖先并收敛；手工反向修改只会在上游内容之上再造一次改动。
NAV
