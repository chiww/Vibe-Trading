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

from backtest.loaders.base import (
    cached_loader_fetch,
    validate_date_range,
    validate_ohlc,
)
from backtest.loaders.registry import register

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


def _by_symbol() -> dict[str, dict]:
    """``symbol -> 条目``。清单的键是 ``market/symbol``。"""
    out: dict[str, dict] = {}
    for key, meta in _entries().items():
        _, _, symbol = key.partition("/")
        if symbol:
            out[symbol] = meta
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
        """按清单取 OHLCV。清单里没有的标的直接跳过——是否报错由调用方决定。"""
        validate_date_range(start_date, end_date)
        if interval not in ("1D", "1d"):
            logger.warning(
                "tdxtap 快照只有日线，interval=%s 无法提供；按日线返回", interval
            )
        index = _by_symbol()
        result: Dict[str, pd.DataFrame] = {}
        for code in codes:
            meta = index.get(code)
            if meta is None:
                logger.warning("tdxtap: 快照里没有 %s", code)
                continue
            try:
                frame = cached_loader_fetch(
                    source=self.name,
                    symbol=code,
                    timeframe=interval,
                    start_date=start_date,
                    end_date=end_date,
                    fields=None,
                    fetch=lambda m=meta: self._read(m, start_date, end_date),
                )
            except Exception as exc:
                logger.warning("tdxtap: 读 %s 失败: %s", code, exc)
                continue
            if frame is not None and not frame.empty:
                result[code] = frame
        return result

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
