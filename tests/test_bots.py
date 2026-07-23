"""Tests for the bots/ stack: journal, RL agent, paper broker, trading desk."""

import json
import os

import numpy as np
import pandas as pd
import pytest

from bots.brokers import PaperBroker, get_broker
from bots.copytrader import manual
from bots.journal import TradeJournal
from bots.learning import QTraderAgent
from bots.organization import DeskConfig, DeskReport, TradingDesk, zone_touch_count
from bots.risk import DrawdownGuard


def make_desk(tmp_path, broker, journal, price_df, config=None, agent=None, htf_history_fn=None):
    return TradingDesk(
        broker=broker,
        journal=journal,
        agent=agent or QTraderAgent(model_path=str(tmp_path / "q.json")),
        config=config or DeskConfig(news_blackout=False, min_copy_score=0),
        history_fn=lambda _s: price_df,
        guard=DrawdownGuard(state_path=str(tmp_path / "day_state.json")),
        manual_signals_path=str(tmp_path / "manual_signals.json"),
        htf_history_fn=htf_history_fn,
    )


@pytest.fixture
def price_df():
    rng = np.random.default_rng(42)
    prices = 100 * np.exp(np.cumsum(rng.normal(0.0004, 0.01, 300)))
    return pd.DataFrame({"close": prices})


@pytest.fixture
def journal(tmp_path):
    return TradeJournal(path=str(tmp_path / "journal.json"))


def test_close_trade_logs_losses_to_mistakes_file(journal, tmp_path, monkeypatch):
    monkeypatch.setenv("BOT_DATA_DIR", str(tmp_path))
    log_path = tmp_path / "mistakes_log.md"

    winner = journal.open_trade("WIN", "long", 10, 100.0, setup="good-setup")
    journal.close_trade(winner.trade_id, 110.0)
    assert not log_path.exists()  # a winner writes nothing

    loser = journal.open_trade("LOSS", "long", 10, 100.0, setup="bad-setup")
    journal.close_trade(loser.trade_id, 95.0)
    assert log_path.exists()
    text = log_path.read_text()
    assert "LOSS" in text and "bad-setup" in text and "-5.00" in text

    # admin-tagged records (canceled orders, migrations) aren't real
    # trading mistakes and shouldn't clutter the log
    admin_loss = journal.open_trade("ADMIN", "long", 10, 100.0, setup="x")
    admin_loss.tags.append("admin")
    journal.close_trade(admin_loss.trade_id, 90.0)
    assert "ADMIN" not in log_path.read_text()


def test_trading_day_rolls_at_5pm_new_york():
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from bots.journal import trading_day

    ny = ZoneInfo("America/New_York")
    # 4:59pm ET Wednesday July 15 -> still trading day July 15
    assert trading_day(datetime(2026, 7, 15, 16, 59, tzinfo=ny)) == "2026-07-15"
    # 5:00pm ET Wednesday -> the NEXT trading day (July 16), forex convention
    assert trading_day(datetime(2026, 7, 15, 17, 0, tzinfo=ny)) == "2026-07-16"
    # 2am UTC July 16 == 10pm ET July 15 -> trading day July 16, so the
    # overnight Asian session and the following London/NY sessions share
    # one budget under ONE day label (this is the session-22 bug: with a
    # UTC-midnight roll they were split, and Asian-session trades burned
    # the whole cap before London even opened)
    from datetime import timezone as tz
    assert trading_day(datetime(2026, 7, 16, 2, 0, tzinfo=tz.utc)) == "2026-07-16"
    # naive timestamps are treated as UTC, not local time
    assert trading_day(datetime(2026, 7, 16, 2, 0)) == "2026-07-16"


def test_journal_records_and_learns(journal):
    # Five losing trades on one setup should get it blocked.
    for _ in range(5):
        record = journal.open_trade("TEST", "long", 10, 100.0, setup="bad-setup")
        journal.close_trade(record.trade_id, 95.0)
    winner = journal.open_trade("TEST", "long", 10, 100.0, setup="good-setup")
    journal.close_trade(winner.trade_id, 110.0)

    stats = journal.setup_stats()
    assert stats["bad-setup"]["trades"] == 5
    assert stats["bad-setup"]["avg_pnl"] < 0
    assert journal.should_avoid("bad-setup")
    assert not journal.should_avoid("good-setup")


def test_risk_of_ruin_matches_the_reference_table():
    from bots.journal import risk_of_ruin

    # Classic even-money gambler's-ruin table: 55% win rate (10% edge),
    # risking 10%/5%/2% per trade -> ~13% / ~1.7% / ~0.02% ruin probability.
    assert risk_of_ruin(0.55, 0.10) == pytest.approx(0.137, abs=0.01)
    assert risk_of_ruin(0.55, 0.05) == pytest.approx(0.017, abs=0.005)
    assert risk_of_ruin(0.55, 0.02) == pytest.approx(0.0002, abs=0.001)
    # No edge (50% win rate, 1:1 payoff) -> ruin is certain
    assert risk_of_ruin(0.50, 0.02) == 1.0
    assert risk_of_ruin(0.40, 0.02) == 1.0
    # Zero risk per trade -> never bets, never ruined
    assert risk_of_ruin(0.55, 0.0) == 0.0


def test_performance_metrics_includes_win_rate_and_risk_of_ruin(journal):
    for i in range(6):
        record = journal.open_trade("TEST", "long", 10, 100.0, setup=f"s{i}")
        journal.close_trade(record.trade_id, 110.0 if i < 4 else 90.0)  # 4 wins, 2 losses

    metrics = journal.performance_metrics()
    assert metrics["win_rate"] == pytest.approx(4 / 6)
    assert metrics["trades"] == 6


def test_journal_persists(tmp_path):
    path = str(tmp_path / "journal.json")
    journal = TradeJournal(path=path)
    record = journal.open_trade("AAPL", "long", 1, 150.0, setup="s")
    journal.close_trade(record.trade_id, 160.0)

    reloaded = TradeJournal(path=path)
    assert len(reloaded.trades) == 1
    assert list(reloaded.trades.values())[0].pnl == pytest.approx(10.0)


def test_agent_trains_and_signals(price_df, tmp_path):
    agent = QTraderAgent(model_path=str(tmp_path / "q.json"))
    stats = agent.train(price_df, episodes=10)
    assert agent.trained_episodes == 10
    assert stats["trades"] >= 0
    assert agent.signal(price_df) in ("buy", "sell", "hold")

    agent.save()
    fresh = QTraderAgent(model_path=str(tmp_path / "q.json"))
    assert fresh.load()
    assert fresh.q == agent.q


def test_agent_train_reports_real_total_practice_trades(price_df, tmp_path):
    # Session 48: train() must report the REAL total closed trades summed
    # across every exploring pass, not just the final eval pass -- so
    # practice scripts can print an honest, measured total instead of
    # guessing from episode count alone.
    agent = QTraderAgent(model_path=str(tmp_path / "q.json"))
    stats = agent.train(price_df, episodes=15)
    assert "training_trades" in stats
    assert stats["training_trades"] >= stats["trades"]

    # more episodes over the same data -> at least as much total practice
    more = QTraderAgent(model_path=str(tmp_path / "q2.json"))
    more_stats = more.train(price_df, episodes=45)
    assert more_stats["training_trades"] >= stats["training_trades"]


def test_agent_accepts_yfinance_columns(price_df, tmp_path):
    agent = QTraderAgent(model_path=str(tmp_path / "q.json"))
    upper = price_df.rename(columns={"close": "Close"})
    assert agent.signal(upper) in ("buy", "sell", "hold")


def test_paper_broker_round_trip(tmp_path):
    broker = PaperBroker(
        starting_cash=1_000,
        state_path=str(tmp_path / "acct.json"),
        price_overrides={"XYZ": 10.0},
    )
    result = broker.buy("XYZ", 50)
    assert result.ok and result.fill_price == 10.0
    assert broker.cash() == pytest.approx(500.0)
    assert broker.positions() == {"XYZ": 50}

    # can't oversell or overspend
    assert not broker.sell("XYZ", 100).ok
    assert not broker.buy("XYZ", 1_000).ok

    result = broker.sell("XYZ", 50)
    assert result.ok
    assert broker.cash() == pytest.approx(1_000.0)
    assert broker.positions() == {}


def test_get_broker_factory(tmp_path):
    broker = get_broker("paper", state_path=str(tmp_path / "acct.json"))
    assert broker.name == "paper" and broker.is_paper
    with pytest.raises(ValueError):
        get_broker("etrade")


def test_desk_cycle_buys_and_respects_lessons(price_df, tmp_path, journal):
    broker = PaperBroker(
        starting_cash=10_000,
        state_path=str(tmp_path / "acct.json"),
        price_overrides={"DEMO": float(price_df["close"].iloc[-1])},
    )
    agent = QTraderAgent(model_path=str(tmp_path / "q.json"))
    agent.train(price_df, episodes=5)
    desk = make_desk(tmp_path, broker, journal, price_df, agent=agent)
    report = desk.run_once(symbols=["DEMO"])
    actions = {a.symbol: a for a in report.actions}
    assert "DEMO" in actions
    assert actions["DEMO"].action in ("buy", "skip")
    if actions["DEMO"].action == "buy":
        assert broker.positions().get("DEMO", 0) > 0
        assert journal.open_position_for("DEMO") is not None
        # risk sizing: no more than 15% of equity in one name
        cost = actions["DEMO"].quantity * broker.price("DEMO")
        assert cost <= 10_000 * 0.15 * 1.01


def test_desk_stop_loss_closes_position(price_df, tmp_path, journal):
    entry_price = 100.0
    crashed = 90.0  # -10% -> beyond the 5% stop
    broker = PaperBroker(
        starting_cash=10_000,
        state_path=str(tmp_path / "acct.json"),
        price_overrides={"DEMO": entry_price, "UNRELATED": 50.0},
    )
    assert broker.buy("DEMO", 10).ok
    journal.open_trade("DEMO", "long", 10, entry_price, setup="test")
    broker.price_overrides["DEMO"] = crashed

    desk = make_desk(
        tmp_path, broker, journal, price_df,
        # risk budget 0 -> no new entries, keeps the test offline
        config=DeskConfig(news_blackout=False, min_copy_score=99, risk_per_trade_pct=0.0),
    )
    report = desk.run_once(symbols=["UNRELATED"])
    sells = [a for a in report.actions if a.action == "sell"]
    assert sells and sells[0].ok
    assert broker.positions() == {}
    closed = [t for t in journal.trades.values() if not t.is_open]
    assert closed and closed[0].pnl == pytest.approx((crashed - entry_price) * 10)


def test_circuit_breaker_blocks_new_entries(price_df, tmp_path, journal):
    guard = DrawdownGuard(max_daily_loss_pct=0.05, state_path=str(tmp_path / "day.json"))
    assert guard.check(10_000)[0] is False  # records the day-start baseline
    halted, msg = guard.check(9_400)  # down 6% -> breaker trips
    assert halted and "CIRCUIT BREAKER" in msg

    broker = PaperBroker(
        starting_cash=9_400,
        state_path=str(tmp_path / "acct.json"),
        price_overrides={"DEMO": 100.0},
    )
    desk = TradingDesk(
        broker=broker,
        journal=journal,
        agent=QTraderAgent(model_path=str(tmp_path / "q.json")),
        config=DeskConfig(news_blackout=False, min_copy_score=0),
        history_fn=lambda _s: price_df,
        guard=guard,
        manual_signals_path=str(tmp_path / "manual.json"),
    )
    report = desk.run_once(symbols=["DEMO"])
    assert not [a for a in report.actions if a.action == "buy"]
    assert any("CIRCUIT BREAKER" in n for n in report.notes)


def test_mirror_signal_executes_under_risk_rules_and_consumes(price_df, tmp_path, journal):
    sig_path = str(tmp_path / "manual_signals.json")
    manual.add_signal("DEMO", side="buy", source="mambafx", path=sig_path)

    last = float(price_df["close"].iloc[-1])
    broker = PaperBroker(
        starting_cash=10_000,
        state_path=str(tmp_path / "acct.json"),
        price_overrides={"DEMO": last},
    )
    desk = TradingDesk(
        broker=broker,
        journal=journal,
        agent=QTraderAgent(model_path=str(tmp_path / "q.json")),
        config=DeskConfig(news_blackout=False, min_copy_score=99),
        history_fn=lambda _s: price_df,
        guard=DrawdownGuard(state_path=str(tmp_path / "day.json")),
        manual_signals_path=sig_path,
    )
    report = desk.run_once(symbols=["DEMO"])
    buys = [a for a in report.actions if a.action == "buy" and a.ok]
    assert buys, report.describe()
    record = journal.open_position_for("DEMO")
    assert record is not None and record.setup == "mirror:mambafx"
    assert manual.pending_signals(sig_path) == []  # consumed after execution


def test_risk_per_trade_sizing(price_df, tmp_path, journal):
    broker = PaperBroker(
        starting_cash=10_000,
        state_path=str(tmp_path / "acct.json"),
        price_overrides={"DEMO": 100.0},
    )
    desk = make_desk(
        tmp_path, broker, journal, price_df,
        # risking 0.5% with a 5% stop -> position value = 10000*0.005/0.05 = 1000
        config=DeskConfig(news_blackout=False, min_copy_score=0, risk_per_trade_pct=0.005, stop_loss_pct=0.05),
    )
    report = desk.run_once(symbols=["DEMO"])
    buys = [a for a in report.actions if a.action == "buy" and a.ok]
    assert buys, report.describe()
    assert buys[0].quantity * 100.0 == pytest.approx(1_000.0, rel=0.01)


def test_paper_broker_margin_buying_power(tmp_path):
    # cash account (leverage 1): unchanged, can't spend beyond cash
    cash_acct = PaperBroker(
        starting_cash=1_000, state_path=str(tmp_path / "cash.json"),
        price_overrides={"XYZ": 10.0},
    )
    assert not cash_acct.buy("XYZ", 150).ok

    # margin account (5x): the same $1,500 order fills, cash goes negative,
    # equity is unchanged by the fill itself
    margin = PaperBroker(
        starting_cash=1_000, state_path=str(tmp_path / "margin.json"),
        price_overrides={"XYZ": 10.0}, leverage=5.0,
    )
    assert margin.buy("XYZ", 150).ok
    assert margin.cash() == pytest.approx(-500.0)
    assert margin.equity() == pytest.approx(1_000.0)
    # total notional still capped at leverage * equity: this order would
    # take exposure to $5,500 on $1,000 equity
    assert not margin.buy("XYZ", 400).ok
    assert margin.sell("XYZ", 150).ok
    assert margin.cash() == pytest.approx(1_000.0)


def test_leverage_lets_configured_risk_actually_apply(price_df, tmp_path, journal):
    # Session 47 bug: with a tight scalp stop, a no-leverage account
    # physically cannot risk risk_per_trade_pct -- notional was capped at
    # max_position_pct (15%) of equity and settled cash, so a "1.5% risk"
    # trade with a 0.5% stop risked ~$4 on $5k instead of $75.
    broker = PaperBroker(
        starting_cash=5_000, state_path=str(tmp_path / "acct.json"),
        price_overrides={"DEMO": 100.0}, leverage=5.0,
    )
    desk = make_desk(
        tmp_path, broker, journal, price_df,
        config=DeskConfig(
            news_blackout=False, min_copy_score=0,
            risk_per_trade_pct=0.015, stop_loss_pct=0.005,
            max_leverage=5.0, max_position_pct=3.0,
        ),
    )
    report = desk.run_once(symbols=["DEMO"])
    buys = [a for a in report.actions if a.action == "buy" and a.ok]
    assert buys, report.describe()
    notional = buys[0].quantity * 100.0
    # notional = equity * 1.5% / 0.5% stop = 3x equity...
    assert notional == pytest.approx(15_000.0, rel=0.01)
    # ...so the dollar risk at the stop is the CONFIGURED 1.5% of equity
    assert notional * 0.005 == pytest.approx(5_000 * 0.015, rel=0.01)


def test_leverage_exposure_cap_counts_open_positions(price_df, tmp_path, journal):
    broker = PaperBroker(
        starting_cash=5_000, state_path=str(tmp_path / "acct.json"),
        price_overrides={"DEMO": 100.0, "HELD": 50.0}, leverage=5.0,
    )
    # existing position eats most of the 5x exposure cap
    assert broker.buy("HELD", 400).ok  # $20,000 notional on $5,000 equity
    desk = make_desk(
        tmp_path, broker, journal, price_df,
        config=DeskConfig(
            news_blackout=False, min_copy_score=0,
            risk_per_trade_pct=0.015, stop_loss_pct=0.005,
            max_leverage=5.0, max_position_pct=3.0,
        ),
    )
    report = desk.run_once(symbols=["DEMO"])
    buys = [a for a in report.actions if a.action == "buy" and a.ok]
    assert buys, report.describe()
    # only $5,000 of headroom left (5x * $5,000 equity - $20,000 held)
    assert buys[0].quantity * 100.0 == pytest.approx(5_000.0, rel=0.01)


def test_journal_records_mfe_and_mae(price_df, tmp_path, journal):
    # Session 47: every managed cycle records the trade's max favorable /
    # adverse excursion into its tags, so exit-rule tuning (is the 2R
    # target reachable?) can be answered from evidence after the fact.
    broker = PaperBroker(
        starting_cash=10_000, state_path=str(tmp_path / "acct.json"),
        price_overrides={"DEMO": 100.0, "UNRELATED": 50.0},
    )
    assert broker.buy("DEMO", 10).ok
    journal.open_trade("DEMO", "long", 10, 100.0, setup="test")
    desk = make_desk(
        tmp_path, broker, journal, price_df,
        config=DeskConfig(news_blackout=False, min_copy_score=99,
                          risk_per_trade_pct=0.0),
    )
    broker.price_overrides["DEMO"] = 102.0  # +2% peak
    desk.run_once(symbols=["UNRELATED"])
    broker.price_overrides["DEMO"] = 94.0  # -6% -> beyond the 5% stop
    desk.run_once(symbols=["UNRELATED"])
    closed = [t for t in journal.trades.values() if not t.is_open]
    assert closed
    tags = {t.split(":")[0]: float(t.split(":", 1)[1])
            for t in closed[0].tags if ":" in t}
    assert tags["mfe"] == pytest.approx(0.02, abs=1e-6)
    assert tags["mae"] == pytest.approx(-0.06, abs=1e-6)


