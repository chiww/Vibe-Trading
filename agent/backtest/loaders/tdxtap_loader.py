"""tdxtap 快照 loader：读 ``~/.vibe-trading/data-bridge/tdxtap/``。

数据放在下游自己的地盘而不是 ``~/.tdxtap``，因为回测子进程以沙箱用户降权
运行，只有 ``cache`` / ``data-bridge`` / ``qveris.json`` 被重新暴露进临时家目录
（``src/core/runner.py`` 的 ``_SANDBOX_HOME_REEXPOSE``），而 ``TDXTAP_HOME``
也不在子进程环境白名单里。``tdxtap export vibe-trading`` 负责把快照复制过来。
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Dict, List, Optional

import pandas as pd

from backtest.loaders.base import validate_date_range, validate_ohlc
from backtest.loaders.registry import price_caliber, register

logger = logging.getLogger(__name__)

SNAPSHOT_DIR = Path.home() / ".vibe-trading" / "data-bridge" / "tdxtap"
MANIFEST = SNAPSHOT_DIR / "manifest.json"

_OHLCV = ["open", "high", "low", "close", "volume"]


def _entries() -> dict[str, dict]:
    """读快照清单；不存在或损坏时按空处理。"""
    try:
        raw = json.loads(MANIFEST.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    entries = raw.get("entries")
    return entries if isinstance(entries, dict) else {}


def _by_symbol() -> dict[str, tuple[str, dict]]:
    """``symbol -> (market, 条目)``。清单的键是 ``market/symbol``。

    market 必须留着：口径核对是按市场比的（A 股前复权，港美股未复权），而
    只拿 symbol 做键会让同名标的跨市场静默互相覆盖——一只港股被当成同名
    A 股喂进回测，全程零告警。歧义在这里被丢掉并告警，缺口随后由
    ``_NO_NETWORK_FALLBACK_SOURCES`` 守卫升级成硬失败。
    """
    out: dict[str, tuple[str, dict]] = {}
    ambiguous: set[str] = set()
    for key, meta in _entries().items():
        market, _, symbol = key.partition("/")
        if not market or not symbol:
            continue
        if symbol in out and out[symbol][0] != market:
            ambiguous.add(symbol)
            continue
        out[symbol] = (market, meta)
    for symbol in ambiguous:
        logger.warning(
            "tdxtap: %s 在快照里跨市场重名（%s 与其它市场），无法判定该用哪一份；跳过",
            symbol,
            out[symbol][0],
        )
        out.pop(symbol, None)
    return out


@register
class DataLoader:
    """tdxtap 快照（通达信取数 CLI 的产物）。"""

    name = "tdxtap"
    markets = {"a_share", "hk_equity", "us_equity"}
    # tdxtap 在落库边界统一归一到股，三个市场一致。本仓库其余 A 股源声明
    # lots；差异由每只票的 provenance 暴露，不在这里抹平。
    volume_units = {"a_share": "shares", "hk_equity": "shares", "us_equity": "shares"}
    requires_auth = False

    def is_available(self) -> bool:
        """快照清单存在且有条目时可用。"""
        return bool(_entries())

    def fetch(
        self,
        codes: List[str],
        start_date: str,
        end_date: str,
        *,
        interval: str = "1D",
        fields: Optional[List[str]] = None,
    ) -> Dict[str, pd.DataFrame]:
        """按清单取 OHLCV。清单里没有的标的直接跳过——是否报错由调用方决定。

        跳过在本源上等于硬失败：``tdxtap`` 在
        ``_NO_NETWORK_FALLBACK_SOURCES`` 里，任何缺口都会被 runner 变成
        ``NoAvailableSourceError``。所以下面每个 ``continue`` 都是「响亮地
        失败」，不是「悄悄少给一只票」。
        """
        validate_date_range(start_date, end_date)
        if interval not in ("1D", "1d"):
            # 快照只有日线。按日线数据做 5 分钟线年化会差 78 倍
            # （calc_bars_per_year("5m", "tdxtap") = 19656），而且这个错误会
            # 一路落到 Sharpe 上。tdxtap 不在任何 FALLBACK_CHAINS 里，拒绝
            # 不会让 auto 链上的探测变脆，所以这里拒绝而不是告警后凑合。
            logger.error(
                "tdxtap 快照只有日线，interval=%s 无法提供；拒绝返回日线冒充 %s，"
                "否则年化换算会按 %s 的粒度做。请把 interval 改成 1D。",
                interval, interval, interval,
            )
            return {}
        index = _by_symbol()
        result: Dict[str, pd.DataFrame] = {}
        for code in codes:
            entry = index.get(code)
            if entry is None:
                logger.warning("tdxtap: 快照里没有 %s", code)
                continue
            market, meta = entry
            if not self._caliber_agrees(code, market, meta):
                continue
            try:
                # 不过 cached_loader_fetch：读本地 parquet 本来就比读缓存快，
                # 而缓存的前提「已结算的历史不再变化」对本源不成立——A 股前复权
                # 序列每次除权都整段重算。
                frame = self._read(meta, start_date, end_date)
            except Exception as exc:
                logger.warning("tdxtap: 读 %s 失败: %s", code, exc)
                continue
            if frame is not None and not frame.empty:
                result[code] = frame
        return result

    @staticmethod
    def _caliber_agrees(code: str, market: str, meta: dict) -> bool:
        """快照自报的口径是否与 registry 的声明一致。

        registry 里的 ``PRICE_CALIBER_BY_SOURCE_MARKET`` 是一句硬编码断言，
        而 manifest 每次导出都带着真实的 ``adjustment``。三条设计要求里只有
        「口径如实声明」没有运行时兜底，这里把它补上：对不上就不要这只票，
        让它走已验证的硬失败路径，而不是把未复权价当成前复权价喂进去。
        """
        declared = price_caliber("tdxtap", market)
        actual = meta.get("adjustment")
        if actual == declared:
            return True
        logger.warning(
            "tdxtap: %s（%s）快照口径为 %s，registry 声明的是 %s；两者不一致时"
            "无法判断价格可比性，跳过该标的",
            code, market, actual, declared,
        )
        return False

    @staticmethod
    def _read(meta: dict, start_date: str, end_date: str) -> pd.DataFrame | None:
        path = SNAPSHOT_DIR / str(meta.get("path", ""))
        if not path.is_file():
            return None
        frame = pd.read_parquet(path)
        if not isinstance(frame.index, pd.DatetimeIndex):
            return None
        # pandas 3 的 to_datetime 默认产出 [us]；本仓库按 ns 解读，混用会把
        # 2024 年读成 1970 年而全程不报错。
        frame.index = frame.index.as_unit("ns")
        frame.index.name = "trade_date"
        frame = frame[[c for c in _OHLCV if c in frame.columns]]
        for col in frame.columns:
            frame[col] = pd.to_numeric(frame[col], errors="coerce")
        frame = frame.dropna(subset=["open", "high", "low", "close"])
        frame = validate_ohlc(frame)
        start = pd.Timestamp(start_date)
        end = pd.Timestamp(end_date) + pd.Timedelta(days=1) - pd.Timedelta(seconds=1)
        frame = frame[(frame.index >= start) & (frame.index <= end)]
        return frame.sort_index() if not frame.empty else None
