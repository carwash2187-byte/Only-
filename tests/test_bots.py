"""Tests for the bots/ stack: journal, RL agent, paper broker, trading desk."""

import numpy as np
import pandas as pd
import pytest

from bots.brokers import PaperBroker, get_broker
from bots.journal import TradeJournal
from bots.learning import QTraderAgent
from bots.organization import DeskConfig, TradingDesk


@pytest.fixture
def price_df():
    rng = np.random.default_rng(42)
    prices = 100 * np.exp(np.cumsum(rng.normal(0.0004, 0.01, 300)))
    return pd.DataFrame({"close": prices})


@pytest.fixture
def journal(tmp_path):
    return TradeJournal(path=str(tmp_path / "journal.json"))


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
    desk = TradingDesk(
        broker=broker,
        journal=journal,
        agent=agent,
        config=DeskConfig(min_copy_score=0),
        history_fn=lambda _s: price_df,
    )
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
        price_overrides={"DEMO": entry_price},
    )
    assert broker.buy("DEMO", 10).ok
    journal.open_trade("DEMO", "long", 10, entry_price, setup="test")
    broker.price_overrides["DEMO"] = crashed

    agent = QTraderAgent(model_path=str(tmp_path / "q.json"))
    desk = TradingDesk(
        broker=broker,
        journal=journal,
        agent=agent,
        config=DeskConfig(min_copy_score=99),  # no new entries
        history_fn=lambda _s: price_df,
    )
    report = desk.run_once(symbols=[])
    sells = [a for a in report.actions if a.action == "sell"]
    assert sells and sells[0].ok
    assert broker.positions() == {}
    closed = [t for t in journal.trades.values() if not t.is_open]
    assert closed and closed[0].pnl == pytest.approx((crashed - entry_price) * 10)