def test_autopilot_writes_last_cycle_feed(price_df, tmp_path, journal, monkeypatch):
    # Session 47 command center: every cycle publishes its per-symbol
    # decisions (with real reason strings) to BOT_DATA_DIR/last_cycle.json
    # for the dashboard's decision feed.
    from bots.autopilot import write_last_cycle

    monkeypatch.setenv("BOT_DATA_DIR", str(tmp_path))
    broker = PaperBroker(
        starting_cash=10_000, state_path=str(tmp_path / "acct.json"),
        price_overrides={"DEMO": 100.0},
    )
    desk = make_desk(
        tmp_path, broker, journal, price_df,
        config=DeskConfig(news_blackout=False, min_copy_score=0,
                          risk_per_trade_pct=0.005, stop_loss_pct=0.05),
    )
    report = desk.run_once(symbols=["DEMO"])
    write_last_cycle(desk, report, "test-stamp")
    with open(tmp_path / "last_cycle.json") as fh:
        payload = json.load(fh)
    assert payload["stamp"] == "test-stamp"
    assert payload["equity"] == pytest.approx(10_000, rel=0.01)
    assert payload["actions"] and payload["actions"][0]["symbol"] == "DEMO"
    assert "reason" in payload["actions"][0]


def test_funded_config_carries_leverage_for_tight_stops():
    from bots.organization import funded_account_config

    cfg = funded_account_config()
    assert cfg.max_leverage == 5.0
    assert cfg.max_position_pct == 3.0
    # sanity: at a typical 0.5% ATR stop, the per-position notional cap
    # (3x equity) is exactly what risking 1.5% of equity requires
    assert cfg.risk_per_trade_pct / 0.005 == pytest.approx(cfg.max_position_pct)


def test_desk_skips_entry_with_pending_order(price_df, tmp_path, journal):
    class FakePendingBroker(PaperBroker):
        def has_pending_order(self, symbol):
            return symbol.upper() == "DEMO"

    broker = FakePendingBroker(
        starting_cash=10_000,
        state_path=str(tmp_path / "acct.json"),
        price_overrides={"DEMO": 100.0},
    )
    desk = make_desk(tmp_path, broker, journal, price_df, config=DeskConfig(news_blackout=False, min_copy_score=0))
    report = desk.run_once(symbols=["DEMO"])
    assert not [a for a in report.actions if a.action == "buy"]
    assert any("pending fill" in a.reason for a in report.actions if a.symbol == "DEMO")
    assert broker.positions() == {}


def test_max_positions_counts_pending_journal_entries(price_df, tmp_path, journal):
    # Simulate a broker that queues orders instead of filling them (like a
    # stock order placed outside market hours): buy() succeeds and the
    # journal opens a trade, but broker.positions() stays empty.
    class QueuedFillBroker(PaperBroker):
        def positions(self):
            return {}

    broker = QueuedFillBroker(
        starting_cash=100_000,
        state_path=str(tmp_path / "acct.json"),
        price_overrides={f"SYM{i}": 100.0 for i in range(6)},
    )
    desk = make_desk(
        tmp_path, broker, journal, price_df,
        config=DeskConfig(news_blackout=False, min_copy_score=0, max_positions=5),
    )
    report = desk.run_once(symbols=[f"SYM{i}" for i in range(6)])
    buys = [a for a in report.actions if a.action == "buy" and a.ok]
    assert len(buys) == 5, report.describe()
    assert any(
        a.action == "skip" and "max positions" in a.reason for a in report.actions
    )
    open_symbols = {t.symbol for t in journal.trades.values() if t.is_open}
    assert len(open_symbols) == 5


def test_intraday_state_features(tmp_path):
    from bots.learning.agent import extract_state

    rng = np.random.default_rng(3)
    frames = []
    for day in ("2026-07-08", "2026-07-09"):
        idx = pd.date_range(f"{day} 09:30", periods=78, freq="5min")
        px = 100 * np.exp(np.cumsum(rng.normal(0, 0.001, 78)))
        frames.append(
            pd.DataFrame(
                {"open": px, "high": px * 1.001, "low": px * 0.999, "close": px,
                 "volume": rng.integers(1000, 5000, 78)},
                index=idx,
            )
        )
    intraday = pd.concat(frames)
    state = extract_state(intraday, 100, holding=False)
    for part in ("trend-", "rsi-", "vwap-", "orb-", "tod-", "pos-out"):
        assert part in state, state
    # first bar of a session is inside the opening range and the open hour
    state_open = extract_state(intraday, 78, holding=False)
    assert "orb-in" in state_open and "tod-open" in state_open

    daily = pd.DataFrame({"close": 100 + np.arange(60.0)})
    daily_state = extract_state(daily, 50, holding=True)
    assert "vwap-" not in daily_state  # daily states unchanged for old Q-tables
    assert daily_state.endswith("pos-in")


def test_daily_trade_cap(price_df, tmp_path, journal):
    for i in range(3):
        journal.open_trade(f"OLD{i}", "long", 1, 10.0, setup="s")  # opened today
    broker = PaperBroker(
        starting_cash=100_000,
        state_path=str(tmp_path / "acct.json"),
        price_overrides={"NEW1": 10.0, "NEW2": 10.0},
    )
    desk = make_desk(
        tmp_path, broker, journal, price_df,
        config=DeskConfig(news_blackout=False, min_copy_score=0, max_positions=99, max_trades_per_day=4),
    )
    report = desk.run_once(symbols=["NEW1", "NEW2"])
    buys = [a for a in report.actions if a.action == "buy" and a.ok]
    capped = [a for a in report.actions if "daily trade cap" in a.reason]
    assert len(buys) == 1 and len(capped) == 1, report.describe()


def test_flatten_all_closes_every_position(price_df, tmp_path, journal):
    broker = PaperBroker(
        starting_cash=10_000,
        state_path=str(tmp_path / "acct.json"),
        price_overrides={"AAA": 50.0, "BBB": 20.0},
    )
    assert broker.buy("AAA", 10).ok
    assert broker.buy("BBB", 25).ok
    journal.open_trade("AAA", "long", 10, 50.0, setup="daytrade")
    journal.open_trade("BBB", "long", 25, 20.0, setup="daytrade")

    desk = make_desk(tmp_path, broker, journal, price_df)
    report = desk.flatten_all(reason="end of day")
    sells = [a for a in report.actions if a.action == "sell" and a.ok]
    assert {a.symbol for a in sells} == {"AAA", "BBB"}
    assert all("end of day" in a.reason for a in sells)
    assert broker.positions() == {}
    assert all(not t.is_open for t in journal.trades.values())


def test_minutes_to_stock_close():
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from bots.autopilot import minutes_to_stock_close

    ny = ZoneInfo("America/New_York")
    assert minutes_to_stock_close(datetime(2026, 7, 14, 15, 50, tzinfo=ny)) == 10
    assert minutes_to_stock_close(datetime(2026, 7, 14, 12, 0, tzinfo=ny)) == 240
    assert minutes_to_stock_close(datetime(2026, 7, 14, 20, 0, tzinfo=ny)) is None
    assert minutes_to_stock_close(datetime(2026, 7, 18, 12, 0, tzinfo=ny)) is None  # Saturday


def test_autopilot_flattens_before_close(price_df, tmp_path, monkeypatch):
    import bots.autopilot as ap
    from bots.organization import DeskConfig, TradingDesk
    from bots.risk import DrawdownGuard

    broker = PaperBroker(
        starting_cash=10_000,
        state_path=str(tmp_path / "acct.json"),
        price_overrides={"AAA": 50.0},
    )
    assert broker.buy("AAA", 10).ok
    journal = TradeJournal(path=str(tmp_path / "journal.json"))
    journal.open_trade("AAA", "long", 10, 50.0, setup="daytrade")

    desk = TradingDesk(
        broker=broker,
        journal=journal,
        agent=QTraderAgent(model_path=str(tmp_path / "q.json")),
        config=DeskConfig(news_blackout=False, min_copy_score=0, day_trading=True),
        history_fn=lambda _s: price_df,
        guard=DrawdownGuard(state_path=str(tmp_path / "day.json")),
        manual_signals_path=str(tmp_path / "manual.json"),
    )

    calls = []
    monkeypatch.setattr(desk, "flatten_all", lambda **kw: calls.append("flatten") or DeskReport())
    monkeypatch.setattr(desk, "run_once", lambda **kw: calls.append("run_once") or DeskReport())
    monkeypatch.setattr(ap, "market_is_open", lambda _m, now=None: True)
    # 10 minutes to close, under the default 15-minute flatten window
    monkeypatch.setattr(ap, "minutes_to_stock_close", lambda now=None: 10)
    monkeypatch.setattr(ap.time, "sleep", lambda _s: None)

    ap.run_autopilot(broker_name="paper", interval_minutes=1, desk=desk, max_cycles=1)
    assert calls == ["flatten"]


def test_buy_bracket_fallback_and_desk_uses_it(price_df, tmp_path, journal):
    calls = {}

    class BracketBroker(PaperBroker):
        def buy_bracket(self, symbol, quantity, stop_loss_pct, take_profit_pct):
            calls["args"] = (symbol, stop_loss_pct, take_profit_pct)
            return super().buy(symbol, quantity)

    broker = BracketBroker(
        starting_cash=10_000,
        state_path=str(tmp_path / "acct.json"),
        price_overrides={"DEMO": 100.0},
    )
    desk = make_desk(
        tmp_path, broker, journal, price_df,
        config=DeskConfig(news_blackout=False, min_copy_score=0, stop_loss_pct=0.015, take_profit_pct=0.03),
    )
    report = desk.run_once(symbols=["DEMO"])
    assert [a for a in report.actions if a.action == "buy" and a.ok], report.describe()
    assert calls["args"] == ("DEMO", 0.015, 0.03)

    # base-class fallback: plain broker still works through buy_bracket
    plain = PaperBroker(
        starting_cash=1_000, state_path=str(tmp_path / "b.json"),
        price_overrides={"XYZ": 10.0},
    )
    assert plain.buy_bracket("XYZ", 5, 0.015, 0.03).ok


def test_reconcile_closes_orphaned_journal_entries(price_df, tmp_path, journal):
    # journal thinks we hold GONE, but the broker has no such position and no
    # pending order -> a bracket stop/target must have filled between cycles
    journal.open_trade("GONE", "long", 10, 100.0, setup="daytrade")
    broker = PaperBroker(
        starting_cash=10_000,
        state_path=str(tmp_path / "acct.json"),
        price_overrides={"GONE": 103.0},
    )
    desk = make_desk(
        tmp_path, broker, journal, price_df,
        config=DeskConfig(news_blackout=False, min_copy_score=99, max_positions=0),
    )
    report = desk.run_once(symbols=["GONE"])
    reconciled = [a for a in report.actions if "reconciled" in a.reason]
    assert reconciled, report.describe()
    record = [t for t in journal.trades.values() if t.symbol == "GONE"][0]
    assert not record.is_open and record.pnl == pytest.approx(30.0)


def test_performance_metrics(journal):
    # 1 winner (+100), 2 losers (-30, -20) -> profit factor 100/50=2.0,
    # expectancy (100-30-20)/3=+16.67, drawdown = worst peak-to-trough dip
    for pnl_pairs in [(100.0, 200.0), (-30.0, 170.0), (-20.0, 150.0)]:
        entry, exit_ = 100.0, 100.0 + pnl_pairs[0]
        t = journal.open_trade("X", "long", 1, entry, setup="s")
        journal.close_trade(t.trade_id, exit_)
    m = journal.performance_metrics()
    assert m["profit_factor"] == pytest.approx(2.0)
    assert m["expectancy"] == pytest.approx(50.0 / 3)
    assert m["max_drawdown"] == pytest.approx(50.0)  # peak 100 -> trough 50


def test_performance_metrics_all_wins_is_infinite_profit_factor(journal):
    t = journal.open_trade("X", "long", 1, 100.0, setup="s")
    journal.close_trade(t.trade_id, 110.0)
    assert journal.performance_metrics()["profit_factor"] == float("inf")


def test_extract_calls_buy_and_sell():
    from bots.social.signals import extract_calls

    calls = extract_calls("Just went LONG on $AAPL here, also might SELL TSLA soon")
    by_symbol = {c.symbol: c.side for c in calls}
    assert by_symbol.get("AAPL") == "buy"
    assert by_symbol.get("TSLA") == "sell"


def test_extract_calls_forex_and_stopwords():
    from bots.social.signals import extract_calls

    calls = extract_calls("BUY EURUSD now, I think it goes up. Also SHORT GBPJPY")
    symbols = {c.symbol for c in calls}
    assert "EURUSD" in symbols
    assert "GBPJPY" in symbols
    assert "I" not in symbols  # stopword filtered, not a real ticker


def test_extract_calls_no_false_positive_on_plain_text():
    from bots.social.signals import extract_calls

    assert extract_calls("Market was choppy today, nothing exciting happened.") == []


def test_out_of_sample_backtest(price_df):
    from bots.learning.backtest import run_backtest

    result = run_backtest(price_df, train_fraction=0.7, episodes=5)
    assert result["train_bars"] + result["test_bars"] == len(price_df)
    assert result["test_bars"] < result["train_bars"]
    assert "edge_vs_buy_hold_pct" in result
    assert result["edge_vs_buy_hold_pct"] == pytest.approx(
        result["agent_return_pct"] - result["buy_hold_return_pct"]
    )


def test_market_hours_clock():
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from bots.autopilot import market_is_open

    ny = ZoneInfo("America/New_York")
    tuesday_noon = datetime(2026, 7, 14, 12, 0, tzinfo=ny)
    tuesday_night = datetime(2026, 7, 14, 20, 0, tzinfo=ny)
    saturday = datetime(2026, 7, 18, 12, 0, tzinfo=ny)
    sunday_evening = datetime(2026, 7, 19, 18, 0, tzinfo=ny)

    assert market_is_open("stocks", tuesday_noon)
    assert not market_is_open("stocks", tuesday_night)
    assert not market_is_open("stocks", saturday)
    assert market_is_open("forex", tuesday_night)
    assert not market_is_open("forex", saturday)
    assert market_is_open("forex", sunday_evening)
    assert market_is_open("crypto", saturday)


def test_autopilot_runs_cycles_offline(price_df, tmp_path, monkeypatch, capsys):
    import bots.autopilot as ap

    monkeypatch.setattr(ap.time, "sleep", lambda _s: None)
    monkeypatch.setattr(ap, "market_is_open", lambda _m, now=None: True)

    broker = PaperBroker(
        starting_cash=10_000,
        state_path=str(tmp_path / "acct.json"),
        price_overrides={"DEMO": float(price_df["close"].iloc[-1])},
    )
    journal = TradeJournal(path=str(tmp_path / "journal.json"))
    desk = make_desk(tmp_path, broker, journal, price_df)

    ap.run_autopilot(
        broker_name="paper", interval_minutes=1, symbols=["DEMO"],
        max_cycles=2, desk=desk,
    )
    out = capsys.readouterr().out
    assert "Autopilot started" in out
    assert "cycle 2" in out


def test_autopilot_weekend_crypto_fallback(price_df, tmp_path, monkeypatch):
    import bots.autopilot as ap

    monkeypatch.setattr(ap.time, "sleep", lambda _s: None)
    # forex closed, crypto open -- the Saturday scenario
    monkeypatch.setattr(
        ap, "market_is_open", lambda m, now=None: m == "crypto"
    )

    broker = PaperBroker(
        starting_cash=10_000,
        state_path=str(tmp_path / "acct.json"),
        price_overrides={"EURUSD": 1.1, "BTC-USD": 60_000.0},
    )
    journal = TradeJournal(path=str(tmp_path / "journal.json"))
    desk = make_desk(tmp_path, broker, journal, price_df)

    seen_symbols = []
    monkeypatch.setattr(
        desk, "run_once",
        lambda symbols=None: seen_symbols.append(symbols) or DeskReport(),
    )

    ap.run_autopilot(
        broker_name="paper", interval_minutes=1, symbols=["EURUSD"],
        market_override="forex", weekend_symbols=["BTC-USD"],
        max_cycles=1, desk=desk,
    )
    assert seen_symbols == [["BTC-USD"]]


def test_select_active_market_respects_weekend_trading_allowed(monkeypatch):
    """A firm that bans ALL weekend trading (clarity_one_step_challenge_config)
    must not get the crypto weekend-fallback even if --weekend-symbols was
    passed -- e.g. by a copy-pasted launch command from another account.
    weekend_trading_allowed=False is what makes this code-enforced instead
    of relying on remembering a CLI flag. Tests the pure selection function
    directly (see its docstring for why NOT going through run_autopilot's
    loop here) rather than running the full autopilot loop."""
    import bots.autopilot as ap

    monkeypatch.setattr(ap, "market_is_open", lambda m, now=None: m == "crypto")

    # allowed (default): forex closed, crypto open -> falls back to crypto
    market, syms, stock_active = ap.select_active_market(
        "forex", ["EURUSD"], ["BTC-USD"], None, True, None
    )
    assert (market, syms) == ("crypto", ["BTC-USD"])

    # banned: same conditions, but weekend_trading_allowed=False -> stays on
    # forex (which the mock reports closed) instead of touching crypto
    market, syms, stock_active = ap.select_active_market(
        "forex", ["EURUSD"], ["BTC-USD"], None, False, None
    )
    assert (market, syms) == ("forex", ["EURUSD"])


def test_clarity_one_step_challenge_config_bans_weekend_trading():
    from bots.organization import clarity_one_step_challenge_config

    assert clarity_one_step_challenge_config(funded=False).weekend_trading_allowed is False
    assert clarity_one_step_challenge_config(funded=True).weekend_trading_allowed is False


def test_autopilot_adds_stocks_during_nyse_hours(price_df, tmp_path, monkeypatch):
    import bots.autopilot as ap

    monkeypatch.setattr(ap.time, "sleep", lambda _s: None)
    # forex and stocks both open -- a Wednesday at noon ET scenario
    monkeypatch.setattr(ap, "market_is_open", lambda m, now=None: True)
    monkeypatch.setattr(ap, "minutes_to_stock_close", lambda now=None: 240)  # not near close

    broker = PaperBroker(
        starting_cash=10_000,
        state_path=str(tmp_path / "acct.json"),
        price_overrides={"EURUSD": 1.1, "AAPL": 200.0},
    )
    journal = TradeJournal(path=str(tmp_path / "journal.json"))
    desk = make_desk(tmp_path, broker, journal, price_df)

    seen_symbols = []
    monkeypatch.setattr(
        desk, "run_once",
        lambda symbols=None: seen_symbols.append(symbols) or DeskReport(),
    )

    ap.run_autopilot(
        broker_name="paper", interval_minutes=1, symbols=["EURUSD"],
        market_override="forex", stock_symbols=["AAPL"],
        max_cycles=1, desk=desk,
    )
    assert seen_symbols == [["EURUSD", "AAPL"]]


