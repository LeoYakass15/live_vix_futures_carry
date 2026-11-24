"""
Live paper trading of the short VIX book (rules in RULES.md, history in LIVE.md).

Every weekend I download the CBOE settlements (scripts/download_cboe.py, in Google Colab)
into data/cboe/vix_futures_cboe.csv. Then, in this order:

    python live.py week        -> P&L of the week that just ended, chart, block for LIVE.md
    python live.py positions   -> position for Monday (traded at Monday's settlement)
    python live.py chart       -> the empty "live vs backtest" cone, before the first week

and I commit before Sunday midnight, Paris time, when VIX futures reopen.
"""
import sys
import numpy as np
import pandas as pd
import matplotlib.pyplot as plt
import backtest as bt

CAPITAL = 1_000_000                     # paper capital in USD
MULTIPLIER = 1000                       # a VIX future pays 1,000 USD per point
COST_PER_CONTRACT = bt.COST * MULTIPLIER    # 50 USD per contract traded
FIRST_FRIDAY = "2026-11-27"             # first decision, traded on Monday 30 November 2026
POSITIONS = "results/positions_live.csv"
PNL = "results/live_pnl.csv"


def refresh_vix():
    """Latest VIX close from FRED (free). Without internet, keep the old file."""
    try:
        pd.read_csv("https://fred.stlouisfed.org/graph/fredgraph.csv?id=VIXCLS").to_csv("data/vix_fred.csv", index=False)
    except Exception as e:
        print("FRED not reached, I keep data/vix_fred.csv:", e)


def load_positions():
    try:
        return pd.read_csv(POSITIONS, parse_dates=["friday", "hold"])
    except FileNotFoundError:
        return pd.DataFrame()


def week_pnl(px, pos, start, end):
    """USD P&L between two Fridays. Each day I hold the last position traded before that day.
    A position decided on Friday is traded at the settlement of the next trading day (Monday)."""
    pos = pos.copy()
    pos["execution"] = [px.index[px.index > f][0] if (px.index > f).any() else pd.NaT for f in pos["friday"]]
    pnl, cost = 0.0, 0.0
    for d in px.index[(px.index > start) & (px.index <= end)]:
        prev = px.index[px.index < d][-1]
        held = pos[pos["execution"] < d]
        if len(held):
            n, c = held["contracts"].iloc[-1], held["hold"].iloc[-1]
            pnl += n * MULTIPLIER * (px.at[d, c] - px.at[prev, c])
        today = pos[pos["execution"] == d]
        if len(today):                                  # the Monday trade
            new = today.iloc[-1]
            old_n = held["contracts"].iloc[-1] if len(held) else 0
            same = len(held) > 0 and held["hold"].iloc[-1] == new["hold"]
            cost += (abs(new["contracts"] - old_n) if same else abs(new["contracts"]) + abs(old_n)) * COST_PER_CONTRACT
    return pnl - cost, cost


def simulate(px, pos):
    """Rebuilds the whole live track record from the positions file and the CBOE prices."""
    fridays = px.index.to_series().groupby(px.index.to_period("W-FRI")).max()
    fridays = fridays[fridays > pos["friday"].iloc[0]]
    nav, prev, rows = CAPITAL, pos["friday"].iloc[0], []
    for f in fridays:
        usd, cost = week_pnl(px, pos, prev, f)
        rows.append({"friday": f, "pnl_usd": usd, "pnl_pct": usd / nav, "costs_usd": cost, "nav": nav + usd})
        nav, prev = nav + usd, f
    return pd.DataFrame(rows)


