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


# ── 取不到数据即报错退出：三条设计要求里的第 2 条 ────────────────────────

def test_unavailable_snapshot_never_degrades_to_tushare(tmp_path, monkeypatch):
    """快照整体不可用时 ``_get_loader`` 必须抛，而不是换成 tushare。

    这是最常见的失守形态：用户忘了跑 ``tdxtap pull --export``、manifest 读到
    一半、或沙箱读不到目录。registry 已正确抛 NoAvailableSourceError，
    runner 的「未知源兜底 tushare」分支却把它吞掉，于是有 token 时静默拿到
    网络数据，无 token 时给出一条指向 tushare 的误导报错。
    """
    from backtest.loaders.base import NoAvailableSourceError
    from backtest.runner import _get_loader

    monkeypatch.setattr(tdxtap_loader, "SNAPSHOT_DIR", tmp_path / "missing")
    monkeypatch.setattr(tdxtap_loader, "MANIFEST", tmp_path / "missing" / "manifest.json")

    with pytest.raises(NoAvailableSourceError) as excinfo:
        _get_loader("tdxtap")
    assert "tdxtap" in str(excinfo.value)


def test_unknown_sources_still_fall_back_to_tushare():
    """不在名单里的源保持原有兜底——本次收窄只针对 no-network 名单。"""
    from backtest.loaders.registry import LOADER_REGISTRY, _ensure_registered
    from backtest.runner import _get_loader

    _ensure_registered()
    assert _get_loader("no-such-source") is LOADER_REGISTRY["tushare"]


def test_fetch_data_map_raises_for_a_symbol_outside_the_snapshot(snapshot):
    """spec 点名的端到端用例：显式 source="tdxtap" 请求一只不在快照的标的，
    ``fetch_data_map`` 在产出 run card 之前抛 NoAvailableSourceError。"""
    from backtest.loaders.base import NoAvailableSourceError
    from backtest.runner import fetch_data_map

    with pytest.raises(NoAvailableSourceError) as excinfo:
        fetch_data_map({
            "codes": ["000001.SZ", "600000.SH"],
            "start_date": "2024-01-02",
            "end_date": "2024-01-15",
            "source": "tdxtap",
            "interval": "1D",
        })
    assert "600000.SH" in str(excinfo.value)


def test_fetch_data_map_serves_the_snapshot_when_every_symbol_is_present(snapshot):
    """反面：篮子齐全时照常返回，且只署 tdxtap。"""
    from backtest.runner import fetch_data_map

    result = fetch_data_map({
        "codes": ["000001.SZ"],
        "start_date": "2024-01-02",
        "end_date": "2024-01-15",
        "source": "tdxtap",
        "interval": "1D",
    })
    assert result.effective_sources == ["tdxtap"]
    assert sorted(result.data_map) == ["000001.SZ"]


def test_cli_reports_a_missing_symbol_as_a_json_envelope(tmp_path, monkeypatch, capsys):
    """缺票即抛是本分支的日常路径，不是边角异常：CLI 必须给 JSON 信封，
    与 main() 里其余失败一致，而不是一屏 traceback。"""
    import json as _json

    from backtest import runner as runner_mod
    from backtest.loaders.base import NoAvailableSourceError

    run_dir = tmp_path / "run"
    (run_dir / "code").mkdir(parents=True)
    (run_dir / "code" / "signal_engine.py").write_text(
        "class SignalEngine:\n    pass\n", encoding="utf-8"
    )
    (run_dir / "config.json").write_text(
        _json.dumps({
            "codes": ["600000.SH"],
            "start_date": "2024-01-02",
            "end_date": "2024-01-15",
            "source": "tdxtap",
        }),
        encoding="utf-8",
    )
    monkeypatch.setenv("VIBE_TRADING_ALLOWED_RUN_ROOTS", str(tmp_path))
    monkeypatch.setattr(runner_mod, "_load_module_from_file",
                        lambda path, name: type("M", (), {"SignalEngine": type("SignalEngine", (), {})}))
    monkeypatch.setattr(runner_mod, "_validate_signal_engine_class", lambda cls: None)

    def _raise(config):
        raise NoAvailableSourceError("incomplete data for source=tdxtap; missing symbols: ['600000.SH']")

    monkeypatch.setattr(runner_mod, "fetch_data_map", _raise)

    with pytest.raises(SystemExit) as excinfo:
        runner_mod.main(run_dir)
    assert excinfo.value.code == 1
    payload = _json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert "600000.SH" in payload["error"]


# ── benchmark 必须遵守同一约定 ───────────────────────────────────────────