def test_autopilot_flattens_only_stock_leg_near_nyse_close(price_df, tmp_path, monkeypatch):
    import bots.autopilot as ap
    from bots.organization import DeskConfig, TradingDesk
    from bots.risk import DrawdownGuard

    broker = PaperBroker(
        starting_cash=10_000,
        state_path=str(tmp_path / "acct.json"),
        price_overrides={"EURUSD": 1.1, "AAPL": 200.0},
    )
    assert broker.buy("EURUSD", 100).ok
    assert broker.buy("AAPL", 5).ok
    journal = TradeJournal(path=str(tmp_path / "journal.json"))
    journal.open_trade("EURUSD", "long", 100, 1.1, setup="daytrade")
    journal.open_trade("AAPL", "long", 5, 200.0, setup="daytrade")

    desk = TradingDesk(
        broker=broker,
        journal=journal,
        agent=QTraderAgent(model_path=str(tmp_path / "q.json")),
        config=DeskConfig(news_blackout=False, min_copy_score=0, day_trading=True),
        history_fn=lambda _s: price_df,
        guard=DrawdownGuard(state_path=str(tmp_path / "day.json")),
        manual_signals_path=str(tmp_path / "manual.json"),
    )

    monkeypatch.setattr(ap.time, "sleep", lambda _s: None)
    monkeypatch.setattr(ap, "market_is_open", lambda m, now=None: True)
    monkeypatch.setattr(ap, "minutes_to_stock_close", lambda now=None: 10)  # near close

    ap.run_autopilot(
        broker_name="paper", interval_minutes=1, symbols=["EURUSD"],
        market_override="forex", stock_symbols=["AAPL"],
        max_cycles=1, desk=desk,
    )
    positions = broker.positions()
    assert "AAPL" not in positions  # flattened
    assert positions.get("EURUSD") == pytest.approx(100)  # left alone


def test_index_alias_resolution():
    from bots.marketdata import resolve_symbol

    assert resolve_symbol("US30") == "YM=F"
    assert resolve_symbol("us30") == "YM=F"
    assert resolve_symbol("NAS100") == "NQ=F"
    assert resolve_symbol("nasdaq") == "NQ=F"
    assert resolve_symbol("US500") == "ES=F"
    assert resolve_symbol("GOLD") == "GC=F"
    assert resolve_symbol("XAUUSD") == "GC=F"
    assert resolve_symbol("OIL") == "CL=F"
    assert resolve_symbol("WTI") == "CL=F"
    assert resolve_symbol("SILVER") == "SI=F"
    assert resolve_symbol("XAGUSD") == "SI=F"
    assert resolve_symbol("US2000") == "RTY=F"
    assert resolve_symbol("RUSSELL") == "RTY=F"
    # session 35 widening: the new instruments carry real spread costs too
    from bots.spreads import spread_pct

    assert spread_pct("SI=F") > spread_pct("GC=F")  # silver wider than gold
    assert spread_pct("RTY=F") > spread_pct("ES=F")  # Russell wider than S&P
    # unknown symbols pass through unchanged
    assert resolve_symbol("EURUSD") == "EURUSD"
    assert resolve_symbol("YM=F") == "YM=F"


def test_oanda_symbol_normalization():
    from bots.brokers.oanda import _instrument

    assert _instrument("EURUSD") == "EUR_USD"
    assert _instrument("eur/usd") == "EUR_USD"
    assert _instrument("EUR_USD") == "EUR_USD"
    assert _instrument("GBPJPY") == "GBP_JPY"


def test_tradelocker_lot_conversion():
    from bots.brokers.tradelocker_broker import _units_to_lots

    # forex: units -> lots at 100k/lot, floored to 2dp, 0.01 minimum
    assert _units_to_lots("EURUSD", 100_000) == 1.0
    assert _units_to_lots("EURUSD", 1_500) == pytest.approx(0.01)
    assert _units_to_lots("EURUSD", 250_000) == pytest.approx(2.5)
    assert _units_to_lots("GBPJPY", 100) == 0.01  # clamped to min lot
    # non-forex instruments pass through unchanged
    assert _units_to_lots("NAS100", 2.5) == 2.5
    assert _units_to_lots("BTCUSD.X", 0.3) == 0.3


def test_news_guard_blocks_around_high_impact_events():
    from datetime import datetime, timedelta, timezone

    from bots.newsguard import NewsGuard

    now = datetime(2026, 7, 15, 12, 30, tzinfo=timezone.utc)
    events = [
        {"title": "CPI y/y", "country": "USD", "impact": "High",
         "date": (now + timedelta(minutes=5)).isoformat()},
        {"title": "Low thing", "country": "USD", "impact": "Low",
         "date": now.isoformat()},
        {"title": "EUR news", "country": "EUR", "impact": "High",
         "date": now.isoformat()},
    ]
    guard = NewsGuard(currencies=("USD",), fetch_fn=lambda: events)
    blocked, why = guard.blackout(now=now)
    assert blocked and "CPI" in why

    # 30 minutes later: outside the window
    blocked, _ = guard.blackout(now=now + timedelta(minutes=30))
    assert not blocked

    # EUR-only guard ignores the USD event
    eur_guard = NewsGuard(currencies=("CHF",), fetch_fn=lambda: events)
    assert not eur_guard.blackout(now=now)[0]


def test_news_guard_fails_safe_on_broken_feed():
    from bots.newsguard import NewsGuard

    def broken():
        raise RuntimeError("feed down")

    guard = NewsGuard(fetch_fn=broken)
    blocked, why = guard.blackout()
    assert not blocked  # never lock the desk because a feed is down


def test_correlation_guard_caps_cluster_exposure(price_df, tmp_path, journal):
    # 3 mega-cap tech candidates, cluster cap 2 -> third is skipped even
    # though position slots remain
    broker = PaperBroker(
        starting_cash=100_000,
        state_path=str(tmp_path / "acct.json"),
        price_overrides={"AAPL": 100.0, "MSFT": 100.0, "NVDA": 100.0},
    )
    desk = make_desk(
        tmp_path, broker, journal, price_df,
        config=DeskConfig(news_blackout=False, min_copy_score=0,
                          max_positions=99, max_per_correlation_group=2),
    )
    report = desk.run_once(symbols=["AAPL", "MSFT", "NVDA"])
    buys = [a for a in report.actions if a.action == "buy" and a.ok]
    skipped = [a for a in report.actions if "correlation guard" in a.reason]
    assert len(buys) == 2 and len(skipped) == 1, report.describe()


def test_correlation_group_covers_the_desks_own_index_and_metal_names():
    """Session 46: correlation_group() does exact string matching, and the
    desk's OWN watchlist names (US30/NAS100/US500/US2000, GOLD/SILVER/OIL --
    used in every real launch command) were missing from CORRELATION_GROUPS
    entirely, silently disabling the correlation cap for 3 of 4 US indices
    and all 3 commodities on the actual funded/challenge watchlist."""
    from bots.organization import correlation_group

    assert correlation_group("US30") == "us-broad"
    assert correlation_group("US500") == "us-broad"
    assert correlation_group("US2000") == "us-broad"
    assert correlation_group("NAS100") == "us-tech"
    assert correlation_group("GOLD") == "gold"
    assert correlation_group("SILVER") == "gold"
    assert correlation_group("OIL") == "oil"
    # and GOLD/SILVER really do share a cluster with each other (not just
    # both independently mapping to "gold" by coincidence)
    assert correlation_group("GOLD") == correlation_group("SILVER")


def test_correlation_guard_caps_us_index_cfd_exposure(price_df, tmp_path, journal):
    """The exact real-world scenario the gap allowed: 4 simultaneous US
    index CFD positions (effectively one 4x-concentrated bet on US equities)
    going completely uncapped because the desk's own symbol names weren't
    in any correlation group."""
    broker = PaperBroker(
        starting_cash=100_000,
        state_path=str(tmp_path / "acct.json"),
        price_overrides={"US30": 100.0, "NAS100": 100.0, "US500": 100.0, "US2000": 100.0},
    )
    desk = make_desk(
        tmp_path, broker, journal, price_df,
        config=DeskConfig(news_blackout=False, min_copy_score=0,
                          max_positions=99, max_per_correlation_group=2),
    )
    report = desk.run_once(symbols=["US30", "US500", "US2000"])  # us-broad cluster
    buys = [a for a in report.actions if a.action == "buy" and a.ok]
    skipped = [a for a in report.actions if "correlation guard" in a.reason]
    assert len(buys) == 2 and len(skipped) == 1, report.describe()


def test_forex_session_score_overlap_is_highest():
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from bots.organization import active_forex_session, forex_session_score

    ny = ZoneInfo("America/New_York")
    overlap_time = datetime(2026, 7, 15, 9, 0, tzinfo=ny)  # 9am ET -> overlap
    asian_time = datetime(2026, 7, 15, 1, 0, tzinfo=ny)  # 1am ET -> asian

    assert active_forex_session(overlap_time) == "overlap"
    assert forex_session_score("EURUSD", overlap_time) == 2
    assert forex_session_score("EUR_USD", overlap_time) == 2  # underscore format normalized

    assert active_forex_session(asian_time) == "asian"
    assert forex_session_score("EURUSD", asian_time) == 0
    assert forex_session_score("USDJPY", asian_time) == 1
    assert forex_session_score("AAPL", overlap_time) == 0  # not an FX pair


def test_session_aware_forex_skips_off_session_pairs(price_df, tmp_path, journal, monkeypatch):
    from datetime import datetime
    from zoneinfo import ZoneInfo

    import bots.organization as org_mod

    asian_time = datetime(2026, 7, 15, 1, 0, tzinfo=ZoneInfo("America/New_York"))
    monkeypatch.setattr(org_mod, "active_forex_session", lambda now=None: "asian")

    broker = PaperBroker(
        starting_cash=100_000,
        state_path=str(tmp_path / "acct.json"),
        price_overrides={"EURUSD": 1.1, "USDJPY": 150.0},
    )
    desk = make_desk(
        tmp_path, broker, journal, price_df,
        config=DeskConfig(news_blackout=False, min_copy_score=0,
                          max_positions=99, session_aware_forex=True),
    )
    report = desk.run_once(symbols=["EURUSD", "USDJPY"])
    skipped = [a for a in report.actions if "session filter" in a.reason]
    buys = [a for a in report.actions if a.action == "buy" and a.ok]
    # EURUSD isn't active in the asian session -> skipped; USDJPY is -> traded
    assert any(a.symbol == "EURUSD" for a in skipped), report.describe()
    assert all(a.symbol != "EURUSD" for a in buys), report.describe()


def test_session_aware_forex_applies_to_non_usd_crosses_too(price_df, tmp_path, journal, monkeypatch):
    # EURGBP is a forex pair but sits in the "eur-crosses" correlation
    # group, not "usd-fx" -- the session filter must still catch it (this
    # was a real bug: it used to key off correlation_group == "usd-fx"
    # specifically, so anything outside that one group silently skipped
    # the session check entirely).
    import bots.organization as org_mod

    monkeypatch.setattr(org_mod, "active_forex_session", lambda now=None: "asian")

    broker = PaperBroker(
        starting_cash=100_000,
        state_path=str(tmp_path / "acct.json"),
        price_overrides={"EURGBP": 0.85},
    )
    desk = make_desk(
        tmp_path, broker, journal, price_df,
        config=DeskConfig(news_blackout=False, min_copy_score=0,
                          max_positions=99, session_aware_forex=True),
    )
    report = desk.run_once(symbols=["EURGBP"])
    skipped = [a for a in report.actions if "session filter" in a.reason]
    assert any(a.symbol == "EURGBP" for a in skipped), report.describe()


def test_reduce_size_after_loss_halves_next_position(price_df, tmp_path, journal):
    broker = PaperBroker(
        starting_cash=100_000, state_path=str(tmp_path / "acct.json"),
        price_overrides={"DEMO": 100.0},
    )
    # A closed loser recorded just now -> journal.last_closed_trade() is a loss
    record = journal.open_trade("OTHER", "long", 10, 100.0, setup="daytrade")
    journal.close_trade(record.trade_id, 95.0)

    # Small risk_per_trade_pct so the risk-based budget (not the
    # max_position_pct cap) is the binding constraint on quantity -- only
    # then does halving risk_pct actually show up in the order size.
    config = DeskConfig(news_blackout=False, min_copy_score=0,
                        risk_per_trade_pct=0.005, reduce_size_after_loss=True)
    desk = make_desk(tmp_path, broker, journal, price_df, config=config)
    report = desk.run_once(symbols=["DEMO"])
    buys = [a for a in report.actions if a.action == "buy" and a.ok]
    assert len(buys) == 1
    assert "anti-martingale" in buys[0].reason

    full_size_config = DeskConfig(news_blackout=False, min_copy_score=0,
                                  risk_per_trade_pct=0.005, reduce_size_after_loss=False)
    broker2 = PaperBroker(
        starting_cash=100_000, state_path=str(tmp_path / "acct2.json"),
        price_overrides={"DEMO": 100.0},
    )
    journal2 = TradeJournal(path=str(tmp_path / "journal2.json"))
    desk2 = make_desk(tmp_path, broker2, journal2, price_df, config=full_size_config)
    report2 = desk2.run_once(symbols=["DEMO"])
    full_buy = [a for a in report2.actions if a.action == "buy" and a.ok][0]
    assert buys[0].quantity == pytest.approx(full_buy.quantity / 2.0, rel=0.05)


def _backdate_entry(journal, record, minutes):
    from datetime import datetime, timedelta, timezone

    record.entry_time = (
        datetime.now(timezone.utc) - timedelta(minutes=minutes)
    ).isoformat()
    journal.save()


def test_time_stop_closes_stale_trade(price_df, tmp_path, journal):
    broker = PaperBroker(
        starting_cash=10_000, state_path=str(tmp_path / "acct.json"),
        price_overrides={"DEMO": 100.0},
    )
    assert broker.buy("DEMO", 10).ok
    record = journal.open_trade("DEMO", "long", 10, 100.0, setup="daytrade")
    _backdate_entry(journal, record, minutes=180)  # 3 hours old, flat since entry

    config = DeskConfig(news_blackout=False, min_copy_score=99,
                        risk_per_trade_pct=0.0, stop_loss_pct=0.015,
                        take_profit_pct=0.03, max_hold_minutes=120)
    desk = make_desk(tmp_path, broker, journal, price_df, config=config)
    report = desk.run_once(symbols=[])
    sells = [a for a in report.actions if a.action == "sell" and a.ok]
    assert len(sells) == 1 and "time stop" in sells[0].reason, report.describe()
    assert "DEMO" not in broker.positions()


def test_time_stop_spares_breakeven_armed_and_fresh_trades(price_df, tmp_path, journal):
    broker = PaperBroker(
        starting_cash=10_000, state_path=str(tmp_path / "acct.json"),
        price_overrides={"OLDWIN": 102.0, "FRESH": 100.0},
    )
    assert broker.buy("OLDWIN", 10).ok
    assert broker.buy("FRESH", 10).ok
    # OLDWIN: old but already reached +1R (breakeven-armed) -> exempt
    old_win = journal.open_trade("OLDWIN", "long", 10, 100.0, setup="daytrade")
    old_win.tags.append("breakeven-armed")
    _backdate_entry(journal, old_win, minutes=180)
    # FRESH: opened just now -> under the cap, exempt
    journal.open_trade("FRESH", "long", 10, 100.0, setup="daytrade")

    config = DeskConfig(news_blackout=False, min_copy_score=99,
                        risk_per_trade_pct=0.0, stop_loss_pct=0.015,
                        take_profit_pct=0.03, max_hold_minutes=120)
    desk = make_desk(tmp_path, broker, journal, price_df, config=config)
    report = desk.run_once(symbols=[])
    assert not [a for a in report.actions if "time stop" in a.reason], report.describe()
    assert set(broker.positions()) == {"OLDWIN", "FRESH"}


def _synthetic_session_df(today_bars_after_open: list, prior_days: int = 3) -> pd.DataFrame:
    """Builds a multi-day 5-minute intraday OHLC frame: `prior_days` full
    quiet sessions (so ATR/history requirements are satisfied) followed by
    today's session, where the first 6 bars (30 min) set the opening
    range and `today_bars_after_open` are appended as closes after that."""
    frames = []
    for d in range(prior_days):
        day = pd.Timestamp("2026-07-01") + pd.Timedelta(days=d)
        idx = pd.date_range(day + pd.Timedelta(hours=9, minutes=30), periods=20, freq="5min")
        close = pd.Series(100.0, index=idx) + np.linspace(0, 0.5, 20)
        frames.append(pd.DataFrame({
            "open": close, "high": close + 0.3, "low": close - 0.3, "close": close,
        }, index=idx))

    today = pd.Timestamp("2026-07-01") + pd.Timedelta(days=prior_days)
    or_closes = [100.0, 100.3, 99.8, 100.4, 99.9, 100.2]  # opening range: ~99.7-100.5
    all_closes = or_closes + today_bars_after_open
    idx = pd.date_range(today + pd.Timedelta(hours=9, minutes=30), periods=len(all_closes), freq="5min")
    close = pd.Series(all_closes, index=idx)
    frames.append(pd.DataFrame({
        "open": close, "high": close + 0.3, "low": close - 0.3, "close": close,
    }, index=idx))
    return pd.concat(frames)


def test_orb_chase_filter_vetoes_extended_early_breakout():
    from bots.organization import orb_chase_filter

    # Opening range tops out ~100.5; jumping to 110 minutes later with no
    # pullback is exactly the "chasing the breakout candle" pattern.
    chasing_df = _synthetic_session_df([100.4, 101.0, 105.0, 110.0])
    assert orb_chase_filter(chasing_df) == "chasing"

    # Still within the opening range, no breakout to chase.
    calm_df = _synthetic_session_df([100.3, 100.4, 100.2, 100.5])
    assert orb_chase_filter(calm_df) is None


def test_orb_chase_filter_only_applies_in_the_early_window():
    from bots.organization import orb_chase_filter

    # Same extended move, but 24+ bars (2h+) after the open -- normal
    # trend continuation by then, not a chase, so no opinion.
    late_bars = [100.4] + [100.5 + i * 0.5 for i in range(30)]  # ends far above OR high
    late_df = _synthetic_session_df(late_bars)
    assert orb_chase_filter(late_df) is None


