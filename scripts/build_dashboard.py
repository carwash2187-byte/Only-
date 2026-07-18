"""Generate a static HTML snapshot of the paper trading ledger.

Reads paper_state/*.json (committed cloud paper account), fetches live
prices for open positions, and writes a self-contained HTML file with no
login and no external requests at view time -- safe to publish as a
read-only status page.

Usage: BOT_DATA_DIR=paper_state python scripts/build_dashboard.py [out.html]
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone

import os

from bots import marketdata
from bots.journal import risk_of_ruin
from bots.paths import data_path

OUT_PATH = sys.argv[1] if len(sys.argv) > 1 else "/tmp/dashboard.html"
VERDICT_TARGET = 25


def load(name: str) -> dict:
    with open(data_path(name), "r", encoding="utf-8") as fh:
        return json.load(fh)


def performance_metrics(closed_trades: list) -> dict:
    """Profit factor, expectancy, max drawdown -- see bots/journal.py's
    TradeJournal.performance_metrics for why these beat win rate alone."""
    if not closed_trades:
        return {}
    ordered = sorted(closed_trades, key=lambda t: t.get("exit_time") or "")
    pnls = [t["pnl"] or 0.0 for t in ordered]
    gross_profit = sum(p for p in pnls if p > 0)
    gross_loss = -sum(p for p in pnls if p < 0)
    cumulative = peak = max_dd = 0.0
    for p in pnls:
        cumulative += p
        peak = max(peak, cumulative)
        max_dd = max(max_dd, peak - cumulative)
    wins = sum(1 for p in pnls if p > 0)
    win_rate = wins / len(pnls)
    return {
        "profit_factor": gross_profit / gross_loss if gross_loss > 0 else float("inf"),
        "expectancy": sum(pnls) / len(pnls),
        "max_drawdown": max_dd,
        "win_rate": win_rate,
        # risk_per_trade_pct=0.015 matches funded_account_config's current
        # default; this is a rough estimate (see risk_of_ruin docstring),
        # not a precise number, but a real one computed from actual trades.
        "risk_of_ruin": risk_of_ruin(win_rate, 0.015) if len(pnls) >= 5 else None,
    }


def equity_curve_svg(closed_trades: list, start_equity: float, current_equity: float,
                     width: int = 640, height: int = 160) -> str:
    """Line chart of cumulative account equity over time, built from closed
    trades in order, ending at the live current equity (including whatever
    is still open right now)."""
    ordered = sorted(closed_trades, key=lambda t: t.get("exit_time") or "")
    points = [start_equity]
    labels = ["start"]
    running = start_equity
    for t in ordered:
        running += t["pnl"] or 0.0
        points.append(running)
        labels.append((t.get("exit_time") or "")[:16].replace("T", " "))
    points.append(current_equity)
    labels.append("now")

    lo, hi = min(points), max(points)
    pad = (hi - lo) * 0.1 or max(hi * 0.02, 1.0)
    lo, hi = lo - pad, hi + pad
    margin = 8
    n = len(points)

    def xy(i: int, v: float):
        x = margin + (width - 2 * margin) * (i / max(n - 1, 1))
        y = margin + (height - 2 * margin) * (1 - (v - lo) / max(hi - lo, 1e-9))
        return x, y

    coords = [xy(i, v) for i, v in enumerate(points)]
    line = " ".join(f"{x:.1f},{y:.1f}" for x, y in coords)
    area = f"{margin},{height - margin} " + line + f" {width - margin},{height - margin}"
    trend = "gain" if points[-1] >= points[0] else "loss"
    last_x, last_y = coords[-1]
    baseline_y = xy(0, start_equity)[1]

    return f"""
    <svg viewBox="0 0 {width} {height}" class="equity-chart chart-{trend}"
         role="img" aria-label="Account equity over time, {fmt_pct((current_equity/start_equity-1)*100 if start_equity else 0)} since start">
      <line x1="{margin}" y1="{baseline_y:.1f}" x2="{width - margin}" y2="{baseline_y:.1f}"
            class="chart-baseline" />
      <polygon points="{area}" class="chart-area" />
      <polyline points="{line}" class="chart-line" />
      <circle cx="{last_x:.1f}" cy="{last_y:.1f}" r="3.5" class="chart-dot" />
    </svg>"""


def load_account():
    """(cash, positions, broker_label, day_state_filename). Prefers the live
    Alpaca paper account when credentials are configured; falls back to the
    local simulated paper account otherwise."""
    if os.environ.get("ALPACA_API_KEY_ID"):
        from bots.brokers.alpaca import AlpacaBroker

        broker = AlpacaBroker()
        return broker.cash(), broker.positions(), "Alpaca paper account", f"day_state_{broker.name}.json"
    acct = load("paper_account.json")
    return acct["cash"], acct["positions"], "Local simulated account", "day_state_paper.json"


def fmt_money(x: float) -> str:
    sign = "-" if x < 0 else ""
    return f"{sign}${abs(x):,.2f}"


def fmt_pct(x: float) -> str:
    return f"{'+' if x >= 0 else ''}{x:.2f}%"


def build() -> str:
    cash, positions, broker_label, day_state_file = load_account()
    journal = load("trade_journal.json")
    try:
        day = load(day_state_file)
    except FileNotFoundError:
        # No baseline recorded yet (first run of the day) -- use current
        # equity as the reference point so the delta starts at zero.
        current_value = cash
        for sym, qty in positions.items():
            try:
                current_value += qty * marketdata.get_price(sym)
            except Exception:
                pass
        day = {"start_equity": current_value}

    open_trades = {t["symbol"]: t for t in journal["trades"] if t["exit_price"] is None}
    closed_trades = [t for t in journal["trades"] if t["exit_price"] is not None]

    rows = []
    positions_value = 0.0
    for symbol, qty in positions.items():
        try:
            price = marketdata.get_price(symbol)
        except Exception:
            price = None
        entry = open_trades.get(symbol, {}).get("entry_price")
        notes = open_trades.get(symbol, {}).get("notes", "")
        sources = sorted({
            part.split(" top-")[0].strip()
            for part in notes.split(";")
            if "top-" in part or "Congress" in part
        })
        if price is not None:
            positions_value += qty * price
        rows.append({
            "symbol": symbol,
            "qty": qty,
            "entry": entry,
            "price": price,
            "sources": sources,
        })
    rows.sort(key=lambda r: (r["price"] or 0) * r["qty"], reverse=True)

    equity = cash + positions_value
    start_equity = day.get("start_equity", 10000.0)
    change = equity - start_equity
    change_pct = (change / start_equity * 100.0) if start_equity else 0.0

    setup_stats: dict = {}
    for t in closed_trades:
        s = setup_stats.setdefault(t["setup"], {"trades": 0, "pnl": 0.0, "wins": 0})
        s["trades"] += 1
        s["pnl"] += t["pnl"] or 0.0
        if (t["pnl"] or 0) > 0:
            s["wins"] += 1

    generated = datetime.now(timezone.utc).strftime("%b %d, %Y · %H:%M UTC")
    trend_class = "flat" if abs(change) < 0.01 else ("gain" if change > 0 else "loss")

    def position_row(r: dict) -> str:
        entry = r["entry"] or 0.0
        price = r["price"]
        if price is not None and entry:
            udelta = (price - entry) * r["qty"]
            upct = (price / entry - 1.0) * 100.0
            udelta_s = fmt_money(udelta)
            upct_s = fmt_pct(upct)
            cls = "flat" if abs(upct) < 0.01 else ("gain" if upct > 0 else "loss")
        else:
            udelta_s, upct_s, cls = "—", "—", "flat"
        price_s = f"${price:,.2f}" if price is not None else "—"
        badges = "".join(f'<span class="badge">{s}</span>' for s in r["sources"][:3])
        return f"""
        <tr>
          <td class="sym">{r['symbol']}</td>
          <td class="num">{r['qty']:,.4f}</td>
          <td class="num">${entry:,.2f}</td>
          <td class="num">{price_s}</td>
          <td class="num {cls}">{udelta_s}<span class="sub">{upct_s}</span></td>
          <td class="sources">{badges}</td>
        </tr>"""

    position_rows_html = "\n".join(position_row(r) for r in rows) or (
        '<tr><td colspan="6" class="empty">No open positions.</td></tr>'
    )

    def journal_entry(t: dict) -> str:
        date = t["entry_time"][:10]
        note = t["notes"].split(";")[0].strip()
        return f"""
        <li class="entry">
          <div class="entry-head">
            <span class="entry-sym">{t['symbol']}</span>
            <span class="entry-tag">OPEN</span>
            <span class="entry-date">{date}</span>
          </div>
          <p class="entry-note">{note}</p>
        </li>"""

    recent = sorted(journal["trades"], key=lambda t: t["entry_time"], reverse=True)[:8]
    journal_html = "\n".join(journal_entry(t) for t in recent) or (
        '<li class="empty">No trades recorded yet.</li>'
    )

    if setup_stats:
        lesson_rows = "\n".join(
            f'<tr><td>{setup}</td><td class="num">{s["trades"]}</td>'
            f'<td class="num">{s["wins"]/s["trades"]:.0%}</td>'
            f'<td class="num {"gain" if s["pnl"]>=0 else "loss"}">{fmt_money(s["pnl"])}</td></tr>'
            for setup, s in sorted(setup_stats.items(), key=lambda kv: kv[1]["pnl"])
        )
        lessons_html = f"""
        <table class="ledger">
          <thead><tr><th>Setup</th><th>Trades</th><th>Win rate</th><th>Total P/L</th></tr></thead>
          <tbody>{lesson_rows}</tbody>
        </table>"""
    else:
        lessons_html = (
            '<p class="empty">Not enough closed trades yet for the desk to have '
            "learned anything. A setup needs 5+ closed trades before it can be "
            "judged, and none have closed so far — every position below is still open.</p>"
        )

    verdict_count = len(closed_trades)
    verdict_pct = min(100, round(verdict_count / VERDICT_TARGET * 100))

    chart_svg = equity_curve_svg(closed_trades, start_equity, equity)

    metrics = performance_metrics(closed_trades)
    if metrics:
        pf = metrics["profit_factor"]
        pf_s = "∞" if pf == float("inf") else f"{pf:.2f}"
        metrics_html = (
            f'<span class="chip">PF {pf_s}</span> '
            f'<span class="chip">Win rate {metrics["win_rate"]:.0%}</span> '
            f'<span class="chip">Expectancy {fmt_money(metrics["expectancy"])}/trade</span> '
            f'<span class="chip">Max DD {fmt_money(metrics["max_drawdown"])}</span>'
        )
        if metrics["risk_of_ruin"] is not None:
            metrics_html += (
                f' <span class="chip">Risk of ruin (est.) {metrics["risk_of_ruin"]:.1%}</span>'
            )
    else:
        metrics_html = '<span class="chip">not enough closed trades yet</span>'

    # Payout radar: same gates the desk loop checks (session 39) -- the
    # bot can't press the withdraw button, but it knows when you could.
    try:
        from bots.journal import TradeJournal

        payout = TradeJournal().payout_readiness()
        if payout["eligible"]:
            payout_html = (
                f'<p class="progress-note"><strong>READY TO CASH OUT:</strong> '
                f'${payout["profit"]:.2f} realized across {payout["trading_days"]} '
                "trading days, consistency OK — request the payout from the "
                "firm's dashboard (paper account: this proves the pipeline).</p>"
            )
        else:
            blockers = "".join(f"<li>{b}</li>" for b in payout["blockers"])
            payout_html = (
                f'<p class="progress-note">Realized profit ${payout["profit"]:.2f} '
                f'over {payout["trading_days"]} trading days '
                f'({payout["days_since_first_trade"]} days since first trade). '
                f"Not withdrawable yet:</p><ul>{blockers}</ul>"
            )
    except Exception as exc:
        payout_html = f'<p class="progress-note">payout radar unavailable ({exc})</p>'

    # Live activity: tail the autopilot's own cycle log so the dashboard
    # shows what the desk decided in its last few minutes, verbatim.
    import glob as _glob
    import html as _html

    live_log = "(no live log found -- is the autopilot running?)"
    candidates = ["/tmp/autopilot_paper.log"] + _glob.glob(
        "/tmp/claude-*/**/autopilot.log", recursive=True
    )
    for log_path in candidates:
        try:
            with open(log_path, "r", encoding="utf-8", errors="replace") as fh:
                lines = fh.readlines()[-35:]
            if lines:
                live_log = _html.escape("".join(lines))
                break
        except OSError:
            continue

    return TEMPLATE.format(
        generated=generated,
        broker_label=broker_label,
        equity=fmt_money(equity),
        change_s=fmt_money(change),
        change_pct=fmt_pct(change_pct),
        trend_class=trend_class,
        cash=fmt_money(cash),
        positions_value=fmt_money(positions_value),
        start_equity=fmt_money(start_equity),
        open_count=len(positions),
        closed_count=len(closed_trades),
        position_rows=position_rows_html,
        journal_entries=journal_html,
        lessons=lessons_html,
        verdict_count=verdict_count,
        verdict_target=VERDICT_TARGET,
        verdict_pct=verdict_pct,
        metrics_html=metrics_html,
        chart_svg=chart_svg,
        payout_html=payout_html,
        live_log=live_log,
    )


TEMPLATE = r"""<title>Trading Desk — Paper Ledger</title>
<style>
:root {{
  --bg: #eef2ea;
  --bg-alt: #e1e9dc;
  --card: #f6f8f3;
  --ink: #202b23;
  --ink-soft: #4b5a4e;
  --accent: #2b3a6b;
  --accent-soft: #5165a3;
  --gain: #1e7a46;
  --loss: #a23b2e;
  --line: #c3cdbc;
  --radius: 3px;
}}
@media (prefers-color-scheme: dark) {{
  :root {{
    --bg: #12161a;
    --bg-alt: #1b2127;
    --card: #171c21;
    --ink: #dde3da;
    --ink-soft: #93a093;
    --accent: #8fa3e0;
    --accent-soft: #6d81bd;
    --gain: #52c284;
    --loss: #e2695a;
    --line: #2a323a;
  }}
}}
:root[data-theme="dark"] {{
  --bg: #12161a;
  --bg-alt: #1b2127;
  --card: #171c21;
  --ink: #dde3da;
  --ink-soft: #93a099;
  --accent: #8fa3e0;
  --accent-soft: #6d81bd;
  --gain: #52c284;
  --loss: #e2695a;
  --line: #2a323a;
}}
:root[data-theme="light"] {{
  --bg: #eef2ea;
  --bg-alt: #e1e9dc;
  --card: #f6f8f3;
  --ink: #202b23;
  --ink-soft: #4b5a4e;
  --accent: #2b3a6b;
  --accent-soft: #5165a3;
  --gain: #1e7a46;
  --loss: #a23b2e;
  --line: #c3cdbc;
}}

