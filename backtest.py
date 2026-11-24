"""
Short VIX futures when the curve is in contango (the volatility risk premium).

Rule: every Friday, if the 2nd VIX future is above the 1st (contango), I am short the
front VIX future the following week, sized to 10% volatility. Otherwise I stay in cash.
The trade is done at Monday's settlement price, exactly as in the live book.

Run:  python backtest.py
Data: CBOE settlement of every VIX future since 2004 (data/cboe/), VIX index (FRED),
      S&P 500 (Yahoo + FRED), SPVXSTR index (Bloomberg). The data/ folder is not published.
"""
import numpy as np
import pandas as pd
import matplotlib.pyplot as plt
from scipy.stats import norm, skew, kurtosis
import statsmodels.api as sm

# ---------------------------------------------------------------- settings (frozen, see RULES.md)
THRESHOLD = 0.0        # short only if UX2 / UX1 - 1 > THRESHOLD
CURVE = "ux2_ux1"      # how contango is measured: "ux2_ux1", "ux1_vix" or "both"
VOL_WINDOW = 20        # days of daily returns to measure the vol of the future I hold
TARGET_VOL = 0.10      # 10% annual volatility for the book
MAX_NOTIONAL = 1.0     # never short more than 100% of capital in notional
COST = 0.05            # VIX points paid per contract traded (one tick)
START = "2006-01-01"
IN_SAMPLE_END = "2015-12-31"   # 2016-2026 is the out-of-sample period

BLUE, ORANGE, AQUA, GREY = "#2a78d6", "#eb6834", "#1baf7a", "#8a8a85"


# ---------------------------------------------------------------- 1. data
def load_futures():
    """Table of settlement prices: one row per day, one column per contract (its expiry date)."""
    d = pd.read_csv("data/cboe/vix_futures_cboe.csv")
    d["date"] = pd.to_datetime(d["Trade Date"], format="mixed")
    d["expiry"] = pd.to_datetime(d["Expiry"])
    d["price"] = d["Settle"]
    if "Close" in d:                                   # early 2013: CBOE wrote Settle = 0
        d["price"] = d["price"].where(d["price"] > 0, d["Close"])
    d.loc[d["date"] < "2007-03-26", "price"] /= 10     # before March 2007, quoted 10 x VIX
    d = d[d["price"] > 0]

    # the real expiry is the last day the contract traded (holidays move it by a day)
    last_day = d.groupby("expiry")["date"].max()
    real = last_day.where(last_day < d["date"].max(), last_day.index.to_series())
    d["expiry"] = d["expiry"].map(real)

    px = d.pivot_table(index="date", columns="expiry", values="price")
    px = px.ffill(limit=3)                             # a missing day here and there
    for e in px.columns:                               # but nothing after expiry
        px.loc[px.index > e, e] = np.nan
    return px


def load_vix():
    v = pd.read_csv("data/vix_fred.csv", index_col=0, parse_dates=True).iloc[:, 0]
    return pd.to_numeric(v, errors="coerce").dropna()


def load_spx():
    yahoo = pd.read_csv("data/spx_yahoo.csv", index_col=0, parse_dates=True)["spx"]
    fred = pd.read_csv("data/spx_fred.csv", index_col=0, parse_dates=True).iloc[:, 0]
    fred = pd.to_numeric(fred, errors="coerce").dropna()
    return pd.concat([yahoo, fred[fred.index > yahoo.index[-1]]])