def test_orb_retest_required_blocks_entry(price_df, tmp_path, journal):
    from bots.organization import DeskConfig

    chasing_df = _synthetic_session_df([100.4, 101.0, 105.0, 110.0])
    broker = PaperBroker(
        starting_cash=10_000, state_path=str(tmp_path / "acct.json"),
        price_overrides={"DEMO": 110.0},
    )
    config = DeskConfig(news_blackout=False, min_copy_score=0, orb_retest_required=True)
    desk = make_desk(tmp_path, broker, journal, chasing_df, config=config)
    report = desk.run_once(symbols=["DEMO"])
    blocked = [a for a in report.actions if "breakout filter" in a.reason]
    assert len(blocked) == 1, report.describe()
    assert not any(a.action == "buy" and a.ok for a in report.actions)


def test_heikin_ashi_smooths_noisy_uptrend_into_a_clean_trend():
    from bots.organization import heikin_ashi, trend_direction

    rng = np.random.default_rng(7)
    n = 60
    base = np.linspace(100, 130, n)  # clear uptrend
    noise = rng.normal(0, 3, n)  # noisy enough to flip a naive close-to-close read
    close = base + noise
    df = pd.DataFrame({
        "open": close - rng.normal(0, 1, n),
        "high": close + abs(rng.normal(0, 1.5, n)),
        "low": close - abs(rng.normal(0, 1.5, n)),
        "close": close,
    })
    ha = heikin_ashi(df)
    assert len(ha) == len(df)
    assert set(ha.columns) == {"open", "high", "low", "close"}
    # HA close should track the same overall uptrend
    assert trend_direction(ha) == "up"
    # HA high/low bound HA open/close, standard HA invariant
    assert (ha["high"] >= ha[["open", "close"]].max(axis=1) - 1e-9).all()
    assert (ha["low"] <= ha[["open", "close"]].min(axis=1) + 1e-9).all()


def test_htf_confirm_blocks_entry_against_higher_timeframe_downtrend(price_df, tmp_path, journal):
    from bots.organization import trend_direction

    downtrend_htf = pd.DataFrame({"close": np.linspace(120, 80, 60)})
    uptrend_htf = pd.DataFrame({"close": np.linspace(80, 120, 60)})
    assert trend_direction(downtrend_htf) == "down"
    assert trend_direction(uptrend_htf) == "up"

    broker = PaperBroker(
        starting_cash=100_000, state_path=str(tmp_path / "acct.json"),
        price_overrides={"EURUSD": 1.1},
    )
    config = DeskConfig(news_blackout=False, min_copy_score=0, timeframe="5m", htf_confirm=True)
    desk = make_desk(
        tmp_path, broker, journal, price_df, config=config,
        htf_history_fn=lambda _s, _tf: downtrend_htf,
    )
    report = desk.run_once(symbols=["EURUSD"])
    blocked = [a for a in report.actions if "higher-timeframe filter" in a.reason]
    assert len(blocked) == 1, report.describe()
    assert not any(a.action == "buy" and a.ok for a in report.actions), report.describe()


def test_htf_confirm_allows_entry_with_higher_timeframe_uptrend(price_df, tmp_path, journal):
    uptrend_htf = pd.DataFrame({"close": np.linspace(80, 120, 60)})
    broker = PaperBroker(
        starting_cash=100_000, state_path=str(tmp_path / "acct.json"),
        price_overrides={"EURUSD": 1.1},
    )
    config = DeskConfig(news_blackout=False, min_copy_score=0, timeframe="5m", htf_confirm=True)
    desk = make_desk(
        tmp_path, broker, journal, price_df, config=config,
        htf_history_fn=lambda _s, _tf: uptrend_htf,
    )
    report = desk.run_once(symbols=["EURUSD"])
    blocked = [a for a in report.actions if "higher-timeframe filter" in a.reason]
    assert len(blocked) == 0, report.describe()


def test_closing_a_trade_updates_the_qtable_live(price_df, tmp_path, journal):
    broker = PaperBroker(
        starting_cash=10_000, state_path=str(tmp_path / "acct.json"),
        price_overrides={"DEMO": 100.0},
    )
    assert broker.buy("DEMO", 10).ok
    agent = QTraderAgent(model_path=str(tmp_path / "q.json"))
    state = agent.current_state(price_df, holding=False)
    record = journal.open_trade("DEMO", "long", 10, 100.0, setup=f"copytrade:{state}")
    before = dict(agent._q_row(state))  # snapshot before any update

    config = DeskConfig(news_blackout=False, min_copy_score=99,
                        risk_per_trade_pct=0.0, stop_loss_pct=0.01, take_profit_pct=0.02)
    desk = make_desk(tmp_path, broker, journal, price_df, config=config, agent=agent)
    broker.price_overrides["DEMO"] = 103.0  # past the 2% take-profit
    report = desk.run_once(symbols=[])

    sells = [a for a in report.actions if a.action == "sell" and a.ok]
    assert len(sells) == 1, report.describe()
    after = agent._q_row(state)
    assert after["buy"] != before.get("buy", 0.0), "Q-value for this state/action didn't move"

    # and it's not just in memory -- it was actually saved to disk
    reloaded = QTraderAgent(model_path=str(tmp_path / "q.json"))
    assert reloaded.load()
    assert reloaded.q[state]["buy"] == pytest.approx(after["buy"])


def test_online_learning_skips_admin_and_manual_trades(price_df, tmp_path, journal):
    broker = PaperBroker(
        starting_cash=10_000, state_path=str(tmp_path / "acct.json"),
        price_overrides={"ADMIN": 100.0, "MANUAL": 100.0},
    )
    assert broker.buy("ADMIN", 10).ok
    assert broker.buy("MANUAL", 10).ok
    agent = QTraderAgent(model_path=str(tmp_path / "q.json"))

    admin_record = journal.open_trade("ADMIN", "long", 10, 100.0, setup="copytrade:trend-up|rsi-neutral|pos-out")
    admin_record.tags.append("admin")
    manual_record = journal.open_trade("MANUAL", "long", 10, 100.0, setup="mambafx-call")  # no ":" state suffix

    config = DeskConfig(news_blackout=False, min_copy_score=99,
                        risk_per_trade_pct=0.0, stop_loss_pct=0.01, take_profit_pct=0.02)
    desk = make_desk(tmp_path, broker, journal, price_df, config=config, agent=agent)
    broker.price_overrides["ADMIN"] = 103.0
    broker.price_overrides["MANUAL"] = 103.0
    desk.run_once(symbols=[])

    # Neither trade should have added any new state to the Q-table
    assert agent.q == {}


def test_breakeven_stop_after_1r(price_df, tmp_path, journal):
    broker = PaperBroker(
        starting_cash=10_000,
        state_path=str(tmp_path / "acct.json"),
        price_overrides={"DEMO": 100.0},
    )
    assert broker.buy("DEMO", 10).ok
    record = journal.open_trade("DEMO", "long", 10, 100.0, setup="daytrade")
    config = DeskConfig(news_blackout=False, min_copy_score=99,
                        risk_per_trade_pct=0.0, stop_loss_pct=0.015,
                        take_profit_pct=0.03)
    desk = make_desk(tmp_path, broker, journal, price_df, config=config)

    # +2% (>= 1R of 1.5%, below 3% target) -> stop arms at breakeven, holds
    broker.price_overrides["DEMO"] = 102.0
    report1 = desk.run_once(symbols=[])
    assert any("breakeven" in n for n in report1.notes), report1.describe()
    assert "breakeven-armed" in journal.trades[record.trade_id].tags
    assert broker.positions() == {"DEMO": 10}

    # pullback to entry -> risk-free exit at ~zero instead of riding to -1.5%
    broker.price_overrides["DEMO"] = 99.9
    report2 = desk.run_once(symbols=[])
    sells = [a for a in report2.actions if a.action == "sell" and a.ok]
    assert sells and "breakeven" in sells[0].reason, report2.describe()
    closed = journal.trades[record.trade_id]
    assert not closed.is_open and closed.pnl == pytest.approx(-1.0, abs=0.01)


def test_rl_exit_does_not_cut_a_breakeven_armed_winner_short(price_df, tmp_path, journal):
    broker = PaperBroker(
        starting_cash=10_000, state_path=str(tmp_path / "acct.json"),
        price_overrides={"DEMO": 100.0},
    )
    assert broker.buy("DEMO", 10).ok
    record = journal.open_trade("DEMO", "long", 10, 100.0, setup="daytrade")
    config = DeskConfig(news_blackout=False, min_copy_score=99,
                        risk_per_trade_pct=0.0, stop_loss_pct=0.015,
                        take_profit_pct=0.03)
    agent = QTraderAgent(model_path=str(tmp_path / "q.json"))
    agent.signal = lambda *a, **k: "sell"  # force an RL exit signal every cycle
    desk = make_desk(tmp_path, broker, journal, price_df, config=config, agent=agent)

    # +2% -> past the 1.5% stop distance -> breakeven-armed, still below
    # the 3% target. A pre-armed RL "sell" here used to cut the winner
    # short well before it reached the real target.
    broker.price_overrides["DEMO"] = 102.0
    report = desk.run_once(symbols=[])
    assert "breakeven-armed" in journal.trades[record.trade_id].tags
    assert broker.positions() == {"DEMO": 10}, report.describe()
    assert not [a for a in report.actions if a.action == "sell" and a.ok], report.describe()


def test_rl_exit_still_works_before_breakeven_armed(price_df, tmp_path, journal):
    broker = PaperBroker(
        starting_cash=10_000, state_path=str(tmp_path / "acct.json"),
        price_overrides={"DEMO": 100.0},
    )
    assert broker.buy("DEMO", 10).ok
    journal.open_trade("DEMO", "long", 10, 100.0, setup="daytrade")
    config = DeskConfig(news_blackout=False, min_copy_score=99,
                        risk_per_trade_pct=0.0, stop_loss_pct=0.015,
                        take_profit_pct=0.03)
    agent = QTraderAgent(model_path=str(tmp_path / "q.json"))
    agent.signal = lambda *a, **k: "sell"
    desk = make_desk(tmp_path, broker, journal, price_df, config=config, agent=agent)

    # Session 34: the RL exit is asymmetric now. A GREEN trade below +1R
    # is left alone (forensics on all 22 real desk trades: zero ever
    # reached the take-profit; 7 slightly-green trades were scratched by
    # this exact exit) -- but a RED trade may still be cut early.
    broker.price_overrides["DEMO"] = 100.3  # slightly green: hands off
    report = desk.run_once(symbols=[])
    sells = [a for a in report.actions if a.action == "sell" and a.ok]
    assert not sells, report.describe()

    broker.price_overrides["DEMO"] = 99.5  # red (above stop): cut the loser
    report = desk.run_once(symbols=[])
    sells = [a for a in report.actions if a.action == "sell" and a.ok]
    assert sells and "RL agent says exit" in sells[0].reason, report.describe()
    assert "cutting the loser" in sells[0].reason


def test_paper_broker_default_has_no_spread_cost(tmp_path):
    # Existing behavior must stay untouched by default -- 79+ other tests
    # assert exact fill prices and rely on this.
    broker = PaperBroker(
        starting_cash=10_000, state_path=str(tmp_path / "acct.json"),
        price_overrides={"EURUSD": 1.1000},
    )
    result = broker.buy("EURUSD", 10)
    assert result.ok and result.fill_price == pytest.approx(1.1000)


def test_paper_broker_model_spread_charges_realistic_cost(tmp_path):
    broker = PaperBroker(
        starting_cash=10_000, state_path=str(tmp_path / "acct.json"),
        price_overrides={"EURUSD": 1.1000}, model_spread=True,
    )
    buy = broker.buy("EURUSD", 10)
    assert buy.ok
    assert buy.fill_price > 1.1000  # bought at the ask, above mid
    sell = broker.sell("EURUSD", 10)
    assert sell.ok
    assert sell.fill_price < 1.1000  # sold at the bid, below mid
    # round trip at the same mid price should show a small net cost, not
    # break even -- this is the whole point (no more frictionless fills)
    assert broker.cash() < 10_000


def test_spread_pct_covers_the_desks_own_index_and_commodity_names():
    """Session 46: spread_pct() only recognized futures-style aliases
    (GC=F, CL=F, NQ=F...) -- the desk's OWN watchlist names (GOLD/SILVER/
    OIL/US30/NAS100/US500/US2000, used in every real launch command) fell
    through to the tight stocks default (0.00005), undercosting these 7 of
    19 watchlist symbols and inflating the paper account's real P&L on
    them."""
    from bots.spreads import spread_pct

    stocks_default = 0.00005
    assert spread_pct("GOLD") == spread_pct("GC=F") != stocks_default
    assert spread_pct("SILVER") == spread_pct("SI=F") != stocks_default
    assert spread_pct("OIL") == spread_pct("CL=F") != stocks_default
    assert spread_pct("NAS100") == spread_pct("NQ=F") != stocks_default
    assert spread_pct("US30") == spread_pct("YM=F") != stocks_default
    assert spread_pct("US500") == spread_pct("ES=F") != stocks_default
    assert spread_pct("US2000") == spread_pct("RTY=F") != stocks_default


def test_spread_pct_widens_crypto_on_weekends(monkeypatch):
    import bots.organization as org_mod
    from bots.spreads import spread_pct

    monkeypatch.setattr(org_mod, "is_weekend_forex_gap", lambda now=None: False)
    weekday = spread_pct("BTC-USD")
    monkeypatch.setattr(org_mod, "is_weekend_forex_gap", lambda now=None: True)
    weekend = spread_pct("BTC-USD")
    assert weekend > weekday
    assert spread_pct("EURUSD") < spread_pct("EURJPY")  # major tighter than JPY cross


def test_is_weekend_forex_gap():
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from bots.organization import is_weekend_forex_gap

    ny = ZoneInfo("America/New_York")
    assert is_weekend_forex_gap(datetime(2026, 7, 18, 12, 0, tzinfo=ny))  # Saturday
    assert is_weekend_forex_gap(datetime(2026, 7, 17, 18, 0, tzinfo=ny))  # Friday 6pm
    assert not is_weekend_forex_gap(datetime(2026, 7, 17, 16, 0, tzinfo=ny))  # Friday 4pm
    assert is_weekend_forex_gap(datetime(2026, 7, 19, 10, 0, tzinfo=ny))  # Sunday 10am
    assert not is_weekend_forex_gap(datetime(2026, 7, 19, 18, 0, tzinfo=ny))  # Sunday 6pm
    assert not is_weekend_forex_gap(datetime(2026, 7, 15, 12, 0, tzinfo=ny))  # Wednesday


def test_weekend_crypto_trades_get_half_size(price_df, tmp_path, journal, monkeypatch):
    import bots.organization as org_mod

    monkeypatch.setattr(org_mod, "is_weekend_forex_gap", lambda now=None: True)

    broker = PaperBroker(
        starting_cash=100_000, state_path=str(tmp_path / "acct.json"),
        price_overrides={"BTC-USD": 60_000.0},
    )
    config = DeskConfig(news_blackout=False, min_copy_score=0, risk_per_trade_pct=0.005)
    desk = make_desk(tmp_path, broker, journal, price_df, config=config)
    report = desk.run_once(symbols=["BTC-USD"])
    buys = [a for a in report.actions if a.action == "buy" and a.ok]
    assert len(buys) == 1
    assert "weekend crypto" in buys[0].reason

    # same setup with the caution disabled -> full size, no note
    config2 = DeskConfig(news_blackout=False, min_copy_score=0, risk_per_trade_pct=0.005,
                         weekend_crypto_caution=False)
    broker2 = PaperBroker(
        starting_cash=100_000, state_path=str(tmp_path / "acct2.json"),
        price_overrides={"BTC-USD": 60_000.0},
    )
    journal2 = TradeJournal(path=str(tmp_path / "journal2.json"))
    desk2 = make_desk(tmp_path, broker2, journal2, price_df, config=config2)
    report2 = desk2.run_once(symbols=["BTC-USD"])
    full_buy = [a for a in report2.actions if a.action == "buy" and a.ok][0]
    assert "weekend crypto" not in full_buy.reason
    assert buys[0].quantity == pytest.approx(full_buy.quantity / 2.0, rel=0.05)


def test_loss_streak_rule_blocks_entries(price_df, tmp_path, journal):
    for i in range(3):
        t = journal.open_trade(f"L{i}", "long", 1, 100.0, setup="s")
        journal.close_trade(t.trade_id, 99.0)  # 3 straight losses today
    assert journal.consecutive_losses_today() == 3

    broker = PaperBroker(
        starting_cash=10_000,
        state_path=str(tmp_path / "acct.json"),
        price_overrides={"DEMO": 100.0},
    )
    desk = make_desk(
        tmp_path, broker, journal, price_df,
        config=DeskConfig(news_blackout=False, min_copy_score=0,
                          max_consecutive_losses=3),
    )
    report = desk.run_once(symbols=["DEMO"])
    assert not [a for a in report.actions if a.action == "buy"]
    assert any("loss-streak" in n for n in report.notes)

    # a winner resets the streak
    w = journal.open_trade("W", "long", 1, 100.0, setup="s")
    journal.close_trade(w.trade_id, 105.0)
    assert journal.consecutive_losses_today() == 0


def test_atr_stops_size_by_volatility(tmp_path, journal):
    import numpy as np
    import pandas as pd

    def make_ohlc(vol):
        rng = np.random.default_rng(9)
        close = 100 + np.cumsum(rng.normal(0, vol, 100))
        return pd.DataFrame({
            "high": close + vol * 2, "low": close - vol * 2, "close": close,
        })

    calm, wild = make_ohlc(0.05), make_ohlc(2.0)

    from bots.organization import atr_pct

    calm_atr, wild_atr = atr_pct(calm), atr_pct(wild)
    assert calm_atr and wild_atr and wild_atr > calm_atr * 5

    # desk sizing: wild instrument gets a wider stop and a smaller position
    quantities = {}
    for name, df in (("CALM", calm), ("WILD", wild)):
        broker = PaperBroker(
            starting_cash=100_000,
            state_path=str(tmp_path / f"acct-{name}.json"),
            price_overrides={name: 100.0},
        )
        desk = make_desk(
            tmp_path, broker, journal, df,
            config=DeskConfig(news_blackout=False, min_copy_score=0,
                              atr_stops=True, max_per_correlation_group=0,
                              max_position_pct=1.0),
        )
        report = desk.run_once(symbols=[name])
        buys = [a for a in report.actions if a.action == "buy" and a.ok]
        if buys:
            quantities[name] = buys[0].quantity
            record = [t for t in journal.trades.values() if t.symbol == name][-1]
            assert record.stop_pct is not None
    if "CALM" in quantities and "WILD" in quantities:
        assert quantities["WILD"] < quantities["CALM"]