* {{ box-sizing: border-box; }}
body {{
  background: var(--bg);
  color: var(--ink);
  font-family: ui-monospace, "SF Mono", "Cascadia Mono", "Roboto Mono", Menlo, monospace;
  margin: 0;
  padding: 2.5rem 1.25rem 5rem;
  line-height: 1.5;
  background-image: repeating-linear-gradient(
    var(--bg), var(--bg) 2.15rem, var(--bg-alt) 2.15rem, var(--bg-alt) 2.3rem
  );
  background-attachment: local;
}}
main {{ max-width: 46rem; margin: 0 auto; }}

.serif {{
  font-family: ui-serif, "Iowan Old Style", "Palatino Linotype", Georgia, "Times New Roman", serif;
}}

header.masthead {{
  display: flex;
  justify-content: space-between;
  align-items: flex-end;
  gap: 1rem;
  border-bottom: 2px solid var(--ink);
  padding-bottom: 1rem;
  margin-bottom: 1.75rem;
  flex-wrap: wrap;
}}
.eyebrow {{
  text-transform: uppercase;
  letter-spacing: 0.14em;
  font-size: 0.72rem;
  color: var(--ink-soft);
}}
h1 {{
  font-family: inherit;
  font-size: clamp(1.5rem, 4vw, 2.05rem);
  margin: 0.2rem 0 0;
  letter-spacing: 0.01em;
  text-wrap: balance;
}}
.updated {{
  font-size: 0.75rem;
  color: var(--ink-soft);
  text-align: right;
}}
.watermark {{
  display: inline-block;
  margin-top: 0.35rem;
  font-size: 0.68rem;
  letter-spacing: 0.1em;
  color: var(--accent-soft);
  border: 1px solid var(--accent-soft);
  border-radius: var(--radius);
  padding: 0.15rem 0.5rem;
}}