def weekly_table(px, vix, vol_window=VOL_WINDOW):
    """One row per Friday: the curve, the contract to hold next week and its recent volatility."""
    days = px.index
    fridays = days.to_series().groupby(days.to_period("W-FRI")).max()    # last trading day of the week
    fridays = fridays[fridays >= pd.Timestamp(START) - pd.Timedelta(days=7)]
    returns = px.pct_change(fill_method=None)
    rows = []
    for s in fridays:
        alive = [e for e in px.columns if e > s and px.at[s, e] > 0]
        if len(alive) < 2:
            continue
        ux1, ux2 = alive[0], alive[1]
        hold = [e for e in alive if e > s + pd.Timedelta(days=10)][0]   # must not expire next week
        later = days[days > s]
        rows.append({
            "friday": s,
            "execution": later[0] if len(later) else s + pd.offsets.BDay(1),   # next trading day
            "ux1": px.at[s, ux1], "ux2": px.at[s, ux2],
            "vix": vix.asof(s),
            "hold": hold, "hold_price": px.at[s, hold],
            "vol": returns[hold].loc[:s].tail(vol_window).std() * np.sqrt(252),
        })
    return pd.DataFrame(rows).set_index("friday")


# ---------------------------------------------------------------- 2. strategy
def positions(table, threshold=THRESHOLD, curve=CURVE, always_short=False):
    """Weight of the book in % of capital (negative = short), decided each Friday."""
    contango_12 = table["ux2"] / table["ux1"] - 1
    contango_spot = table["ux1"] / table["vix"] - 1
    if curve == "ux2_ux1":
        signal = contango_12 > threshold
    elif curve == "ux1_vix":
        signal = contango_spot > threshold
    else:
        signal = (contango_12 > threshold) & (contango_spot > threshold)
    if always_short:
        signal = signal | True
    size = (TARGET_VOL / table["vol"]).clip(upper=MAX_NOTIONAL)
    return -size.where(signal, 0.0)


def daily_pnl(px, table, weight, same_day=False):
    """Daily P&L in % of capital. The Friday decision is traded at Monday's settlement
    (or at Friday's own settlement if same_day=True) and held until the next trade."""
    pnl = pd.Series(0.0, index=px.index)
    trade = pd.Series(table.index if same_day else table["execution"].values, index=table.index)
    prev_w, prev_c = 0.0, None
    for i in range(len(table)):
        e = trade.iloc[i]
        if e not in px.index:                 # next Monday not in the data yet
            break
        e_next = trade.iloc[i + 1] if i + 1 < len(table) else px.index[-1]
        c, w = table["hold"].iloc[i], weight.iloc[i]
        f = px[c].loc[e:e_next].ffill()
        entry = f.iloc[0]
        pnl.loc[f.index[1:]] += w * f.diff().iloc[1:] / entry
        traded = abs(w - prev_w) if c == prev_c else abs(w) + abs(prev_w)   # a roll = 2 trades
        pnl.loc[e] -= traded * COST / entry
        prev_w, prev_c = w, c
    return pnl


def weekly(pnl):
    w = pnl.resample("W-FRI").sum()
    return w.loc[START:]


def run(px, table, **kwargs):
    same_day = kwargs.pop("same_day", False)
    weight = positions(table, **kwargs)
    return weekly(daily_pnl(px, table, weight, same_day)), weight


# ---------------------------------------------------------------- 3. statistics
def stats(pnl):
    n = len(pnl)
    sr = pnl.mean() / pnl.std() * np.sqrt(52)
    nav = (1 + pnl).cumprod()
    return {
        "Return (ann.)": pnl.mean() * 52,
        "Vol (ann.)": pnl.std() * np.sqrt(52),
        "Sharpe": sr,
        "t-stat": sr * np.sqrt(n / 52),          # = mean / (std / sqrt(n))
        "Max drawdown": (nav / nav.cummax() - 1).min(),
        "Worst week": pnl.min(),
        "Skew": skew(pnl),
        "Kurtosis": kurtosis(pnl, fisher=False),
        "Weeks in the market": (pnl != 0).mean(),
        "Weeks": n,
    }