def test_max_drawdown_guard_halts_and_stays_halted(tmp_path):
    from bots.risk import MaxDrawdownGuard

    guard = MaxDrawdownGuard(max_total_drawdown_pct=0.05, state_path=str(tmp_path / "dd.json"))
    assert guard.check(10_000)[0] is False  # peak = 10000
    assert guard.check(10_500)[0] is False  # new peak = 10500, no drawdown
    halted, msg = guard.check(9_900)  # down 5.7% from peak 10500 -> breach
    assert halted and "HALTED" in msg
    # stays halted even if equity recovers -- funded accounts don't un-terminate
    still_halted, _ = guard.check(10_600)
    assert still_halted


def test_max_drawdown_guard_size_multiplier_tapers_before_the_halt(tmp_path):
    from bots.risk import MaxDrawdownGuard

    guard = MaxDrawdownGuard(max_total_drawdown_pct=0.05, state_path=str(tmp_path / "dd.json"))
    guard.check(10_000)  # peak = 10000, no drawdown yet
    assert guard.size_multiplier(10_000) == 1.0
    guard.check(9_800)  # 2% drawdown -- under half of the 5% ceiling
    assert guard.size_multiplier(9_800) == 1.0
    guard.check(9_650)  # 3.5% drawdown -- 70% of ceiling used -> half size
    assert guard.size_multiplier(9_650) == 0.5
    guard.check(9_550)  # 4.5% drawdown -- 90% of ceiling used -> quarter size
    assert guard.size_multiplier(9_550) == 0.25


def test_drawdown_taper_reduces_entry_size(price_df, tmp_path, journal):
    from bots.risk import MaxDrawdownGuard

    # Broker equity itself must reflect the drawdown -- run_once() calls
    # max_drawdown_guard.check(broker.equity()) with the real current
    # equity, which would otherwise overwrite/heal any pre-set guard state.
    broker = PaperBroker(
        starting_cash=96_500, state_path=str(tmp_path / "acct.json"),
        price_overrides={"DEMO": 100.0},
    )
    dd_guard = MaxDrawdownGuard(max_total_drawdown_pct=0.05, state_path=str(tmp_path / "dd.json"))
    dd_guard.check(100_000)  # establish a peak above current equity

    # small risk_per_trade_pct so the risk-based budget (not max_position_pct)
    # is the binding constraint on quantity
    config = DeskConfig(news_blackout=False, min_copy_score=0,
                        risk_per_trade_pct=0.005, max_total_drawdown_pct=0.05)
    desk = TradingDesk(
        broker=broker, journal=journal,
        agent=QTraderAgent(model_path=str(tmp_path / "q.json")),
        config=config, history_fn=lambda _s: price_df,
        guard=DrawdownGuard(state_path=str(tmp_path / "day.json")),
        manual_signals_path=str(tmp_path / "manual.json"),
        max_drawdown_guard=dd_guard,
    )
    report = desk.run_once(symbols=["DEMO"])
    buys = [a for a in report.actions if a.action == "buy" and a.ok]
    assert len(buys) == 1
    assert "drawdown taper" in buys[0].reason


def test_funded_account_config_matches_screenshot_numbers():
    from bots.organization import funded_account_config

    cfg = funded_account_config()
    assert cfg.max_daily_loss_pct == 0.03
    assert cfg.max_total_drawdown_pct == 0.05
    assert cfg.day_trading and cfg.atr_stops and cfg.news_blackout


def test_guard_state_isolated_per_broker_and_respects_bot_data_dir(tmp_path, journal, monkeypatch):
    # Real-world scenario this protects: connecting a new broker (e.g.
    # TradeLocker for a funded account) must NOT read yesterday's daily
    # baseline or drawdown peak from a completely different broker/equity
    # scale -- that's exactly the "-900% false drawdown" bug from early in
    # this project. Verifies the fix still holds without needing real
    # TradeLocker credentials: any broker with a distinct .name gets its
    # own isolated guard state file, located wherever BOT_DATA_DIR points.
    monkeypatch.setenv("BOT_DATA_DIR", str(tmp_path))

    class FakeFundedBroker(PaperBroker):
        name = "tradelocker"

    broker = FakeFundedBroker(starting_cash=50_000, state_path=str(tmp_path / "tl_acct.json"))
    desk = TradingDesk(
        broker=broker, journal=journal,
        agent=QTraderAgent(model_path=str(tmp_path / "q.json")),
        config=DeskConfig(news_blackout=False, min_copy_score=0, max_total_drawdown_pct=0.05),
    )
    assert os.path.basename(desk.guard.state_path) == "day_state_tradelocker.json"
    assert os.path.basename(desk.max_drawdown_guard.state_path) == "max_drawdown_state_tradelocker.json"
    # both land inside BOT_DATA_DIR, not the default bot_data/ or paper's dir
    assert os.path.dirname(desk.guard.state_path) == str(tmp_path)
    assert os.path.dirname(desk.max_drawdown_guard.state_path) == str(tmp_path)

    # a $10k paper account's guard state must be a completely separate file
    paper_broker = PaperBroker(starting_cash=10_000, state_path=str(tmp_path / "paper_acct.json"))
    paper_desk = TradingDesk(
        broker=paper_broker, journal=TradeJournal(path=str(tmp_path / "paper_journal.json")),
        agent=QTraderAgent(model_path=str(tmp_path / "q2.json")),
        config=DeskConfig(news_blackout=False, min_copy_score=0, max_total_drawdown_pct=0.05),
    )
    assert paper_desk.guard.state_path != desk.guard.state_path
    assert paper_desk.max_drawdown_guard.state_path != desk.max_drawdown_guard.state_path


def test_two_funded_accounts_of_the_same_broker_type_stay_fully_separate(monkeypatch, tmp_path):
    # The scenario this protects: TWO real funded TradeLocker accounts,
    # different logins, run as two independent processes. Both brokers
    # share the SAME .name ("tradelocker") -- the previous test only
    # proved isolation across different broker TYPES. This proves the
    # actual safety net for running two accounts of the SAME type: point
    # each process at its own BOT_DATA_DIR and the journal, Q-table, and
    # both drawdown guards all separate cleanly with zero shared state --
    # account 1's losses/limits can never affect account 2's, and vice
    # versa, exactly as two financially separate funded accounts require.
    dir_a = tmp_path / "account_1"
    dir_b = tmp_path / "account_2"
    dir_a.mkdir()
    dir_b.mkdir()

    class FakeFundedBroker(PaperBroker):
        name = "tradelocker"

    monkeypatch.setenv("BOT_DATA_DIR", str(dir_a))
    broker_a = FakeFundedBroker(starting_cash=5_000, state_path=str(dir_a / "acct.json"))
    desk_a = TradingDesk(
        broker=broker_a,
        config=DeskConfig(news_blackout=False, min_copy_score=0, max_total_drawdown_pct=0.05),
    )
    # Account 1 takes a loss and its daily guard records it.
    desk_a.journal.open_trade("EURUSD", "long", 1, 100.0, setup="daytrade")
    desk_a.guard.check(5_000)  # establish today's baseline for account 1
    desk_a.guard.check(4_800)  # account 1 down 4% today

    monkeypatch.setenv("BOT_DATA_DIR", str(dir_b))
    broker_b = FakeFundedBroker(starting_cash=5_000, state_path=str(dir_b / "acct.json"))
    desk_b = TradingDesk(
        broker=broker_b,
        config=DeskConfig(news_blackout=False, min_copy_score=0, max_total_drawdown_pct=0.05),
    )

    # Account 2's journal and guard must be completely untouched by
    # account 1's activity -- separate files, separate baselines.
    assert desk_b.journal.path != desk_a.journal.path
    assert not os.path.exists(desk_b.journal.path) or desk_b.journal.trades == {}
    assert desk_b.guard.state_path != desk_a.guard.state_path
    halted_b, msg_b = desk_b.guard.check(5_000)  # account 2, still flat
    assert not halted_b, msg_b
    assert desk_a.agent.model_path != desk_b.agent.model_path


def test_desk_halts_on_total_drawdown_breach(price_df, tmp_path, journal):
    from bots.risk import MaxDrawdownGuard

    broker = PaperBroker(
        starting_cash=10_000,
        state_path=str(tmp_path / "acct.json"),
        price_overrides={"DEMO": 100.0},
    )
    dd_guard = MaxDrawdownGuard(max_total_drawdown_pct=0.05, state_path=str(tmp_path / "dd.json"))
    dd_guard.check(10_000)  # establish peak
    dd_guard.check(9_400)  # pre-breach the guard directly (down 6%)

    desk = TradingDesk(
        broker=broker,
        journal=journal,
        agent=QTraderAgent(model_path=str(tmp_path / "q.json")),
        config=DeskConfig(news_blackout=False, min_copy_score=0, max_total_drawdown_pct=0.05),
        history_fn=lambda _s: price_df,
        guard=DrawdownGuard(state_path=str(tmp_path / "day.json")),
        manual_signals_path=str(tmp_path / "manual.json"),
        max_drawdown_guard=dd_guard,
    )
    report = desk.run_once(symbols=["DEMO"])
    assert not [a for a in report.actions if a.action == "buy"]
    assert any("HALTED" in n for n in report.notes)


def test_daily_profit_target_cashes_out_and_stops(price_df, tmp_path, journal):
    broker = PaperBroker(
        starting_cash=5_000,
        state_path=str(tmp_path / "acct.json"),
        price_overrides={"WIN": 100.0, "NEXT": 50.0},
    )
    guard = DrawdownGuard(state_path=str(tmp_path / "day.json"))
    guard.check(5_000)  # record day-start baseline

    # open a position that then runs up so equity = 5150 (+3% day)
    assert broker.buy("WIN", 10).ok
    journal.open_trade("WIN", "long", 10, 100.0, setup="daytrade")
    broker.price_overrides["WIN"] = 115.0  # equity 4000 cash + 1150 = 5150

    desk = TradingDesk(
        broker=broker,
        journal=journal,
        agent=QTraderAgent(model_path=str(tmp_path / "q.json")),
        config=DeskConfig(news_blackout=False, min_copy_score=0,
                          daily_profit_target_pct=0.02, take_profit_pct=0.99),
        history_fn=lambda _s: price_df,
        guard=guard,
        manual_signals_path=str(tmp_path / "manual.json"),
    )
    report = desk.run_once(symbols=["NEXT"])
    # cashed out the winner, took no new trades
    sells = [a for a in report.actions if a.action == "sell" and a.ok]
    assert sells and "profit target" in sells[0].reason
    assert broker.positions() == {}
    assert not [a for a in report.actions if a.action == "buy"]
    assert any("done for the day" in n for n in report.notes)

    # next cycle same day: realized gain keeps it stopped
    report2 = desk.run_once(symbols=["NEXT"])
    assert not [a for a in report2.actions if a.action == "buy"]


def test_funded_config_includes_profit_target():
    from bots.organization import funded_account_config

    assert funded_account_config().daily_profit_target_pct == 0.03


def test_adx_regime_filter_skips_choppy_markets(tmp_path, journal):
    import numpy as np
    import pandas as pd

    # trending: steady climb -> high ADX. choppy: tight oscillation -> low ADX.
    n = 120
    trend_close = pd.Series(100 + np.arange(n) * 0.5)
    trending = pd.DataFrame({
        "high": trend_close + 0.2, "low": trend_close - 0.2, "close": trend_close,
    })
    chop_close = pd.Series(100 + 0.3 * np.sin(np.arange(n)))
    choppy = pd.DataFrame({
        "high": chop_close + 0.1, "low": chop_close - 0.1, "close": chop_close,
    })

    from bots.organization import adx_value

    adx_trend, adx_chop = adx_value(trending), adx_value(choppy)
    assert adx_trend is not None and adx_chop is not None
    assert adx_trend > 25 and adx_chop < 20, (adx_trend, adx_chop)

    broker = PaperBroker(
        starting_cash=10_000,
        state_path=str(tmp_path / "acct.json"),
        price_overrides={"CHOP": 100.0},
    )
    desk = make_desk(
        tmp_path, broker, journal, choppy,
        config=DeskConfig(news_blackout=False, min_copy_score=0, min_adx=20.0),
    )
    report = desk.run_once(symbols=["CHOP"])
    skips = [a for a in report.actions if "regime filter" in a.reason]
    assert skips and not [a for a in report.actions if a.action == "buy"], report.describe()


def test_high_conviction_override_bypasses_daily_cap(tmp_path, journal):
    import numpy as np
    import pandas as pd

    n = 120
    trend_close = pd.Series(100 + np.arange(n) * 0.5)
    trending = pd.DataFrame({
        "high": trend_close + 0.2, "low": trend_close - 0.2, "close": trend_close,
    })
    from bots.organization import adx_value
    assert adx_value(trending) >= 40  # sanity: this is a genuinely strong trend

    # A trade already opened today -> the 1-trade daily cap is hit.
    journal.open_trade("ALREADY", "long", 1, 100.0, setup="daytrade")

    broker = PaperBroker(
        starting_cash=10_000, state_path=str(tmp_path / "acct.json"),
        price_overrides={"STRONG": 100.0},
    )
    config = DeskConfig(news_blackout=False, min_copy_score=0,
                        max_trades_per_day=1, high_conviction_adx=40.0,
                        max_high_conviction_overrides=1)
    desk = make_desk(tmp_path, broker, journal, trending, config=config)
    report = desk.run_once(symbols=["STRONG"])
    buys = [a for a in report.actions if a.action == "buy" and a.ok]
    assert len(buys) == 1, report.describe()
    assert "high-conviction override" in buys[0].reason

    record = journal.open_position_for("STRONG")
    assert "high-conviction-override" in record.tags


def test_high_conviction_override_disabled_by_default_still_caps(tmp_path, journal):
    import numpy as np
    import pandas as pd

    n = 120
    trend_close = pd.Series(100 + np.arange(n) * 0.5)
    trending = pd.DataFrame({
        "high": trend_close + 0.2, "low": trend_close - 0.2, "close": trend_close,
    })
    journal.open_trade("ALREADY", "long", 1, 100.0, setup="daytrade")

    broker = PaperBroker(
        starting_cash=10_000, state_path=str(tmp_path / "acct.json"),
        price_overrides={"STRONG": 100.0},
    )
    # high_conviction_adx left at its default (0 = off)
    config = DeskConfig(news_blackout=False, min_copy_score=0, max_trades_per_day=1)
    desk = make_desk(tmp_path, broker, journal, trending, config=config)
    report = desk.run_once(symbols=["STRONG"])
    assert not [a for a in report.actions if a.action == "buy" and a.ok], report.describe()
    assert any("daily trade cap" in a.reason for a in report.actions)


def test_high_conviction_overrides_run_out_per_day(tmp_path, journal):
    import numpy as np
    import pandas as pd

    n = 120
    trend_close = pd.Series(100 + np.arange(n) * 0.5)
    trending = pd.DataFrame({
        "high": trend_close + 0.2, "low": trend_close - 0.2, "close": trend_close,
    })
    journal.open_trade("ALREADY", "long", 1, 100.0, setup="daytrade")
    # Already used up today's one allowed override.
    used = journal.open_trade("PRIOR-OVERRIDE", "long", 1, 100.0, setup="daytrade")
    used.tags.append("high-conviction-override")
    journal.save()

    broker = PaperBroker(
        starting_cash=10_000, state_path=str(tmp_path / "acct.json"),
        price_overrides={"STRONG": 100.0},
    )
    config = DeskConfig(news_blackout=False, min_copy_score=0,
                        max_trades_per_day=1, high_conviction_adx=40.0,
                        max_high_conviction_overrides=1)
    desk = make_desk(tmp_path, broker, journal, trending, config=config)
    report = desk.run_once(symbols=["STRONG"])
    assert not [a for a in report.actions if a.action == "buy" and a.ok], report.describe()
    assert any("daily trade cap" in a.reason for a in report.actions)


def test_asian_session_budget_reserves_trades_for_london(price_df, tmp_path, journal, monkeypatch):
    import bots.organization as org_mod

    # 4 of 10 daily trades already used; asian budget 40% -> asian cap is 4,
    # so during the asian session the desk must refuse a 5th entry even
    # though 6 normal daily slots remain.
    for i in range(4):
        journal.open_trade(f"T{i}", "long", 1, 100.0, setup="daytrade")

    broker = PaperBroker(
        starting_cash=10_000, state_path=str(tmp_path / "acct.json"),
        price_overrides={"USDJPY": 150.0},
    )
    config = DeskConfig(news_blackout=False, min_copy_score=0,
                        max_trades_per_day=10, asian_session_budget_pct=0.4)
    desk = make_desk(tmp_path, broker, journal, price_df, config=config)

    monkeypatch.setattr(org_mod, "active_forex_session", lambda now=None: "asian")
    report = desk.run_once(symbols=["USDJPY"])
    assert not [a for a in report.actions if a.action == "buy" and a.ok], report.describe()
    assert any("session budget" in a.reason for a in report.actions), report.describe()

    # Same desk state, but once London is the live session the reserved
    # budget opens back up.
    monkeypatch.setattr(org_mod, "active_forex_session", lambda now=None: "london")
    report2 = desk.run_once(symbols=["USDJPY"])
    assert not any("session budget" in a.reason for a in report2.actions), report2.describe()


def test_tradeability_ranking_gives_strong_trend_first_claim(tmp_path, journal):
    import numpy as np
    import pandas as pd

    n = 120
    trend_close = pd.Series(100 + np.arange(n) * 0.5)
    trending = pd.DataFrame({
        "high": trend_close + 0.2, "low": trend_close - 0.2, "close": trend_close,
    })
    chop_close = pd.Series(100 + 0.3 * np.sin(np.arange(n)))
    choppy = pd.DataFrame({
        "high": chop_close + 0.1, "low": chop_close - 0.1, "close": chop_close,
    })

    from bots.organization import tradeability_score
    assert tradeability_score("STRONG", trending) > tradeability_score("CHOP", choppy)

    # With only ONE trade allowed today, the desk must spend it on the
    # higher-ranked (trending) candidate even though the choppy one comes
    # first alphabetically/in watchlist order.
    broker = PaperBroker(
        starting_cash=10_000, state_path=str(tmp_path / "acct.json"),
        price_overrides={"CHOP": 100.0, "STRONG": 100.0},
    )
    frames = {"CHOP": choppy, "STRONG": trending}
    desk = TradingDesk(
        broker=broker, journal=journal,
        agent=QTraderAgent(model_path=str(tmp_path / "q.json")),
        config=DeskConfig(news_blackout=False, min_copy_score=0, max_trades_per_day=1),
        history_fn=lambda s: frames[s],
        guard=DrawdownGuard(state_path=str(tmp_path / "day.json")),
        manual_signals_path=str(tmp_path / "manual.json"),
    )
    report = desk.run_once(symbols=["CHOP", "STRONG"])
    buys = [a for a in report.actions if a.action == "buy" and a.ok]
    assert len(buys) == 1 and buys[0].symbol == "STRONG", report.describe()
    assert any("[rank] best to trade right now: STRONG" in n for n in report.notes), report.notes