.hero {{
  background: var(--card);
  border: 1px solid var(--line);
  border-radius: var(--radius);
  padding: 1.6rem 1.6rem 1.4rem;
  margin-bottom: 1.5rem;
}}
.hero-label {{
  text-transform: uppercase;
  letter-spacing: 0.12em;
  font-size: 0.72rem;
  color: var(--ink-soft);
}}
.hero-value {{
  font-size: clamp(2.1rem, 7vw, 3rem);
  font-weight: 600;
  font-variant-numeric: tabular-nums;
  margin: 0.15rem 0 0.2rem;
}}
.hero-delta {{
  font-size: 1rem;
  font-variant-numeric: tabular-nums;
}}
.hero-delta.gain, .num.gain {{ color: var(--gain); }}
.hero-delta.loss, .num.loss {{ color: var(--loss); }}
.hero-delta.flat, .num.flat {{ color: var(--ink-soft); }}

.equity-chart {{
  width: 100%;
  height: auto;
  margin-top: 0.9rem;
  overflow: visible;
}}
.chart-baseline {{ stroke: var(--line); stroke-width: 1; stroke-dasharray: 3 3; }}
.chart-area {{ fill: var(--accent); opacity: 0.08; stroke: none; }}
.chart-line {{ fill: none; stroke: var(--accent); stroke-width: 2; stroke-linejoin: round; stroke-linecap: round; }}
.chart-dot {{ fill: var(--accent); }}
.chart-gain .chart-area {{ fill: var(--gain); }}
.chart-gain .chart-line {{ stroke: var(--gain); }}
.chart-gain .chart-dot {{ fill: var(--gain); }}
.chart-loss .chart-area {{ fill: var(--loss); }}
.chart-loss .chart-line {{ stroke: var(--loss); }}
.chart-loss .chart-dot {{ fill: var(--loss); }}
@media (prefers-reduced-motion: no-preference) {{
  .chart-line {{ stroke-dasharray: 1000; stroke-dashoffset: 1000; animation: draw-line 1.1s ease-out forwards; }}
}}
@keyframes draw-line {{ to {{ stroke-dashoffset: 0; }} }}