def prob_sharpe(pnl, sr0=0.0, use_moments=True):
    """Probabilistic Sharpe Ratio: probability that the true weekly Sharpe is above sr0.
    With use_moments=False the returns are assumed normal (skew 0, kurtosis 3)."""
    sr = pnl.mean() / pnl.std()
    g3, g4 = (skew(pnl), kurtosis(pnl, fisher=False)) if use_moments else (0.0, 3.0)
    z = (sr - sr0) * np.sqrt(len(pnl) - 1) / np.sqrt(1 - g3 * sr + (g4 - 1) / 4 * sr ** 2)
    return norm.cdf(z)


def lucky_sharpe(trial_sharpes):
    """Bailey and Lopez de Prado (2014): the Sharpe the best of N useless strategies
    would show by chance. trial_sharpes are weekly Sharpes."""
    n, euler = len(trial_sharpes), 0.5772
    return np.std(trial_sharpes, ddof=1) * ((1 - euler) * norm.ppf(1 - 1 / n)
                                            + euler * norm.ppf(1 - 1 / (n * np.e)))


# ---------------------------------------------------------------- 4. main
if __name__ == "__main__":
    px, vix = load_futures(), load_vix()
    table = weekly_table(px, vix)

    # every version I try goes into the trials log, good or bad (27 trials, declared before running)
    trials = []
    tables = {w: weekly_table(px, vix, w) for w in [10, 20, 60]}
    for threshold in [0.0, 0.02, 0.05]:
        for curve in ["ux2_ux1", "ux1_vix", "both"]:
            for window in [10, 20, 60]:
                p, _ = run(px, tables[window], threshold=threshold, curve=curve)
                p = p.loc[:IN_SAMPLE_END]
                s = stats(p)
                trials.append({"threshold": threshold, "curve": curve, "vol_window": window,
                               "sharpe_in_sample": round(s["Sharpe"], 3), "t_stat": round(s["t-stat"], 2),
                               "weekly_sharpe": p.mean() / p.std()})
    trials = pd.DataFrame(trials)
    trials.drop(columns="weekly_sharpe").to_csv("results/trials_log.csv", index=False)

    # the pre-registered rule and two comparisons
    pnl, weight = run(px, table)
    naive, _ = run(px, table, always_short=True)
    friday, _ = run(px, table, same_day=True)
    pnl_is, pnl_oos = pnl.loc[:IN_SAMPLE_END], pnl.loc[IN_SAMPLE_END:].iloc[1:]

    table_out = pd.DataFrame({"Always short": stats(naive), "Rule, in-sample": stats(pnl_is),
                              "Rule, out-of-sample": stats(pnl_oos), "Rule, 2006-2026": stats(pnl),
                              "Rule, seen on 26 Jan 2018": stats(pnl.loc[:"2018-01-26"]),
                              "Rule traded on Friday (not possible live)": stats(friday)})
    print(table_out.round(3))
    table_out.round(4).to_csv("results/summary.csv")

    # three probabilities that the true Sharpe is positive, from naive to honest
    sr0 = lucky_sharpe(trials["weekly_sharpe"])
    probs = {"n_trials": len(trials), "lucky_sharpe_annual": sr0 * np.sqrt(52)}
    for name, p in [("in_sample", pnl_is), ("full_period", pnl)]:
        probs[f"prob_normal_{name}"] = prob_sharpe(p, 0, use_moments=False)
        probs[f"prob_with_skew_kurtosis_{name}"] = prob_sharpe(p, 0)
        probs[f"deflated_sharpe_{name}"] = prob_sharpe(p, sr0)
    print("\n", pd.Series(probs).round(3))
    pd.Series(probs).round(4).to_csv("results/dsr.csv", header=False)

    # weekly results, published (no raw prices)
    signals = pd.DataFrame({"contango_ux2_ux1": table["ux2"] / table["ux1"] - 1,
                            "vol_of_future": table["vol"], "weight": weight})
    signals.round(4).to_csv("results/weekly_signals.csv")
    pd.DataFrame({"rule": pnl, "always_short": naive}).to_csv("results/weekly_pnl.csv", float_format="%.6f")

    worst = pd.DataFrame({"Always short": naive.nsmallest(8)}).join(pnl.rename("Rule"))
    worst.round(4).to_csv("results/worst_weeks.csv")
    print("\nWorst weeks:\n", worst.round(3))

    # is it alpha or hidden equity beta?
    spx = load_spx().resample("W-FRI").last().pct_change().reindex(pnl.index)
    both = pd.concat([pnl, spx], axis=1, keys=["pnl", "spx"]).dropna()
    reg = sm.OLS(both["pnl"], sm.add_constant(both["spx"])).fit()
    print(f"\nRegression on S&P: alpha {reg.params['const'] * 52:.2%}/yr (t={reg.tvalues['const']:.2f}),"
          f" beta {reg.params['spx']:.2f} (t={reg.tvalues['spx']:.2f}), R2 {reg.rsquared:.2f}")
    pd.Series({"alpha_per_year": reg.params["const"] * 52, "alpha_t": reg.tvalues["const"],
               "beta_sp500": reg.params["spx"], "beta_t": reg.tvalues["spx"], "r2": reg.rsquared}
              ).round(4).to_csv("results/regression.csv", header=False)

    # check of my roll against the S&P VIX short-term futures index (Bloomberg, monthly)
    long_front = weekly(daily_pnl(px, table, pd.Series(1.0, index=table.index)))
    mine = long_front.resample("ME").sum()
    spvx = pd.read_csv("data/bloomberg/spvxstr_monthly.csv", index_col=0, parse_dates=True)["spvxstr"]
    spvx = spvx.resample("ME").last().pct_change()
    check = pd.concat([mine, spvx], axis=1, keys=["my_long_front", "spvxstr"]).dropna()
    print(f"\nLong front future vs SPVXSTR: correlation {check.corr().iloc[0, 1]:.2f},"
          f" average month {check.mean().iloc[0]:.1%} vs {check.mean().iloc[1]:.1%}")
    check.round(4).to_csv("results/spvxstr_check.csv")

    # crises
    crises = {"Lehman (Sep-Nov 2008)": ("2008-09-01", "2008-11-28"),
              "US downgrade (Aug 2011)": ("2011-07-25", "2011-08-26"),
              "China deval. (Aug 2015)": ("2015-08-17", "2015-09-04"),
              "Volmageddon (Feb 2018)": ("2018-01-29", "2018-02-16"),
              "Covid (Feb-Mar 2020)": ("2020-02-21", "2020-03-31"),
              "Yen carry (Aug 2024)": ("2024-07-29", "2024-08-16"),
              "Tariffs (Apr 2025)": ("2025-03-31", "2025-04-25")}
    rows = []
    for name, (a, b) in crises.items():
        rows.append({"Crisis": name, "Always short": (1 + naive.loc[a:b]).prod() - 1,
                     "Rule (Monday trade)": (1 + pnl.loc[a:b]).prod() - 1,
                     "Rule, if traded on Friday": (1 + friday.loc[a:b]).prod() - 1})
    crisis = pd.DataFrame(rows).set_index("Crisis")
    print("\n", crisis.round(3))
    crisis.round(4).to_csv("results/crashes.csv")

    # ------------------------------------------------------------ charts
    plt.rcParams.update({"axes.spines.top": False, "axes.spines.right": False, "axes.grid": True,
                         "grid.color": "#e6e6e3", "grid.linewidth": 0.8, "font.size": 10})

    fig, ax = plt.subplots(2, 1, figsize=(10, 6), sharex=True, gridspec_kw={"height_ratios": [2, 1]})
    c = (table["ux2"] / table["ux1"] - 1).loc[START:] * 100
    ax[0].fill_between(c.index, c, 0, where=c > 0, color=BLUE, alpha=0.6, lw=0, label="Contango: short")
    ax[0].fill_between(c.index, c, 0, where=c <= 0, color=ORANGE, alpha=0.8, lw=0, label="Backwardation: cash")
    ax[0].set_ylabel("UX2 / UX1 - 1 (%)"); ax[0].legend(loc="lower left", frameon=False)
    ax[0].set_title(f"The VIX futures curve every Friday: contango {(c > 0).mean():.0%} of weeks")
    ax[1].plot(table["vix"].loc[START:], color=GREY, lw=1.2); ax[1].set_ylabel("VIX index")
    plt.tight_layout(); plt.savefig("results/3_term_structure.png", dpi=130); plt.close()

    plt.figure(figsize=(10, 5))
    (1 + naive).cumprod().plot(label="Always short (10% vol target)", color=GREY, lw=1.5)
    (1 + pnl).cumprod().plot(label="Rule: short only in contango", color=BLUE, lw=2)
    plt.axvspan(pd.Timestamp(IN_SAMPLE_END), pnl.index[-1], color=GREY, alpha=0.12, label="Out-of-sample")
    plt.axhline(1, color="black", lw=0.8); plt.xlabel(""); plt.ylabel("Value of 1 invested in 2006")
    plt.legend(frameon=False); plt.title("Growth of 1, after costs, weekly rebalancing"); plt.tight_layout()
    plt.savefig("results/1_equity_curve.png", dpi=130); plt.close()

    plt.figure(figsize=(10, 4.5))
    r = pnl[pnl != 0] * 100
    plt.hist(r, bins=80, color=BLUE, alpha=0.8, density=True, label="Weekly returns of the rule (in the market)")
    x = np.linspace(r.min(), r.max(), 400)
    plt.plot(x, norm.pdf(x, r.mean(), r.std()), color=ORANGE, lw=2, label="Normal law, same mean and vol")
    for d, v in pnl.nsmallest(3).items():
        plt.annotate(d.strftime("%d %b %Y"), (v * 100, 0.005), xytext=(v * 100, 0.06), ha="center",
                     arrowprops={"arrowstyle": "->", "color": GREY}, fontsize=9)
    plt.xlabel("Weekly return (% of capital)"); plt.legend(frameon=False)
    plt.title(f"Many small gains, a few huge losses: skew {skew(pnl):.1f}, kurtosis {kurtosis(pnl, fisher=False):.0f}")
    plt.tight_layout(); plt.savefig("results/4_weekly_returns.png", dpi=130); plt.close()

    plt.figure(figsize=(10, 4.5))
    t = trials.sort_values("sharpe_in_sample").reset_index(drop=True)
    chosen = (t["threshold"] == THRESHOLD) & (t["curve"] == CURVE) & (t["vol_window"] == VOL_WINDOW)
    plt.bar(t.index, t["sharpe_in_sample"], color=[ORANGE if x else BLUE for x in chosen], width=0.7)
    plt.axhline(sr0 * np.sqrt(52), color=GREY, ls="--", lw=1.5, label=f"Sharpe of the best of 27 by pure luck ({sr0 * np.sqrt(52):.2f})")
    plt.xticks([]); plt.ylabel("Sharpe, 2006-2015"); plt.legend(frameon=False)
    plt.title("The 27 trials, sorted (orange = the rule I fixed in advance)")
    plt.tight_layout(); plt.savefig("results/2_trials.png", dpi=130); plt.close()

    ax = (crisis * 100).plot.barh(figsize=(10, 5), color=[GREY, BLUE, AQUA], width=0.75)
    ax.axvline(0, color="black", lw=0.8); ax.invert_yaxis(); ax.set_xlabel("Return over the episode (% of capital)")
    ax.set_ylabel(""); ax.legend(frameon=False); ax.set_title("Crises: the filter helps, the weekend delay hurts")
    plt.tight_layout(); plt.savefig("results/5_crashes.png", dpi=130); plt.close()