def test_funded_config_includes_regime_filter():
    from bots.organization import funded_account_config

    assert funded_account_config().min_adx == 20.0


def test_continuous_market_drops_orb_and_session_phase():
    import numpy as np
    import pandas as pd

    from bots.learning.agent import _is_continuous_market, extract_state

    rng = np.random.default_rng(2)
    idx = pd.date_range("2026-07-10", periods=3 * 288, freq="5min")  # crypto: no gaps
    px = 100 * np.exp(np.cumsum(rng.normal(0, 0.001, len(idx))))
    crypto = pd.DataFrame({
        "high": px * 1.001, "low": px * 0.999, "close": px,
        "volume": rng.integers(100, 500, len(idx)),
    }, index=idx)
    assert _is_continuous_market(crypto, 5)
    state = extract_state(crypto, 400, holding=False)
    assert "vwap-" in state and "orb-" not in state and "tod-" not in state

    frames = []
    for day in ("2026-07-08", "2026-07-09", "2026-07-10"):
        session_idx = pd.date_range(f"{day} 09:30", periods=78, freq="5min")
        p = 100 * np.exp(np.cumsum(rng.normal(0, 0.001, 78)))
        frames.append(pd.DataFrame({
            "high": p * 1.001, "low": p * 0.999, "close": p,
            "volume": rng.integers(100, 500, 78),
        }, index=session_idx))
    stocks = pd.concat(frames)
    assert not _is_continuous_market(stocks, 5)
    stock_state = extract_state(stocks, 100, holding=False)
    assert "orb-" in stock_state and "tod-" in stock_state


def test_drop_forming_bar_removes_incomplete_live_candle():
    import numpy as np
    import pandas as pd

    from bots.learning.agent import _drop_forming_bar

    now = pd.Timestamp.now().floor("min")
    idx = pd.date_range(end=now, periods=40, freq="5min")
    # nudge the last bar to look freshly started (a few seconds old), like
    # a real live intraday fetch mid-candle
    idx = idx[:-1].append(pd.DatetimeIndex([now - pd.Timedelta(seconds=5)]))
    df = pd.DataFrame({"close": np.arange(40, dtype=float)}, index=idx)

    trimmed = _drop_forming_bar(df)
    assert len(trimmed) == len(df) - 1
    assert trimmed.index[-1] == idx[-2]  # kept the last fully-closed bar


def test_drop_forming_bar_keeps_daily_bars_untouched():
    import pandas as pd

    from bots.learning.agent import _drop_forming_bar

    idx = pd.date_range("2026-01-01", periods=30, freq="1D")
    df = pd.DataFrame({"close": range(30)}, index=idx)
    assert len(_drop_forming_bar(df)) == len(df)


# ---------------------------------------------------------------------------
# TradeLocker connector safety (mocked TLAPI -- no credentials, no network)
# ---------------------------------------------------------------------------

class _FakeTLAPI:
    """Stands in for tradelocker.TLAPI: a firm that names gold XAUUSD and
    the Nasdaq index US100, with one EURUSD long already open."""

    INSTRUMENTS = {"EURUSD": 1, "XAUUSD": 101, "US100": 202, "WTI": 404}

    def __init__(self, *args, **kwargs):
        self.orders = []
        self.closed = []
        self.reject_orders = False
        self.positions_rows = []

    def get_instrument_id_from_symbol_name(self, name):
        if name not in self.INSTRUMENTS:
            raise ValueError(f"No instrument found with symbol_name='{name}'")
        return self.INSTRUMENTS[name]

    def get_symbol_name_from_instrument_id(self, iid):
        return {v: k for k, v in self.INSTRUMENTS.items()}[int(iid)]

    def get_all_positions(self):
        return pd.DataFrame(
            self.positions_rows,
            columns=["id", "tradableInstrumentId", "side", "qty", "avgPrice"],
        )

    def get_latest_asking_price(self, iid):
        return {1: 1.1000, 101: 2400.0, 202: 20000.0, 404: 78.5}[int(iid)]

    def get_account_state(self):
        return {"availableFunds": 5000.0, "equity": 5000.0}

    def create_order(self, iid, quantity, side, type_="market", **kwargs):
        if self.reject_orders:
            return None
        self.orders.append({"iid": iid, "quantity": quantity, "side": side, **kwargs})
        return len(self.orders)

    def close_position(self, order_id=0, position_id=0, close_quantity=0):
        self.closed.append({"position_id": position_id, "close_quantity": close_quantity})
        return True


@pytest.fixture
def tl_broker(monkeypatch):
    import tradelocker

    from bots.brokers.tradelocker_broker import TradeLockerBroker

    monkeypatch.setattr(tradelocker, "TLAPI", _FakeTLAPI)
    monkeypatch.setenv("TRADELOCKER_EMAIL", "x@y.z")
    monkeypatch.setenv("TRADELOCKER_PASSWORD", "pw")
    monkeypatch.setenv("TRADELOCKER_SERVER", "DEMO-SRV")
    monkeypatch.delenv("TRADELOCKER_LIVE", raising=False)
    return TradeLockerBroker()


def test_tradelocker_demo_by_default_and_alias_resolution(tl_broker):
    assert tl_broker.is_paper  # never live without TRADELOCKER_LIVE=1
    # watchlist names resolve to whatever this firm calls the instrument
    assert tl_broker._instrument_id("GOLD") == 101
    assert tl_broker._instrument_id("NAS100") == 202
    assert tl_broker.price("GOLD") == 2400.0
    with pytest.raises(ValueError, match="US500"):
        tl_broker._instrument_id("US500")  # firm doesn't offer it -> loud error


def test_tradelocker_oil_resolves_to_wti(tl_broker):
    # Session 48: confirmed against a real AquaFunded account -- their
    # EQUITY_CFD name for crude oil is literally "WTI", not any of the
    # previously-tried aliases (USOIL, XTIUSD, WTIUSD, CRUDEOIL).
    assert tl_broker._instrument_id("OIL") == 404
    assert tl_broker.price("OIL") == 78.5


def test_tradelocker_positions_map_back_to_desk_names_after_restart(tl_broker):
    # Fresh broker, no forward lookups yet (the container-restart case):
    # an open XAUUSD position must come back as GOLD, or the desk's
    # reconciler would mistake the rename for a closed trade.
    tl_broker.api.positions_rows = [[7, 101, "buy", 0.5, 2390.0]]
    assert tl_broker.positions() == {"GOLD": 0.5}


def test_tradelocker_bracket_attaches_stops_and_never_enters_unprotected(tl_broker):
    result = tl_broker.buy_bracket("EURUSD", 100_000, 0.005, 0.01)
    assert result.ok
    order = tl_broker.api.orders[-1]
    # offsets measured from the 1.10 ask: 0.5% stop, 1% target
    assert order["stop_loss"] == pytest.approx(0.0055)
    assert order["stop_loss_type"] == "offset"
    assert order["take_profit"] == pytest.approx(0.011)
    assert order["take_profit_type"] == "offset"

    # bracket rejected -> NO fallback to an unprotected plain buy
    tl_broker.api.reject_orders = True
    before = len(tl_broker.api.orders)
    result = tl_broker.buy_bracket("EURUSD", 100_000, 0.005, 0.01)
    assert not result.ok and "unprotected" in result.error
    assert len(tl_broker.api.orders) == before


def test_tradelocker_sell_closes_position_instead_of_opening_short(tl_broker):
    # hedging-mode accounts: a naked sell would OPEN a short next to the
    # long; exits must go through the position endpoint instead
    tl_broker.api.positions_rows = [[9, 1, "buy", 1.0, 1.0950]]
    result = tl_broker.sell("EURUSD", 100_000)  # 1.0 lots
    assert result.ok
    assert tl_broker.api.closed == [{"position_id": 9, "close_quantity": 0}]
    assert tl_broker.api.orders == []  # no sell order was ever submitted

    # partial exit passes the partial quantity through
    tl_broker.api.closed.clear()
    tl_broker.sell("EURUSD", 50_000)  # 0.5 of the 1.0-lot position
    assert tl_broker.api.closed == [{"position_id": 9, "close_quantity": 0.5}]


def test_tradelocker_weekend_crypto_round_trips_to_journal_name(tl_broker):
    # the weekend fallback journals "BTC-USD" (Yahoo-style); TradeLocker
    # calls it BTCUSD. Both directions must agree or the reconciler
    # orphan-closes a live weekend position.
    tl_broker.api.INSTRUMENTS = dict(tl_broker.api.INSTRUMENTS, BTCUSD=303)
    assert tl_broker._instrument_id("BTC-USD") == 303
    tl_broker.api.positions_rows = [[11, 303, "buy", 0.02, 60000.0]]
    assert tl_broker.positions() == {"BTC-USD": 0.02}

    # and on a fresh broker (post-restart, cold cache) the reverse alias
    # still maps it home
    import tradelocker

    from bots.brokers.tradelocker_broker import TradeLockerBroker

    fresh = TradeLockerBroker()
    fresh.api.INSTRUMENTS = dict(fresh.api.INSTRUMENTS, BTCUSD=303)
    fresh.api.positions_rows = [[11, 303, "buy", 0.02, 60000.0]]
    assert fresh.positions() == {"BTC-USD": 0.02}


# ---------------------------------------------------------------------------
# Session 31: bad-market survival -- loss-budget headroom + rollover blackout
# ---------------------------------------------------------------------------

def test_daily_loss_headroom_shrinks_as_the_day_gets_worse(tmp_path):
    guard = DrawdownGuard(max_daily_loss_pct=0.03, state_path=str(tmp_path / "d.json"))
    guard.check(10_000)  # day baseline
    # fresh day: full budget = 80% of 3% = 2.4%
    assert guard.loss_headroom_pct(10_000, 0.8) == pytest.approx(0.024)
    # down 1.6% on the day: only 0.8% of the budget remains (as a fraction
    # of current equity, slightly larger denominator)
    remaining = guard.loss_headroom_pct(9_840, 0.8)
    assert remaining == pytest.approx((9_840 - 9_760) / 9_840)
    # past the 2.4% consumption point: nothing left, even though the hard
    # 3% halt has NOT tripped yet -- the last 0.6% is slippage insurance
    assert guard.loss_headroom_pct(9_750, 0.8) == 0.0
    halted, _ = guard.check(9_750)
    assert not halted  # proves headroom hits zero BEFORE the breach line


def test_entry_risk_is_capped_to_remaining_daily_headroom(price_df, tmp_path, journal):
    broker = PaperBroker(
        starting_cash=100_000, state_path=str(tmp_path / "acct.json"),
        price_overrides={"DEMO": 100.0},
    )
    guard = DrawdownGuard(max_daily_loss_pct=0.03, state_path=str(tmp_path / "d.json"))
    guard.check(100_000)
    # simulate being down 1.6% on the day by re-checking at lower equity
    guard.check(98_400)

    desk = TradingDesk(
        broker=broker, journal=journal,
        agent=QTraderAgent(model_path=str(tmp_path / "q.json")),
        config=DeskConfig(
            news_blackout=False, min_copy_score=0,
            risk_per_trade_pct=0.015, stop_loss_pct=0.01, take_profit_pct=0.02,
        ),
        history_fn=lambda _s: price_df,
        guard=guard,
        manual_signals_path=str(tmp_path / "manual.json"),
    )
    action = desk._consider_entry("DEMO", "test", equity=98_400)
    if action.action == "buy":
        # risk was capped: quantity * stop distance <= remaining headroom
        worst_loss = action.quantity * 100.0 * 0.01
        assert worst_loss <= 98_400 * guard.loss_headroom_pct(98_400, 0.8) * 1.01
    # deep in the hole: entries refuse outright while check() still says go
    guard.check(97_500)
    action = desk._consider_entry("DEMO", "test", equity=97_500)
    assert action.action == "skip"
    assert "loss budget is spent" in action.reason


def test_max_drawdown_headroom_caps_toward_the_account_ceiling(tmp_path):
    from bots.risk import MaxDrawdownGuard

    guard = MaxDrawdownGuard(max_total_drawdown_pct=0.05, state_path=str(tmp_path / "m.json"))
    guard.check(10_000)  # peak recorded
    # fresh: 80% of 5% = 4% headroom
    assert guard.loss_headroom_pct(10_000, 0.8) == pytest.approx(0.04)
    # equity at 96.5% of peak: only 0.5% of the budgeted floor remains
    remaining = guard.loss_headroom_pct(9_650, 0.8)
    assert remaining == pytest.approx((9_650 - 9_600) / 9_650)
    # below the budgeted floor: zero, though the hard 5% halt hasn't hit
    assert guard.loss_headroom_pct(9_550, 0.8) == 0.0
    halted, _ = guard.check(9_550)
    assert not halted


def test_rollover_window_detection():
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from bots.organization import is_rollover_window

    ny = ZoneInfo("America/New_York")
    assert is_rollover_window(datetime(2026, 7, 15, 17, 0, tzinfo=ny))   # 5pm sharp
    assert is_rollover_window(datetime(2026, 7, 15, 16, 45, tzinfo=ny))  # drain starts
    assert is_rollover_window(datetime(2026, 7, 15, 18, 14, tzinfo=ny))  # still re-quoting
    assert not is_rollover_window(datetime(2026, 7, 15, 16, 44, tzinfo=ny))
    assert not is_rollover_window(datetime(2026, 7, 15, 18, 15, tzinfo=ny))
    assert not is_rollover_window(datetime(2026, 7, 15, 12, 0, tzinfo=ny))  # midday


def test_rollover_blackout_blocks_forex_entries_but_not_crypto(price_df, tmp_path, journal, monkeypatch):
    import bots.organization as org

    broker = PaperBroker(
        starting_cash=10_000, state_path=str(tmp_path / "acct.json"),
        price_overrides={"EURUSD": 1.1, "BTC-USD": 60_000.0},
    )
    desk = make_desk(
        tmp_path, broker, journal, price_df,
        config=DeskConfig(news_blackout=False, min_copy_score=0, rollover_blackout=True),
    )
    monkeypatch.setattr(org, "is_rollover_window", lambda now=None: True)
    action = desk._consider_entry("EURUSD", "test", equity=10_000)
    assert action.action == "skip" and "rollover blackout" in action.reason
    # crypto trades straight through the rollover window
    action = desk._consider_entry("BTC-USD", "test", equity=10_000)
    assert "rollover" not in (action.reason or "")


def test_friday_close_window_detection():
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from bots.organization import is_friday_close_window

    ny = ZoneInfo("America/New_York")
    # 2026-07-17 is a Friday
    assert is_friday_close_window(datetime(2026, 7, 17, 16, 30, tzinfo=ny))
    assert is_friday_close_window(datetime(2026, 7, 17, 16, 59, tzinfo=ny))
    assert not is_friday_close_window(datetime(2026, 7, 17, 16, 29, tzinfo=ny))
    assert not is_friday_close_window(datetime(2026, 7, 17, 17, 0, tzinfo=ny))
    # same time Thursday: not the weekly close
    assert not is_friday_close_window(datetime(2026, 7, 16, 16, 45, tzinfo=ny))


def test_friday_flatten_closes_positions_before_the_weekend(price_df, tmp_path, journal, monkeypatch):
    import bots.organization as org

    broker = PaperBroker(
        starting_cash=10_000, state_path=str(tmp_path / "acct.json"),
        price_overrides={"EURUSD": 1.1, "BTC-USD": 60_000.0},
    )
    broker.buy("EURUSD", 1_000)
    broker.buy("BTC-USD", 0.01)
    journal.open_trade("EURUSD", "long", 1_000, 1.1, setup="daytrade")
    journal.open_trade("BTC-USD", "long", 0.01, 60_000.0, setup="daytrade")

    desk = make_desk(
        tmp_path, broker, journal, price_df,
        config=DeskConfig(news_blackout=False, min_copy_score=0, friday_flatten=True),
    )
    monkeypatch.setattr(org, "is_friday_close_window", lambda now=None: True)
    report = desk.run_once(symbols=["EURUSD"])
    # forex position force-closed before the weekend; crypto (24/7, exempt
    # from the weekend-holding rule's intent here) is left alone
    assert broker.positions().get("EURUSD", 0) == 0
    assert broker.positions().get("BTC-USD", 0) > 0
    assert any("Friday close-out" in n for n in report.notes)
    # and no new entries started during the window
    assert not [a for a in report.actions if a.action == "buy"]


def test_weekend_symbols_none_disables_fallback():
    # "--weekend-symbols none" must translate to no weekend fallback at all
    # (prop accounts without the weekend add-on may not trade Sat/Sun)
    from bots.cli import resolve_weekend_symbols

    assert resolve_weekend_symbols("none", "forex", funded=True) is None
    assert resolve_weekend_symbols("NONE", "forex", funded=True) is None
    # default funded-forex behaviour: the three deepest 24/7 crypto books
    assert resolve_weekend_symbols("", "forex", funded=True) == ["BTC-USD", "ETH-USD", "SOL-USD"]
    assert resolve_weekend_symbols("", "stocks", funded=True) is None
    assert resolve_weekend_symbols("SOL-USD", "forex", funded=True) == ["SOL-USD"]


# ---------------------------------------------------------------------------
# Session 33: token-free in-loop self-correction (cooldown + symbol probation)
# ---------------------------------------------------------------------------

def test_symbol_stats_and_minutes_since_last_loss(journal):
    a = journal.open_trade("GBPJPY", "long", 1, 100.0, setup="daytrade")
    journal.close_trade(a.trade_id, 99.0)   # loss, just now
    b = journal.open_trade("GBPJPY", "long", 1, 100.0, setup="daytrade")
    journal.close_trade(b.trade_id, 102.0)  # win
    stats = journal.symbol_stats("GBPJPY")
    assert stats["trades"] == 2 and stats["win_rate"] == 0.5
    assert stats["pnl"] == pytest.approx(1.0)
    # loss just closed -> minutes-since is tiny; never-lost symbol -> None
    assert journal.minutes_since_last_loss("GBPJPY") < 1.0
    assert journal.minutes_since_last_loss("EURUSD") is None