.stat-grid {{
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(9.5rem, 1fr));
  gap: 1px;
  background: var(--line);
  border: 1px solid var(--line);
  border-radius: var(--radius);
  overflow: hidden;
  margin-bottom: 1.5rem;
}}
.stat {{
  background: var(--card);
  padding: 0.85rem 1rem;
}}
.stat-label {{
  text-transform: uppercase;
  letter-spacing: 0.1em;
  font-size: 0.65rem;
  color: var(--ink-soft);
}}
.stat-value {{
  font-size: 1.05rem;
  font-variant-numeric: tabular-nums;
  margin-top: 0.2rem;
}}
.chip {{
  display: inline-block;
  font-size: 0.68rem;
  letter-spacing: 0.06em;
  padding: 0.1rem 0.4rem;
  border-radius: var(--radius);
  border: 1px solid var(--gain);
  color: var(--gain);
}}

section {{ margin-bottom: 1.85rem; }}
h2 {{
  font-family: inherit;
  font-size: 0.78rem;
  text-transform: uppercase;
  letter-spacing: 0.12em;
  color: var(--ink-soft);
  border-bottom: 1px solid var(--line);
  padding-bottom: 0.4rem;
  margin: 0 0 0.75rem;
}}

.progress-row {{
  display: flex;
  align-items: center;
  gap: 0.75rem;
  margin-bottom: 0.5rem;
}}
.progress-track {{
  flex: 1;
  height: 0.5rem;
  background: var(--bg-alt);
  border: 1px solid var(--line);
  border-radius: var(--radius);
  overflow: hidden;
}}
.progress-fill {{
  height: 100%;
  background: var(--accent);
}}
.progress-count {{
  font-variant-numeric: tabular-nums;
  font-size: 0.85rem;
  white-space: nowrap;
}}
.progress-note {{
  font-family: ui-serif, Georgia, serif;
  font-size: 0.85rem;
  color: var(--ink-soft);
  margin: 0;
}}

