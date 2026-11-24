# Rules of the live book (frozen on 29 November 2026)

These rules are written before the first live week. If I change one, the book becomes "v2"
and starts a new track record. I never rewrite the past.

1. **Instrument**: the front VIX future (CBOE), 1,000 USD per point. If it expires before the next Friday, I hold the 2nd contract instead.
2. **Signal**: every Friday, at the CBOE settlement. Short if the 2nd future is above the 1st (UX2 / UX1 - 1 > 0, contango). Otherwise cash. Same code as the backtest (`backtest.positions`).
3. **Size**: notional = 10% of the NAV / volatility of the future I hold (20 days of daily returns, annualised), capped at 100% of the NAV. Rounded to the nearest whole contract.
4. **Rebalancing**: every week, at the settlement of the next trading day (Monday). No change during the week, whatever happens.
5. **Publication**: the position for Monday is committed to GitHub before Sunday midnight, Paris time, when VIX futures reopen. The commit time stamp is the proof.
6. **Prices**: CBOE daily settlements, downloaded every weekend with `scripts/download_cboe.py`. Bloomberg photos only for spot checks.
7. **Costs**: 0.05 VIX point per contract traded (one tick, 50 USD). A roll counts as two trades.
8. **Capital**: 1,000,000 USD on paper. P&L in USD and in % of capital.
9. **Weekly report**: every week is reported in LIVE.md, good or bad, with a letter to investors at the end of each month. A missed weekend is written as missed, and the position is kept.
10. **No discretion**: I do not cut the position because the news looks scary. The rule decides.
