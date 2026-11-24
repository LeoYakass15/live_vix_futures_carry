# Downloads the settlement price of every VIX future since 2004 (CBOE, free).
# Paste it in a Google Colab cell and run it (about 5 minutes). Output: vix_futures_cboe.csv,
# to put in data/cboe/. I run it every weekend before live.py.
import csv
import datetime as dt
import io
import pandas as pd
import requests

MONTH_CODES = "FGHJKMNQUVXZ"


def expiry_guess(year, month):
    # Wednesday 30 days before the 3rd Friday of the next month
    ny, nm = (year + 1, 1) if month == 12 else (year, month + 1)
    first = dt.date(ny, nm, 1)
    third_friday = first + dt.timedelta(days=(4 - first.weekday()) % 7 + 14)
    return third_friday - dt.timedelta(days=30)


def download(url):
    try:
        r = requests.get(url, timeout=30, headers={"User-Agent": "Mozilla/5.0"})
    except Exception as e:
        print("   network error:", e)
        return None
    if r.status_code != 200 or len(r.text) < 100:
        return None
    start = r.text.find("Trade Date")
    if start < 0:
        return None
    # some old lines have one comma too many: keep as many fields as the header
    rows = list(csv.reader(io.StringIO(r.text[start:])))
    header = [h.strip() for h in rows[0]]
    data = [row[:len(header)] for row in rows[1:] if len(row) >= len(header)]
    return pd.DataFrame(data, columns=header)


def keep_columns(df):
    # date, settlement and close (early 2013 CBOE wrote Settle = 0, then I use Close)
    names = {c.strip().lower(): c for c in df.columns}
    if "trade date" not in names or "settle" not in names:
        return None
    out = pd.DataFrame({"Trade Date": df[names["trade date"]],
                        "Settle": pd.to_numeric(df[names["settle"]], errors="coerce")})
    out["Close"] = pd.to_numeric(df[names["close"]], errors="coerce") if "close" in names else None
    return out


tables = []
for year in range(2004, dt.date.today().year + 2):
    for month in range(1, 13):
        if (year, month) < (2004, 5):
            continue
        exp = expiry_guess(year, month)
        recent = None
        for d in (exp, exp - dt.timedelta(days=1)):        # holiday: one day earlier
            recent = download(f"https://cdn.cboe.com/data/us/futures/market_statistics/historical_data/VX/VX_{d}.csv")
            if recent is not None:
                break
        code = MONTH_CODES[month - 1] + str(year)[2:]
        old = download(f"https://cdn.cboe.com/resources/futures/archive/volume-and-price/CFE_{code}_VX.csv")
        candidates = [keep_columns(x) for x in (recent, old) if x is not None]
        candidates = [x for x in candidates if x is not None]
        if not candidates:
            continue                                         # not listed yet
        df = max(candidates, key=lambda x: ((x["Settle"] > 0) | (x["Close"].fillna(0) > 0)).sum())
        df["Expiry"] = exp
        tables.append(df)
        print("ok:", year, month, len(df), "rows")

out = pd.concat(tables)
out.to_csv("vix_futures_cboe.csv", index=False)
print("Done:", len(out), "rows,", out["Expiry"].nunique(), "contracts")