.table-wrap {{ overflow-x: auto; }}
table.ledger {{
  width: 100%;
  border-collapse: collapse;
  font-size: 0.85rem;
}}
table.ledger th {{
  text-align: left;
  text-transform: uppercase;
  letter-spacing: 0.06em;
  font-size: 0.65rem;
  color: var(--ink-soft);
  border-bottom: 1px solid var(--line);
  padding: 0.4rem 0.6rem;
}}
table.ledger td {{
  padding: 0.55rem 0.6rem;
  border-bottom: 1px solid var(--line);
  vertical-align: top;
}}
table.ledger tr:nth-child(even) td {{ background: var(--bg-alt); }}
td.num {{ font-variant-numeric: tabular-nums; text-align: right; }}
td.sym {{ font-weight: 600; }}
.sub {{ display: block; font-size: 0.7rem; opacity: 0.8; }}
.sources {{ display: flex; flex-wrap: wrap; gap: 0.3rem; }}
.badge {{
  font-size: 0.62rem;
  letter-spacing: 0.03em;
  padding: 0.1rem 0.35rem;
  border-radius: var(--radius);
  background: var(--bg-alt);
  color: var(--ink-soft);
  border: 1px solid var(--line);
  white-space: nowrap;
}}
.empty {{
  font-family: ui-serif, Georgia, serif;
  color: var(--ink-soft);
  font-size: 0.88rem;
  padding: 0.5rem 0;
}}