def new_positions():
    refresh_vix()
    px, vix = bt.load_futures(), bt.load_vix()
    table = bt.weekly_table(px, vix)
    friday, row = table.index[-1], table.iloc[-1]
    weight = bt.positions(table).iloc[-1] + 0.0            # + 0.0 turns -0.0 into 0.0
    try:
        nav = pd.read_csv(PNL)["nav"].iloc[-1]
    except (FileNotFoundError, IndexError):
        nav = CAPITAL
    contracts = int(round(weight * nav / (row["hold_price"] * MULTIPLIER)))
    contango = row["ux2"] / row["ux1"] - 1
    new = pd.DataFrame([{"friday": friday.date(), "ux1": row["ux1"], "ux2": row["ux2"], "contango": round(contango, 4),
                         "hold": row["hold"].date(), "hold_price": row["hold_price"], "vol": round(row["vol"], 4),
                         "weight": round(weight, 4), "contracts": contracts}])
    if friday >= pd.Timestamp(FIRST_FRIDAY):
        old = load_positions()
        if len(old):
            old = old[old["friday"] != friday]
        book = pd.concat([old, new])
        for col in ["friday", "hold"]:
            book[col] = pd.to_datetime(book[col]).dt.strftime("%Y-%m-%d")
        book.to_csv(POSITIONS, index=False)
    else:
        print(f"(Last Friday in the data is {friday:%d %B %Y}, before the launch: nothing saved.)\n")

    notional = abs(contracts) * row["hold_price"] * MULTIPLIER
    signal = "contango: **short**" if weight < 0 else "backwardation: **cash**"
    print(f"### Position for Monday {friday + pd.offsets.BDay(1):%d %B %Y}\n")
    print("| Friday | UX1 | UX2 | UX2 / UX1 - 1 | Signal |\n|---|---|---|---|---|")
    print(f"| {friday:%d/%m/%Y} | {row['ux1']:.2f} | {row['ux2']:.2f} | {contango:+.1%} | {signal} |\n")
    print(f"Position: **{contracts:+d} contracts** of the VIX future expiring {row['hold']:%d %b %Y} "
          f"(price {row['hold_price']:.2f}), notional {notional:,.0f} USD = {abs(weight):.0%} of the NAV. "
          f"Vol of this future over 20 days: {row['vol']:.0%}.\n")
    print(f"Stress test: if this future doubles on Monday (February 2018), the book loses {notional:,.0f} USD.")


def weekly_report():
    pos = load_positions()
    if pos.empty:
        print("No live position yet."); return
    hist = simulate(bt.load_futures(), pos)
    if hist.empty:
        print("No live week yet."); return
    hist["cumulative"] = hist["nav"] / CAPITAL - 1
    hist.to_csv(PNL, index=False, float_format="%.4f", date_format="%Y-%m-%d")
    last = hist.iloc[-1]
    dd = (hist["nav"] / hist["nav"].cummax() - 1).iloc[-1]
    held = pos[pos["friday"] < last["friday"]].iloc[-1]
    side = "Short" if held["contracts"] < 0 else "Cash"
    print(f"### Week {len(hist)}, ending Friday {last['friday']:%d %B %Y}\n")
    print("| Position held | Contracts | Future | P&L (USD) | Costs (USD) |\n|---|---|---|---|---|")
    print(f"| {side} | {held['contracts']:+d} | expiry {held['hold']:%d %b %Y} | {last['pnl_usd']:+,.0f} |"
          f" {-last['costs_usd']:,.0f} |\n")
    print(f"**Week: {last['pnl_pct']:+.2%} ({last['pnl_usd']:+,.0f} USD). Since launch: "
          f"{last['cumulative']:+.2%}. NAV {last['nav']:,.0f} USD. Drawdown {dd:.1%}.**")
    live_chart(hist)


def live_chart(hist):
    """Live P&L against what the backtest expects (mean, 1 and 2 standard deviations)."""
    bt_pnl = pd.read_csv("results/weekly_pnl.csv", index_col=0)["rule"]
    mu, sigma = bt_pnl.mean(), bt_pnl.std()
    weeks = np.arange(0, max(52, len(hist)) + 1)          # the cone covers the first year
    live = np.r_[0, hist["cumulative"].values] if len(hist) else np.array([0.0])
    plt.figure(figsize=(8, 4))
    for k, a in [(2, 0.12), (1, 0.25)]:
        plt.fill_between(weeks, mu * weeks - k * sigma * np.sqrt(weeks), mu * weeks + k * sigma * np.sqrt(weeks),
                         color=bt.BLUE, alpha=a, lw=0)
    plt.plot(weeks, mu * weeks, color=bt.BLUE, ls="--", label="Backtest expectation (1 and 2 std)")
    plt.plot(np.arange(len(live)), live, color="black", lw=2, marker="o", label="Live")
    plt.axhline(0, color="grey", lw=0.8)
    plt.xlabel("Weeks since launch"); plt.ylabel("Cumulative return"); plt.legend(loc="upper left")
    plt.title("Live vs backtest"); plt.tight_layout(); plt.savefig("results/6_live_vs_backtest.png", dpi=120)
    plt.close()


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "chart":         # empty cone before the first week
        live_chart(pd.DataFrame({"cumulative": []}))
    elif len(sys.argv) > 1 and sys.argv[1] == "positions":
        new_positions()
    else:
        weekly_report()