def test_benchmark_never_reaches_the_network_for_tdxtap(monkeypatch):
    """``offline`` 曾硬编码为 ``source == "local"``，于是 tdxtap 的基准直接
    走 YfinanceLoader 触网——绕过了这条分支存在的首要理由。"""
    from backtest.benchmark import resolve_benchmark

    created: list[str] = []

    def _record():
        # 不能只靠抛异常：resolve_benchmark 把取数异常吞成 None，那样测试会
        # 因为「触网了但失败了」而通过。记录构造次数才是真正的判据。
        created.append("yfinance")
        raise AssertionError("yfinance loader must not be created for tdxtap")

    monkeypatch.setattr("backtest.benchmark.YfinanceLoader", _record)

    assert resolve_benchmark(
        strategy_codes=["000001.SZ"],
        source="tdxtap",
        start_date="2024-01-02",
        end_date="2024-01-15",
    ) is None
    assert created == []


def test_benchmark_for_an_a_share_basket_is_not_spy():
    """``_infer_market`` 不认识 tdxtap 时，000001.SZ 被判成 us_equity，
    于是一个 A 股回测拿 SPY 当基准。"""
    from backtest.benchmark import _infer_market, _resolve_ticker

    assert _infer_market(["000001.SZ"], "tdxtap") == "a_share"
    assert _resolve_ticker(["000001.SZ"], "tdxtap", None) == "000300.SH"


# ── 口径声明要有运行时核对 ───────────────────────────────────────────────

def test_a_symbol_whose_snapshot_caliber_contradicts_the_registry_is_skipped(
    snapshot, caplog,
):
    """registry 里的口径是硬编码断言，从没人对着 manifest 核过。核不上就
    跳过该标的——因为 tdxtap 在 _NO_NETWORK_FALLBACK_SOURCES 里，跳过会由
    已验证的守卫升级成 NoAvailableSourceError 硬失败。"""
    import json as _json

    manifest = _json.loads((snapshot / "manifest.json").read_text(encoding="utf-8"))
    manifest["entries"]["a_share/000001.SZ"]["adjustment"] = "raw"
    (snapshot / "manifest.json").write_text(_json.dumps(manifest), encoding="utf-8")

    with caplog.at_level("WARNING"):
        out = DataLoader().fetch(["000001.SZ", "00700.HK"], "2024-01-02", "2024-01-15")

    assert sorted(out) == ["00700.HK"]
    assert "000001.SZ" in caplog.text


def test_caliber_mismatch_becomes_a_hard_failure_through_fetch_data_map(snapshot):
    from backtest.loaders.base import NoAvailableSourceError
    from backtest.runner import fetch_data_map
    import json as _json

    manifest = _json.loads((snapshot / "manifest.json").read_text(encoding="utf-8"))
    manifest["entries"]["a_share/000001.SZ"]["adjustment"] = "raw"
    (snapshot / "manifest.json").write_text(_json.dumps(manifest), encoding="utf-8")

    with pytest.raises(NoAvailableSourceError):
        fetch_data_map({
            "codes": ["000001.SZ"],
            "start_date": "2024-01-02",
            "end_date": "2024-01-15",
            "source": "tdxtap",
            "interval": "1D",
        })


# ── interval 非日线要硬失败 ──────────────────────────────────────────────

@pytest.mark.parametrize("interval", ["5m", "1H", "4H"])
def test_non_daily_intervals_return_nothing(snapshot, interval):
    """快照只有日线。按日线数据做 5 分钟线年化差 78 倍
    （calc_bars_per_year('5m','tdxtap') = 19656），且会真的落到 Sharpe 上。
    tdxtap 不在任何 FALLBACK_CHAINS 里，拒绝不会让 auto 的探测变脆。"""
    assert DataLoader().fetch(["000001.SZ"], "2024-01-02", "2024-01-15",
                              interval=interval) == {}


@pytest.mark.parametrize("interval", ["1D", "1d"])
def test_daily_intervals_are_served(snapshot, interval):
    out = DataLoader().fetch(["000001.SZ"], "2024-01-02", "2024-01-15", interval=interval)
    assert sorted(out) == ["000001.SZ"]


# ── 同名 symbol 跨市场 ───────────────────────────────────────────────────

def test_a_symbol_present_in_two_markets_is_not_silently_overwritten(snapshot, caplog):
    """清单键是 ``market/symbol``；只取 symbol 会让后来者静默覆盖前者，
    于是一只港股可能被当成同名 A 股喂进回测。歧义必须可见。"""
    import json as _json
    import shutil

    manifest = _json.loads((snapshot / "manifest.json").read_text(encoding="utf-8"))
    entry = dict(manifest["entries"]["a_share/000001.SZ"])
    entry["path"] = "us_equity/000001.SZ.parquet"
    entry["adjustment"] = "raw"
    shutil.copy(snapshot / "a_share" / "000001.SZ.parquet",
                snapshot / "us_equity" / "000001.SZ.parquet")
    manifest["entries"]["us_equity/000001.SZ"] = entry
    (snapshot / "manifest.json").write_text(_json.dumps(manifest), encoding="utf-8")

    with caplog.at_level("WARNING"):
        out = DataLoader().fetch(["000001.SZ"], "2024-01-02", "2024-01-15")

    assert out == {}
    assert "000001.SZ" in caplog.text