ul.journal {{ list-style: none; margin: 0; padding: 0; }}
li.entry {{
  border-left: 2px solid var(--line);
  padding: 0.15rem 0 0.9rem 0.9rem;
  margin-bottom: 0.4rem;
}}
.entry-head {{
  display: flex;
  align-items: baseline;
  gap: 0.5rem;
  font-size: 0.78rem;
}}
.entry-sym {{ font-weight: 600; }}
.entry-tag {{
  font-size: 0.62rem;
  letter-spacing: 0.06em;
  color: var(--accent-soft);
  border: 1px solid var(--accent-soft);
  border-radius: var(--radius);
  padding: 0 0.3rem;
}}
.entry-date {{ color: var(--ink-soft); margin-left: auto; }}
.entry-note {{
  font-family: ui-serif, "Iowan Old Style", Georgia, serif;
  font-size: 0.92rem;
  color: var(--ink);
  margin: 0.35rem 0 0;
  max-width: 42rem;
}}

footer {{
  border-top: 2px solid var(--ink);
  padding-top: 1rem;
  font-size: 0.75rem;
  color: var(--ink-soft);
}}
footer p {{ margin: 0.3rem 0; font-family: ui-serif, Georgia, serif; }}
footer strong {{ color: var(--ink); }}

@media (prefers-reduced-motion: no-preference) {{
  .hero, .stat, section {{ animation: rise 0.4s ease-out backwards; }}
  .stat:nth-child(2) {{ animation-delay: 0.03s; }}
  .stat:nth-child(3) {{ animation-delay: 0.06s; }}
  .stat:nth-child(4) {{ animation-delay: 0.09s; }}
}}
@keyframes rise {{
  from {{ opacity: 0; transform: translateY(4px); }}
  to {{ opacity: 1; transform: translateY(0); }}
}}
</style>