def test_cooldown_blocks_reentry_after_a_loss(price_df, tmp_path, journal):
    broker = PaperBroker(
        starting_cash=10_000, state_path=str(tmp_path / "acct.json"),
        price_overrides={"EURUSD": 1.1},
    )
    desk = make_desk(
        tmp_path, broker, journal, price_df,
        config=DeskConfig(news_blackout=False, min_copy_score=0,
                          symbol_cooldown_minutes=30),
    )
    t = journal.open_trade("EURUSD", "long", 1_000, 1.1, setup="daytrade")
    journal.close_trade(t.trade_id, 1.09)  # stopped out moments ago
    action = desk._consider_entry("EURUSD", "test", equity=10_000)
    assert action.action == "skip" and "anti-revenge" in action.reason
    # a different symbol is unaffected
    broker.price_overrides["GBPUSD"] = 1.27
    action = desk._consider_entry("GBPUSD", "test", equity=10_000)
    assert "anti-revenge" not in (action.reason or "")


def test_symbol_probation_halves_size_when_net_negative(price_df, tmp_path, journal):
    broker = PaperBroker(
        starting_cash=100_000, state_path=str(tmp_path / "acct.json"),
        price_overrides={"GBPJPY": 190.0, "EURUSD": 1.1},
    )
    # build a 10-trade net-negative record for GBPJPY (older than any cooldown)
    for i in range(10):
        t = journal.open_trade("GBPJPY", "long", 1, 190.0, setup="daytrade")
        journal.close_trade(t.trade_id, 189.0 if i < 6 else 191.0)
    assert journal.symbol_stats("GBPJPY")["pnl"] < 0

    desk = make_desk(
        tmp_path, broker, journal, price_df,
        # risk small enough that the risk-budget leg (not max_position_pct
        # or cash) is what actually binds, so the halving is observable
        config=DeskConfig(news_blackout=False, min_copy_score=0,
                          symbol_probation=True, risk_per_trade_pct=0.001,
                          stop_loss_pct=0.01, take_profit_pct=0.02),
    )
    probation = desk._consider_entry("GBPJPY", "test", equity=100_000)
    clean = desk._consider_entry("EURUSD", "test", equity=100_000)
    if probation.action == "buy" and clean.action == "buy":
        # same config, same equity: the probation symbol gets half the
        # dollar exposure of the clean-record symbol
        probation_dollars = probation.quantity * 190.0
        clean_dollars = clean.quantity * 1.1
        assert probation_dollars == pytest.approx(clean_dollars / 2, rel=0.05)


# ---------------------------------------------------------------------------
# Session 37: hybrid exit -- trail after target (config-gated, default OFF)
# ---------------------------------------------------------------------------

def _trail_desk(tmp_path, journal, price_df, trail):
    broker = PaperBroker(
        starting_cash=10_000, state_path=str(tmp_path / "acct.json"),
        price_overrides={"DEMO": 100.0},
    )
    broker.buy("DEMO", 10)
    journal.open_trade("DEMO", "long", 10, 100.0, setup="daytrade")
    config = DeskConfig(news_blackout=False, min_copy_score=99,
                        risk_per_trade_pct=0.0, stop_loss_pct=0.01,
                        take_profit_pct=0.02, breakeven_at_1r=False,
                        trail_after_target=trail)
    agent = QTraderAgent(model_path=str(tmp_path / "q.json"))
    agent.signal = lambda *a, **k: "hold"
    return broker, make_desk(tmp_path, broker, journal, price_df,
                             config=config, agent=agent)


def test_fixed_target_still_exits_by_default(price_df, tmp_path, journal):
    broker, desk = _trail_desk(tmp_path, journal, price_df, trail=False)
    broker.price_overrides["DEMO"] = 102.1  # past the 2% target
    report = desk.run_once(symbols=[])
    sells = [a for a in report.actions if a.action == "sell" and a.ok]
    assert sells and "take profit hit" in sells[0].reason


def test_trail_after_target_lets_winners_run_then_locks_gains(price_df, tmp_path, journal):
    broker, desk = _trail_desk(tmp_path, journal, price_df, trail=True)
    # reaching the target no longer exits -- the trade converts to a trail
    broker.price_overrides["DEMO"] = 102.1
    report = desk.run_once(symbols=[])
    assert not [a for a in report.actions if a.action == "sell" and a.ok]
    # runs to +4%: still holding (trail is 1 stop-distance = 1% below hwm)
    broker.price_overrides["DEMO"] = 104.0
    report = desk.run_once(symbols=[])
    assert not [a for a in report.actions if a.action == "sell" and a.ok]
    # pulls back 1% off the 4% high-water mark: trail fires, gains locked
    broker.price_overrides["DEMO"] = 102.9
    report = desk.run_once(symbols=[])
    sells = [a for a in report.actions if a.action == "sell" and a.ok]
    assert sells and "trailing stop hit" in sells[0].reason
    # exit price is far above the original 2% target -- the runner ran
    assert broker.price_overrides["DEMO"] > 102.0


def test_payout_readiness_gates_and_eligibility(journal):
    from datetime import datetime, timedelta, timezone

    # young account, no profit: blocked for the right reasons
    r = journal.payout_readiness()
    assert not r["eligible"]
    assert any("minimum" in b for b in r["blockers"])

    # build 15 days of steady closed profit, $10/day -- old enough, spread
    # enough, big enough
    now = datetime.now(timezone.utc)
    for i in range(15):
        t = journal.open_trade("EURUSD", "long", 1000, 1.10, setup="daytrade")
        t.entry_time = (now - timedelta(days=16 - i)).isoformat()
        journal.close_trade(t.trade_id, 1.11)
        t.pnl = 10.0
        t.exit_time = t.entry_time
    journal.save()
    r = journal.payout_readiness()
    assert r["eligible"], r["blockers"]
    assert r["profit"] == pytest.approx(150.0)
    assert r["trading_days"] >= 5

    # one monster day dominating profit: consistency gate blocks the payout
    t = journal.open_trade("EURUSD", "long", 1000, 1.10, setup="daytrade")
    t.entry_time = (now - timedelta(days=1)).isoformat()
    journal.close_trade(t.trade_id, 1.20)
    t.pnl = 500.0
    t.exit_time = t.entry_time
    journal.save()
    r = journal.payout_readiness()
    assert not r["eligible"]
    assert any("consistency" in b for b in r["blockers"])


def test_vol_spike_filter_blocks_entry_after_flash_bar(tmp_path, journal):
    rng = np.random.default_rng(11)
    n = 60
    closes = 100 * np.exp(np.cumsum(rng.normal(0.0005, 0.001, n)))
    df = pd.DataFrame({
        "high": closes * 1.001, "low": closes * 0.999, "close": closes,
    })
    # last completed bar is a flash move: ~5% range vs ~0.2% typical
    df.loc[df.index[-1], "high"] = closes[-1] * 1.05
    df.loc[df.index[-1], "low"] = closes[-1] * 0.998

    broker = PaperBroker(
        starting_cash=10_000, state_path=str(tmp_path / "acct.json"),
        price_overrides={"BTC-USD": float(closes[-1])},
    )
    desk = make_desk(
        tmp_path, broker, journal, df,
        config=DeskConfig(news_blackout=False, min_copy_score=0,
                          vol_spike_entry_filter=3.0),
    )
    action = desk._consider_entry("BTC-USD", "test", equity=10_000)
    assert action.action == "skip" and "flash-move guard" in action.reason

    # calm last bar: the filter stays out of the way
    df.loc[df.index[-1], "high"] = closes[-1] * 1.001
    df.loc[df.index[-1], "low"] = closes[-1] * 0.999
    action = desk._consider_entry("BTC-USD", "test", equity=10_000)
    assert "flash-move guard" not in (action.reason or "")


def test_adr_exhaustion_blocks_late_entries(tmp_path, journal):
    rng = np.random.default_rng(21)
    frames = []
    # five prior days each ranging ~1.0 around a 100 base
    for d in range(5):
        idx = pd.date_range(f"2026-07-{6 + d} 00:00", periods=96, freq="15min")
        base = 100 + rng.normal(0, 0.1)
        closes = base + rng.normal(0, 0.1, 96)
        frames.append(pd.DataFrame({
            "high": closes + 0.5, "low": closes - 0.5, "close": closes,
        }, index=idx))
    # today: already ranged ~1.5 (150% of ADR)
    idx = pd.date_range("2026-07-11 00:00", periods=48, freq="15min")
    closes = 100 + rng.normal(0, 0.1, 48)
    today = pd.DataFrame({
        "high": closes + 0.75, "low": closes - 0.75, "close": closes,
    }, index=idx)
    df = pd.concat(frames + [today])

    broker = PaperBroker(
        starting_cash=10_000, state_path=str(tmp_path / "acct.json"),
        price_overrides={"EURUSD": float(closes[-1])},
    )
    desk = make_desk(
        tmp_path, broker, journal, df,
        config=DeskConfig(news_blackout=False, min_copy_score=0,
                          adr_exhaustion_pct=1.0),
    )
    action = desk._consider_entry("EURUSD", "test", equity=10_000)
    assert action.action == "skip" and "ADR exhaustion" in action.reason

    # a fresh day with most of its range left is allowed through
    quiet = pd.DataFrame({
        "high": closes[:48] + 0.1, "low": closes[:48] - 0.1, "close": closes[:48],
    }, index=idx)
    df2 = pd.concat(frames + [quiet])
    desk2 = make_desk(
        tmp_path, broker, journal, df2,
        config=DeskConfig(news_blackout=False, min_copy_score=0,
                          adr_exhaustion_pct=1.0),
    )
    action = desk2._consider_entry("EURUSD", "test", equity=10_000)
    assert "ADR exhaustion" not in (action.reason or "")


def test_live_spread_veto_blocks_blown_out_spreads(price_df, tmp_path, journal):
    class WideSpreadBroker(PaperBroker):
        def live_spread_pct(self, symbol):
            return 0.0010  # 10x EURUSD's normal 0.0001

    broker = WideSpreadBroker(
        starting_cash=10_000, state_path=str(tmp_path / "acct.json"),
        price_overrides={"EURUSD": 1.1},
    )
    desk = make_desk(
        tmp_path, broker, journal, price_df,
        config=DeskConfig(news_blackout=False, min_copy_score=0,
                          max_live_spread_multiple=3.0),
    )
    action = desk._consider_entry("EURUSD", "test", equity=10_000)
    assert action.action == "skip" and "spread veto" in action.reason

    # a venue that can't report a live spread is not penalized
    normal = PaperBroker(
        starting_cash=10_000, state_path=str(tmp_path / "b.json"),
        price_overrides={"EURUSD": 1.1},
    )
    desk = make_desk(
        tmp_path, normal, journal, price_df,
        config=DeskConfig(news_blackout=False, min_copy_score=0,
                          max_live_spread_multiple=3.0),
    )
    action = desk._consider_entry("EURUSD", "test", equity=10_000)
    assert "spread veto" not in (action.reason or "")


def test_tradelocker_live_spread_from_bid_ask(tl_broker):
    tl_broker.api.get_latest_bid_price = lambda iid: 1.0999
    live = tl_broker.live_spread_pct("EURUSD")  # ask fixed at 1.1000
    assert live == pytest.approx(0.0001 / 1.09995, rel=1e-3)


def test_funded_config_defaults_to_one_minute_candles():
    # Law, not a suggestion: funded accounts trade 1-minute candles,
    # matching MambaFX's own documented timeframe. Must hold even if a
    # launch script forgets to pass --timeframe explicitly.
    from bots.organization import funded_account_config

    assert funded_account_config().timeframe == "1m"


def test_cli_funded_timeframe_defaults_to_one_minute_without_the_flag():
    from bots.cli import cmd_autopilot

    class FakeArgs:
        broker = "paper"
        live_i_understand_the_risk = False
        timeframe = None  # simulates --timeframe never being passed
        funded = True
        firm_preset = None
        day_trading = False
        llm_committee = False
        symbols = "EURUSD"
        stock_symbols = ""
        market = "forex"
        weekend_symbols = "none"
        interval = 1

    captured = {}

    def fake_run_autopilot(*, desk, **kwargs):
        captured["timeframe"] = desk.config.timeframe
        raise SystemExit(0)  # stop before it actually loops

    import bots.autopilot as autopilot_mod

    orig = autopilot_mod.run_autopilot
    autopilot_mod.run_autopilot = fake_run_autopilot
    try:
        try:
            cmd_autopilot(FakeArgs())
        except SystemExit:
            pass
    finally:
        autopilot_mod.run_autopilot = orig
    assert captured["timeframe"] == "1m"


def test_cli_firm_preset_selects_the_real_firm_rules_not_generic_defaults():
    # Session 48: --firm-preset must actually change which config gets
    # built, not just exist as a flag -- this is the gap that was found:
    # --funded alone always used generic funded_account_config() (5% max
    # drawdown), never a firm's CONFIRMED real numbers.
    from bots.cli import cmd_autopilot

    class FakeArgs:
        broker = "paper"
        live_i_understand_the_risk = False
        timeframe = None
        funded = False  # deliberately NOT set -- firm_preset alone must imply it
        firm_preset = "aquafunded"
        day_trading = False
        llm_committee = False
        symbols = "EURUSD"
        stock_symbols = ""
        market = "forex"
        weekend_symbols = "none"
        interval = 1

    captured = {}

    def fake_run_autopilot(*, desk, **kwargs):
        captured["config"] = desk.config
        raise SystemExit(0)

    import bots.autopilot as autopilot_mod

    orig = autopilot_mod.run_autopilot
    autopilot_mod.run_autopilot = fake_run_autopilot
    try:
        try:
            cmd_autopilot(FakeArgs())
        except SystemExit:
            pass
    finally:
        autopilot_mod.run_autopilot = orig

    cfg = captured["config"]
    assert cfg.max_daily_loss_pct == 0.03
    assert cfg.max_total_drawdown_pct == 0.06  # AquaFunded's real number, not the generic 0.05
    assert cfg.timeframe == "1m"  # funded implied -> 1-minute law still applies


# ---------------------------------------------------------------------------
# Session 42: zone-touch-count "predict off the zone" filter (backed by
# real backtest stats: fresh levels reverse ~70%, well-tested ones break
# through ~75% -- the opposite of "more touches = stronger wall" folklore)
# ---------------------------------------------------------------------------

def test_zone_touch_count_collapses_consecutive_bars_into_one_touch():
    idx = pd.date_range("2026-07-01", periods=20, freq="5min")
    # baseline sits well clear of the zone band (level=100, tolerance_pct
    # 0.001 -> band=0.1) so only the deliberately-raised bars register
    highs = [90.0] * 20
    lows = [89.5] * 20
    # two separate touch EVENTS at level 100: bars 2-3 (one pause), then
    # bar 10 alone (second event)
    for i in (2, 3, 10):
        highs[i] = 100.0
        lows[i] = 99.95
    df = pd.DataFrame({"high": highs, "low": lows}, index=idx)
    assert zone_touch_count(df, 100.0, tolerance_pct=0.001) == 2


def test_zone_touch_count_zero_on_a_virgin_level():
    idx = pd.date_range("2026-07-01", periods=20, freq="5min")
    df = pd.DataFrame({"high": [90.0] * 20, "low": [89.5] * 20}, index=idx)
    assert zone_touch_count(df, 150.0, tolerance_pct=0.001) == 0


def test_zone_filter_blocks_entry_at_a_fresh_untested_level(tmp_path, journal):
    idx = pd.date_range("2026-07-01", periods=60, freq="5min")
    # a clean, quiet run right up to a brand-new high with only ONE bar
    # ever touching it -- a virgin level, per the test's own definition
    closes = [100.0 + i * 0.001 for i in range(59)] + [100.5]
    highs = [c + 0.02 for c in closes]
    lows = [c - 0.02 for c in closes]
    df = pd.DataFrame({"high": highs, "low": lows, "close": closes}, index=idx)

    broker = PaperBroker(
        starting_cash=10_000, state_path=str(tmp_path / "acct.json"),
        price_overrides={"EURUSD": closes[-1]},
    )
    desk = make_desk(
        tmp_path, broker, journal, df,
        config=DeskConfig(news_blackout=False, min_copy_score=0, zone_min_touches=2),
    )
    action = desk._consider_entry("EURUSD", "test", equity=10_000)
    assert action.action == "skip" and "zone filter" in action.reason


def test_zone_filter_allows_entry_at_a_well_tested_level(tmp_path, journal):
    idx = pd.date_range("2026-07-01", periods=60, freq="5min")
    closes = [100.0] * 60
    highs = [100.02] * 60
    lows = [99.98] * 60
    # the recent-50-bar high (101.0) gets touched three separate times
    # before the final approach -- a seasoned, well-tested level. Gap from
    # baseline (100.02) to the level (101.0) is well outside the default
    # 0.15%-of-level tolerance band (~1.5), so only deliberate touches count.
    for i in (10, 11, 25, 40):
        highs[i] = 101.0
    closes[-1] = 100.95
    highs[-1] = 101.0
    df = pd.DataFrame({"high": highs, "low": lows, "close": closes}, index=idx)

    broker = PaperBroker(
        starting_cash=10_000, state_path=str(tmp_path / "acct.json"),
        price_overrides={"EURUSD": closes[-1]},
    )
    desk = make_desk(
        tmp_path, broker, journal, df,
        config=DeskConfig(news_blackout=False, min_copy_score=0, zone_min_touches=2),
    )
    action = desk._consider_entry("EURUSD", "test", equity=10_000)
    assert "zone filter" not in (action.reason or "")


# ---------------------------------------------------------------------------
# Session 43: per-symbol news blackout (protect a pair around ITS currencies'
# high-impact news, not just USD)
# ---------------------------------------------------------------------------

def test_currencies_for_symbol():
    from bots.organization import currencies_for_symbol

    assert currencies_for_symbol("EURJPY") == {"EUR", "JPY"}
    assert currencies_for_symbol("eurusd") == {"EUR", "USD"}
    assert currencies_for_symbol("GOLD") == set()   # USD-driven, cycle guard covers it
    assert currencies_for_symbol("US30") == set()


