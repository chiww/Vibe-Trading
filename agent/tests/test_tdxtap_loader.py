"""tdxtap 快照 loader。

数据放在 ``~/.vibe-trading/data-bridge/tdxtap/`` 而不是 ``~/.tdxtap``：回测子
进程以沙箱用户降权运行，只有 cache / data-bridge / qveris.json 被重新暴露。
"""

from __future__ import annotations

import json

import pandas as pd
import pytest

from backtest.loaders import tdxtap_loader
from backtest.loaders.tdxtap_loader import DataLoader


@pytest.fixture
def snapshot(tmp_path, monkeypatch):
    """造一份合成快照：A股 / 港股 / 美股各一只。"""
    root = tmp_path / "tdxtap"
    entries = {}
    for market, symbol, adjustment in [
        ("a_share", "000001.SZ", "split_dividend"),
        ("hk_equity", "00700.HK", "raw"),
        ("us_equity", "AAPL.US", "raw"),
    ]:
        index = pd.date_range("2024-01-02", periods=10, freq="B")
        frame = pd.DataFrame(
            {"open": [10.0] * 10, "high": [11.0] * 10, "low": [9.0] * 10,
             "close": [10.5] * 10, "volume": [1000.0] * 10},
            index=index,
        )
        frame.index.name = "trade_date"
        rel = f"{market}/{symbol}.parquet"
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        frame.to_parquet(path)
        entries[f"{market}/{symbol}"] = {
            "path": rel, "rows": 10, "start": "2024-01-02", "end": "2024-01-15",
            "adjustment": adjustment, "source": "tdx_std",
            "volume_unit": "shares", "sha256": "0" * 64,
            "fetched_at": "2026-09-14 10:00:00",
        }
    (root / "manifest.json").write_text(
        json.dumps({"entries": entries}, ensure_ascii=False), encoding="utf-8"
    )
    monkeypatch.setattr(tdxtap_loader, "SNAPSHOT_DIR", root)
    monkeypatch.setattr(tdxtap_loader, "MANIFEST", root / "manifest.json")
    return root


def test_available_when_the_manifest_has_entries(snapshot):
    assert DataLoader().is_available() is True


def test_unavailable_without_a_manifest(tmp_path, monkeypatch):
    monkeypatch.setattr(tdxtap_loader, "SNAPSHOT_DIR", tmp_path / "missing")
    monkeypatch.setattr(tdxtap_loader, "MANIFEST", tmp_path / "missing" / "manifest.json")
    assert DataLoader().is_available() is False


def test_unavailable_when_the_manifest_is_corrupt(snapshot):
    (snapshot / "manifest.json").write_text("{ not json", encoding="utf-8")
    assert DataLoader().is_available() is False


def test_fetch_returns_the_loader_frame_contract(snapshot):
    out = DataLoader().fetch(["000001.SZ"], "2024-01-02", "2024-01-15")
    frame = out["000001.SZ"]
    assert list(frame.columns) == ["open", "high", "low", "close", "volume"]
    assert frame.index.name == "trade_date"
    assert str(frame.index.dtype) == "datetime64[ns]"
    assert len(frame) == 10


def test_fetch_serves_all_three_markets(snapshot):
    out = DataLoader().fetch(["000001.SZ", "00700.HK", "AAPL.US"],
                             "2024-01-02", "2024-01-15")
    assert sorted(out) == ["000001.SZ", "00700.HK", "AAPL.US"]


def test_fetch_slices_by_date(snapshot):
    out = DataLoader().fetch(["000001.SZ"], "2024-01-05", "2024-01-09")
    assert out["000001.SZ"].index.min() >= pd.Timestamp("2024-01-05")
    assert out["000001.SZ"].index.max() <= pd.Timestamp("2024-01-09")


def test_symbol_absent_from_the_manifest_is_simply_not_returned(snapshot):
    """loader 只负责报告它有什么；缺口由 runner 决定是抛还是回落。"""
    out = DataLoader().fetch(["000001.SZ", "600000.SH"], "2024-01-02", "2024-01-15")
    assert sorted(out) == ["000001.SZ"]


def test_declares_shares_for_every_market(snapshot):
    """tdxtap 原生统一输出股。本仓库其余 A 股源声明 lots，差异由 per-symbol
    provenance 暴露——这正是 DataLoaderProtocol 规定的用法，不要伪装成 lots。"""
    assert DataLoader.volume_units == {
        "a_share": "shares", "hk_equity": "shares", "us_equity": "shares",
    }


def test_markets_and_auth(snapshot):
    assert DataLoader.markets == {"a_share", "hk_equity", "us_equity"}
    assert DataLoader.requires_auth is False


def test_registered_and_source_is_valid():
    from backtest.loaders.registry import VALID_SOURCES, LOADER_REGISTRY, _ensure_registered
    _ensure_registered()
    assert "tdxtap" in VALID_SOURCES
    assert LOADER_REGISTRY["tdxtap"].name == "tdxtap"


def test_never_degrades_to_a_network_source():
    from backtest.loaders.registry import _NO_NETWORK_FALLBACK_SOURCES
    assert "tdxtap" in _NO_NETWORK_FALLBACK_SOURCES


def test_not_in_any_fallback_chain():
    """显式点名才用。进链会制造「以为吃到快照其实没吃到」的中间态，
    也会让用户已设的 MARKET_DATA_ORDER_* 变成非排列而被打回默认序。"""
    from backtest.loaders.registry import FALLBACK_CHAINS
    for market, chain in FALLBACK_CHAINS.items():
        assert "tdxtap" not in chain, market


def test_price_caliber_is_declared_per_market():
    """local 的口径恒为 unknown，这正是独立 source 买到的东西。"""
    from backtest.loaders.registry import price_caliber
    assert price_caliber("tdxtap", "a_share") == "split_dividend"
    assert price_caliber("tdxtap", "hk_equity") == "raw"
    assert price_caliber("tdxtap", "us_equity") == "raw"


def test_a_share_routes_to_the_china_engine():
    from backtest.engines.china_a import ChinaAEngine
    from backtest.runner import _create_market_engine
    engine = _create_market_engine("tdxtap", {"initial_cash": 100_000}, ["000001.SZ"])
    assert isinstance(engine, ChinaAEngine)