<main>
  <header class="masthead">
    <div>
      <div class="eyebrow">Only- / Trading Desk</div>
      <h1>Paper Ledger</h1>
      <span class="watermark">{broker_label} · SIMULATED FUNDS · NO REAL MONEY</span>
    </div>
    <div class="updated">Last updated<br>{generated}</div>
  </header>

  <div class="hero">
    <div class="hero-label">Account equity</div>
    <div class="hero-value">{equity}</div>
    <div class="hero-delta {trend_class}">{change_s} ({change_pct}) since trial start</div>
    {chart_svg}
  </div>

  <div class="stat-grid">
    <div class="stat">
      <div class="stat-label">Cash</div>
      <div class="stat-value">{cash}</div>
    </div>
    <div class="stat">
      <div class="stat-label">In positions</div>
      <div class="stat-value">{positions_value}</div>
    </div>
    <div class="stat">
      <div class="stat-label">Day-start baseline</div>
      <div class="stat-value">{start_equity}</div>
    </div>
    <div class="stat">
      <div class="stat-label">Circuit breaker</div>
      <div class="stat-value"><span class="chip">ARMED · 5% max daily loss</span></div>
    </div>
  </div>

  <section>
    <h2>Performance metrics (matter more than win rate alone)</h2>
    <p class="progress-note">{metrics_html}</p>
  </section>

  <section>
    <h2>Verdict progress</h2>
    <div class="progress-row">
      <div class="progress-track"><div class="progress-fill" style="width:{verdict_pct}%"></div></div>
      <div class="progress-count">{verdict_count} / {verdict_target} closed trades</div>
    </div>
    <p class="progress-note">The desk's results only mean something after ~25 closed
    trades. Below that, wins or losses are noise, not skill — this bar fills as
    positions close.</p>
  </section>

  <section>
    <h2>Live desk activity (the bot's last minutes of decisions)</h2>
    <div class="table-wrap"><pre style="font-size:12px;line-height:1.5;white-space:pre-wrap">{live_log}</pre></div>
  </section>

  <section>
    <h2>Payout radar (when profit becomes withdrawable)</h2>
    {payout_html}
  </section>

  <section>
    <h2>Open positions ({open_count})</h2>
    <div class="table-wrap">
      <table class="ledger">
        <thead>
          <tr><th>Symbol</th><th>Qty</th><th>Entry</th><th>Now</th><th>Unrealized</th><th>Copied from</th></tr>
        </thead>
        <tbody>{position_rows}</tbody>
      </table>
    </div>
  </section>

  <section>
    <h2>Recent journal entries</h2>
    <ul class="journal">{journal_entries}</ul>
  </section>

  <section>
    <h2>Lessons learned</h2>
    {lessons}
  </section>

  <footer>
    <p><strong>This is fake money.</strong> The desk trades a simulated paper account
    automatically on weekdays, sized so no single trade risks more than 1% of the
    account and the whole account stops trading for the day after a 5% loss.</p>
    <p>Nothing here is investment advice or a profit guarantee. Real money only
    follows if this record earns it.</p>
  </footer>
</main>
"""


if __name__ == "__main__":
    html = build()
    with open(OUT_PATH, "w", encoding="utf-8") as fh:
        fh.write(html)
    print(f"wrote {OUT_PATH}")