def test_per_symbol_news_blocks_only_affected_pairs(tmp_path, journal, price_df):
    from datetime import datetime, timezone

    from bots.learning import QTraderAgent
    from bots.newsguard import NewsGuard

    now_iso = datetime.now(timezone.utc).isoformat()
    jpy_event = [{"country": "JPY", "impact": "High",
                  "title": "BOJ Rate Decision", "date": now_iso}]
    guard = NewsGuard(currencies=("USD",), fetch_fn=lambda: jpy_event)

    agent = QTraderAgent(model_path=str(tmp_path / "q.json"))
    agent.signal = lambda *a, **k: "hold"  # don't let the RL vote veto first
    broker = PaperBroker(
        starting_cash=10_000, state_path=str(tmp_path / "a.json"),
        price_overrides={"EURJPY": 160.0, "AUDUSD": 0.65},
    )
    desk = make_desk(
        tmp_path, broker, journal, price_df,
        config=DeskConfig(news_blackout=True, min_copy_score=0), agent=agent,
    )
    desk._news_guard = guard

    # EURJPY carries the JPY leg -> blocked by the BOJ event
    a = desk._consider_entry("EURJPY", "test", equity=10_000)
    assert a.action == "skip" and "news blackout" in a.reason, a.reason
    # AUDUSD has neither EUR nor JPY -> the BOJ event doesn't touch it
    a = desk._consider_entry("AUDUSD", "test", equity=10_000)
    assert "news blackout" not in (a.reason or "")


# --- synthetic practice / stress harness (session 44) ----------------------

def test_generate_scenarios_is_deterministic_and_balanced():
    from bots.learning.scenarios import REGIMES, generate_scenarios

    a = generate_scenarios(n=39, bars=120, seed=7)
    b = generate_scenarios(n=39, bars=120, seed=7)
    assert len(a) == 39
    # deterministic for a fixed seed
    for (na, da), (nb, db) in zip(a, b):
        assert na == nb
        assert np.allclose(da["close"].values, db["close"].values)
    # a different seed gives different tape
    c = generate_scenarios(n=39, bars=120, seed=8)
    assert not np.allclose(a[0][1]["close"].values, c[0][1]["close"].values)
    # every regime in the catalog is exercised at least once across 39 = 3x13
    assert set(n for n, _ in a) == set(REGIMES)


def test_scenario_frames_are_valid_intraday_ohlc():
    from bots.learning.agent import _is_intraday, extract_state
    from bots.learning.scenarios import generate_scenarios

    for _name, df in generate_scenarios(n=13, bars=150, seed=3):
        assert list(df.columns) == ["open", "high", "low", "close", "volume"]
        # high/low actually bound the bar
        assert (df["high"] >= df[["open", "close"]].max(axis=1) - 1e-9).all()
        assert (df["low"] <= df[["open", "close"]].min(axis=1) + 1e-9).all()
        assert (df["close"] > 0).all()
        # 1-minute spacing must trip the intraday feature path the desk uses live
        assert _is_intraday(df)
        state = extract_state(df, len(df) - 1, holding=False)
        assert "vwap-" in state  # intraday state actually engaged


def test_run_practice_reports_per_regime_without_touching_journal(tmp_path):
    from bots.learning.scenarios import run_practice

    journal_before = tmp_path / "journal.json"
    r = run_practice(n_scenarios=26, bars=140, episodes_per=1, seed=5)
    assert r["scenarios"] == 26
    # a per-regime breakdown is the whole point
    assert r["by_regime"]
    assert r["worst_regime"] in r["by_regime"]
    assert r["best_regime"] in r["by_regime"]
    for stats in r["by_regime"].values():
        assert stats["scenarios"] >= 1
        assert 0.0 <= stats["win_rate"] <= 1.0
    # practising must never have written a journal or account file
    assert not journal_before.exists()


def test_run_practice_hardens_a_supplied_agent_in_place():
    from bots.learning.scenarios import run_practice
    from bots.learning import QTraderAgent

    agent = QTraderAgent(model_path="/dev/null")
    assert agent.trained_episodes == 0
    assert agent.q == {}
    r = run_practice(n_scenarios=13, bars=140, episodes_per=2, agent=agent, seed=2)
    # the agent actually learned: episodes counted up and Q-states were created
    assert agent.trained_episodes > 0
    assert len(agent.q) > 0
    assert r["trained_episodes"] == agent.trained_episodes


# --- news guard fail-closed on an unverifiable feed (session 45) -----------

def test_news_guard_data_fresh_with_live_source():
    from bots.newsguard import NewsGuard

    guard = NewsGuard(currencies=("USD",), fetch_fn=lambda: [])
    # an injected/live source counts as verified-fresh
    assert guard.is_data_fresh() is True


def test_news_guard_not_fresh_when_feed_down_and_no_cache(tmp_path, monkeypatch):
    import bots.newsguard as ng

    monkeypatch.setenv("BOT_DATA_DIR", str(tmp_path))  # empty dir -> no cache file

    def boom(*a, **k):
        raise RuntimeError("network down")

    monkeypatch.setattr(ng.requests, "get", boom)
    guard = ng.NewsGuard(currencies=("USD",))
    blocked, _ = guard.blackout()
    # with nothing to check, the raw blackout can't block -- and freshness is False,
    # which is exactly what the funded fail-closed gate keys off of
    assert blocked is False
    assert guard.is_data_fresh() is False


class _BlindGuard:
    """A news guard whose feed is unreachable: it reports 'no news' but admits
    it can't verify that."""

    def blackout(self, now=None, currencies=None):
        return (False, "no high-impact news in window")

    def is_data_fresh(self):
        return False


def test_funded_news_fail_closed_blocks_when_feed_unverifiable(tmp_path, journal, price_df):
    broker = PaperBroker(
        starting_cash=10_000, state_path=str(tmp_path / "a.json"),
        price_overrides={"EURUSD": 1.10},
    )
    desk = make_desk(
        tmp_path, broker, journal, price_df,
        config=DeskConfig(news_blackout=True, news_fail_closed=True, min_copy_score=0),
    )
    desk._news_guard = _BlindGuard()
    report = desk.run_once(symbols=["EURUSD"])
    assert any("fail-closed" in n for n in report.notes), report.notes


def test_paper_news_trades_through_when_feed_unverifiable(tmp_path, journal, price_df):
    # non-funded default (news_fail_closed=False): a down feed must NOT halt the
    # desk -- paper mode keeps its existing fail-open convenience.
    broker = PaperBroker(
        starting_cash=10_000, state_path=str(tmp_path / "a.json"),
        price_overrides={"EURUSD": 1.10},
    )
    desk = make_desk(
        tmp_path, broker, journal, price_df,
        config=DeskConfig(news_blackout=True, news_fail_closed=False, min_copy_score=0),
    )
    desk._news_guard = _BlindGuard()
    report = desk.run_once(symbols=["EURUSD"])
    assert not any("fail-closed" in n for n in report.notes), report.notes


# --- challenge target lock: bank a prop-firm pass instead of trading past it (session 46) --

def test_challenge_target_guard_locks_once_target_hit(tmp_path):
    from bots.risk import ChallengeTargetGuard

    guard = ChallengeTargetGuard(target_pct=0.10, state_path=str(tmp_path / "ct.json"))
    locked, msg = guard.check(5_000.0)  # baseline set here
    assert locked is False and "challenge progress" in msg
    locked, msg = guard.check(5_400.0)  # +8%, not there yet
    assert locked is False
    locked, msg = guard.check(5_500.0)  # +10% exactly -> hit
    assert locked is True and "TARGET HIT" in msg
    # stays locked even if equity dips back down afterward
    locked, msg = guard.check(5_100.0)
    assert locked is True and "LOCKED" in msg


def test_challenge_target_guard_off_when_target_zero(tmp_path):
    from bots.risk import ChallengeTargetGuard

    guard = ChallengeTargetGuard(target_pct=0.0, state_path=str(tmp_path / "ct.json"))
    locked, _ = guard.check(1_000_000.0)  # absurd gain, but target=0 never fires
    assert locked is False


def test_desk_stops_new_entries_once_challenge_target_hit(tmp_path, journal, price_df):
    from bots.risk import ChallengeTargetGuard

    broker = PaperBroker(
        starting_cash=10_000, state_path=str(tmp_path / "a.json"),
        price_overrides={"EURUSD": 1.10},
    )
    guard = ChallengeTargetGuard(target_pct=0.10, state_path=str(tmp_path / "ct.json"))
    guard.check(10_000.0)  # set baseline
    guard.check(11_500.0)  # +15% -> trips and locks
    desk = TradingDesk(
        broker=broker, journal=journal,
        agent=QTraderAgent(model_path=str(tmp_path / "q.json")),
        config=DeskConfig(news_blackout=False, min_copy_score=0, challenge_target_pct=0.10),
        history_fn=lambda _s: price_df,
        guard=DrawdownGuard(state_path=str(tmp_path / "day_state.json")),
        manual_signals_path=str(tmp_path / "manual_signals.json"),
        challenge_target_guard=guard,
    )
    report = desk.run_once(symbols=["EURUSD"])
    assert any("LOCKED" in n or "TARGET HIT" in n for n in report.notes), report.notes
    assert not report.actions  # no new entries taken


def test_clarity_one_step_challenge_config_matches_screenshotted_rules():
    from bots.organization import clarity_one_step_challenge_config

    challenge = clarity_one_step_challenge_config(funded=False)
    assert challenge.max_daily_loss_pct == 0.04
    assert challenge.max_total_drawdown_pct == 0.06
    assert challenge.challenge_target_pct == 0.10
    assert challenge.friday_flatten is True

    live = clarity_one_step_challenge_config(funded=True)
    assert live.max_daily_loss_pct == 0.04
    assert live.max_total_drawdown_pct == 0.10
    assert live.challenge_target_pct == 0.0  # nothing to lock once funded


def test_aquafunded_instant_config_matches_checkout_screenshot_and_tos():
    from bots.organization import aquafunded_instant_config, funded_account_config

    cfg = aquafunded_instant_config()
    assert cfg.max_daily_loss_pct == 0.03
    assert cfg.max_total_drawdown_pct == 0.06
    # Instant Funded skips the challenge entirely -- no target to lock in,
    # unlike Clarity's One-Step (challenge_target_pct == 0.10 pre-funding)
    assert cfg.challenge_target_pct == 0.0
    # the desk's own leverage stays conservative regardless of the
    # broker's 1:50 ceiling -- this preset doesn't touch max_leverage
    assert cfg.max_leverage == funded_account_config().max_leverage
    # overrides still work, same pattern as the Clarity preset
    tighter = aquafunded_instant_config(max_daily_loss_pct=0.02)
    assert tighter.max_daily_loss_pct == 0.02
    # survival-first sizing (session 48 evidence-law change): 0.25%/trade,
    # NOT the funded default 1.5% -- at 1.5% only 4 consecutive stop-outs
    # breach this account's 6% max drawdown, and the measured journal win
    # rate (31%) makes 4+ losing streaks routine. 24 stop-outs to bust at
    # 0.25% buys the nightly self-train runway instead. Evidence cited in
    # the preset's docstring; raising it back needs new journal evidence.
    assert cfg.risk_per_trade_pct == 0.0025
    assert funded_account_config().risk_per_trade_pct == 0.015  # paper desk unchanged


# --- challenge pass-probability Monte Carlo (session 46) --------------------

def test_simulate_attempt_untrained_agent_never_resolves():
    from bots.learning.agent import QTraderAgent
    from bots.learning.challenge_sim import _generate_attempt_tape, simulate_attempt
    import numpy as np

    agent = QTraderAgent(model_path="/dev/null")  # all-zero Q-table -> always "hold"
    df = _generate_attempt_tape(np.random.default_rng(3), 500)
    result = simulate_attempt(agent, df, start_equity=5_000.0, target_pct=0.10,
                              daily_loss_pct=0.04, max_drawdown_pct=0.06)
    assert result.outcome == "undecided"
    assert result.final_gain_pct == 0.0


def test_simulate_attempt_fails_on_a_manufactured_losing_streak():
    from bots.learning.agent import QTraderAgent
    from bots.learning.challenge_sim import simulate_attempt
    from bots.learning.scenarios import _ohlc_from_close
    import numpy as np

    class AlwaysBuySell(QTraderAgent):
        def choose_action(self, state, explore=False):
            return "buy" if "pos-out" in state else "sell"

    rng = np.random.default_rng(1)
    # steep, relentless downtrend spanning several calendar days -> the daily
    # loss halt resets each day, letting losses actually accumulate to the
    # max-drawdown breach instead of freezing for one day and going quiet
    close = 100.0 * np.cumprod(1.0 - np.full(3000, 0.004))
    df = _ohlc_from_close(close, rng, len(close))
    agent = AlwaysBuySell(model_path="/dev/null")
    result = simulate_attempt(agent, df, start_equity=5_000.0, target_pct=0.10,
                              daily_loss_pct=0.04, max_drawdown_pct=0.06,
                              risk_per_trade_pct=0.015, stop_loss_pct=0.015)
    assert result.outcome == "fail"


def test_simulate_attempt_atr_stops_uses_real_volatility_not_fixed_pct():
    # Session 47: with atr_stops=True, risk sizing at entry should come from
    # the tape's own rolling ATR(14), not the fixed stop_loss_pct -- so an
    # AGGRESSIVE fixed stop_loss_pct (say 5%) should size DIFFERENTLY than a
    # tape whose real ATR is much tighter (a few tenths of a percent).
    from bots.learning.agent import QTraderAgent
    from bots.learning.challenge_sim import simulate_attempt
    from bots.learning.scenarios import _ohlc_from_close
    import numpy as np

    class AlwaysBuySell(QTraderAgent):
        def choose_action(self, state, explore=False):
            return "buy" if "pos-out" in state else "sell"

    rng = np.random.default_rng(4)
    # very low-volatility tape: real ATR should clamp to the 0.3% floor,
    # far tighter than the 5% fixed stop passed in
    close = 100.0 * np.cumprod(1.0 + rng.normal(0.0, 0.0005, 2000))
    df = _ohlc_from_close(close, rng, len(close))
    agent = AlwaysBuySell(model_path="/dev/null")

    fixed = simulate_attempt(agent, df.copy(), start_equity=5_000.0, target_pct=0.10,
                             daily_loss_pct=0.04, max_drawdown_pct=0.06,
                             risk_per_trade_pct=0.015, stop_loss_pct=0.05,
                             atr_stops=False)
    atr = simulate_attempt(agent, df.copy(), start_equity=5_000.0, target_pct=0.10,
                           daily_loss_pct=0.04, max_drawdown_pct=0.06,
                           risk_per_trade_pct=0.015, stop_loss_pct=0.05,
                           atr_stops=True)
    # same tape, same trade actions -> different risk scaling means
    # different final gains (ATR-tight sizing amplifies the same % moves
    # far more than a loose fixed 5% stop would)
    assert fixed.final_gain_pct != pytest.approx(atr.final_gain_pct, abs=1e-9)


def test_run_challenge_monte_carlo_reports_consistent_rates():
    from bots.learning.agent import QTraderAgent
    from bots.learning.challenge_sim import run_challenge_monte_carlo

    agent = QTraderAgent(model_path="/dev/null")
    agent.q = {}  # untrained -> every attempt is undecided, deterministic to check plumbing
    r = run_challenge_monte_carlo(n_attempts=15, bars_per_attempt=400, agent=agent, seed=9)
    assert r["attempts"] == 15
    total = r["pass_rate"] + r["fail_rate"] + r["undecided_rate"]
    assert abs(total - 1.0) < 1e-9
    assert r["undecided_rate"] == 1.0  # untrained agent trades nothing


# --- real-data block-bootstrap validation (session 46 continued) -----------

def _fake_real_pool(rng, n=2000, start=100.0):
    """A small synthetic stand-in for a real OHLCV pool, so these tests
    don't depend on network access to Yahoo Finance."""
    from bots.learning.scenarios import _ohlc_from_close
    rets = rng.normal(0.0001, 0.001, n)
    close = start * np.cumprod(1.0 + rets)
    return _ohlc_from_close(close, rng, n)


def test_block_bootstrap_tape_is_continuous_and_right_length():
    from bots.learning.challenge_sim_real import _block_bootstrap_tape
    import numpy as np

    rng = np.random.default_rng(1)
    pools = {"FAKE1": _fake_real_pool(np.random.default_rng(2)),
             "FAKE2": _fake_real_pool(np.random.default_rng(3), start=50.0)}
    df = _block_bootstrap_tape(pools, rng, bars=1500, block_size=100)
    assert len(df) == 1500
    assert (df["close"] > 0).all()
    # no seam should produce an absurd jump (continuity-adjusted)
    rel_jumps = (df["close"].diff().abs() / df["close"].shift()).dropna()
    assert rel_jumps.max() < 0.2


def test_block_bootstrap_deterministic_for_same_seed():
    from bots.learning.challenge_sim_real import _block_bootstrap_tape
    import numpy as np

    pools = {"FAKE1": _fake_real_pool(np.random.default_rng(2))}
    df1 = _block_bootstrap_tape(pools, np.random.default_rng(5), bars=800, block_size=80)
    df2 = _block_bootstrap_tape(pools, np.random.default_rng(5), bars=800, block_size=80)
    assert np.allclose(df1["close"].values, df2["close"].values)


def test_run_real_data_monte_carlo_uses_supplied_pools_not_network():
    from bots.learning.agent import QTraderAgent
    from bots.learning.challenge_sim_real import run_real_data_monte_carlo
    import numpy as np

    pools = {"FAKE1": _fake_real_pool(np.random.default_rng(2)),
             "FAKE2": _fake_real_pool(np.random.default_rng(3), start=50.0)}
    agent = QTraderAgent(model_path="/dev/null")
    r = run_real_data_monte_carlo(
        n_attempts=6, bars_per_attempt=600, block_size=80,
        agent=agent, pools=pools, seed=1,
    )
    assert r["attempts"] == 6
    assert set(r["symbols_used"]) == {"FAKE1", "FAKE2"}
    total = r["pass_rate"] + r["fail_rate"] + r["undecided_rate"]
    assert abs(total - 1.0) < 1e-9


def test_run_real_data_monte_carlo_raises_without_any_pool():
    from bots.learning.challenge_sim_real import run_real_data_monte_carlo

    try:
        run_real_data_monte_carlo(n_attempts=1, pools={})
        assert False, "expected RuntimeError for empty pools"
    except RuntimeError:
        pass


def test_default_pairs_matches_live_funded_watchlist():
    # Session 47: the challenge-odds estimate should cover the SAME symbols
    # the live --funded desk actually trades, not just 5 FX majors.
    from bots.learning.challenge_sim_real import DEFAULT_PAIRS

    live_watchlist = {
        "EURUSD", "GBPUSD", "USDJPY", "AUDUSD", "NZDUSD", "USDCHF", "USDCAD",
        "EURJPY", "GBPJPY", "AUDJPY", "EURGBP", "EURCHF",
        "US30", "NAS100", "US500", "US2000", "GOLD", "SILVER", "OIL",
    }
    assert set(DEFAULT_PAIRS) == live_watchlist
