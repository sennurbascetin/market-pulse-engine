# Demo mode

Markets are shut most of the week. A portfolio demo that opens onto a frozen tape sells nothing,
so the engine can keep the board alive with synthetic ticks.

```bash
MPE_DEMO_MODE=true python run.py --backfill
```

or set `MPE_DEMO_MODE=true` in `.env`.

---

## What it actually does

Each cycle, `pipeline/demo_seed.py` appends one synthetic observation per ticker:

* the price continues from that ticker's **genuine last close**, moved by a Gaussian step
  calibrated to its own recently measured volatility (clamped to 0.02%–2% per tick);
* the volume is drawn lognormally around that ticker's **own median interval volume**, so a
  BTC-USD tick stays in BTC-USD's range and an AAPL tick in AAPL's.

Everything downstream then behaves normally: the transforms run, the anomaly engine finds
genuine outliers in the synthetic series, and the analyst writes a briefing about it.

## Synthetic rows are labelled, not disguised

This matters more than the feature does.

| Marker | Value |
|---|---|
| `bronze.raw_quotes.source` | `demo_synthetic` |
| `bronze.raw_quotes.is_live` | `false` |
| payload key | `"_synthetic": true` |
| `pipeline.run_log.mode` | `demo` |
| dashboard status bar | `MODE demo` |
| console banner at start-up | `demo mode ON — synthetic ticks are labelled 'demo_synthetic'` |

Nothing downstream has to trust a flag it cannot see, and no query can mistake generated data for
observed data:

```sql
-- observed only
SELECT * FROM bronze.raw_quotes WHERE source <> 'demo_synthetic';

-- how much of Gold is synthetic?
SELECT s.source, count(*)
FROM gold.quotes_enriched g
JOIN silver.quotes s USING (ticker, observed_at)
GROUP BY s.source;
```

## Removing it again

Synthetic rows are ordinary Bronze rows, so deleting them and rebuilding is clean:

```bash
python - <<'PY'
from market_pulse_engine.db.connection import transaction
with transaction() as conn:
    conn.execute("DELETE FROM bronze.raw_quotes WHERE source = 'demo_synthetic'")
    conn.execute("DELETE FROM silver.quotes WHERE source = 'demo_synthetic'")
    conn.execute("DROP TABLE IF EXISTS gold.quotes_enriched")
PY
python -m market_pulse_engine.db.init
python -m market_pulse_engine.transforms.run_transforms
```

---

## Prefer real data where you can

Demo mode is the fallback, not the default. For a live demo the better option is
**`python run.py --backfill`**, which pulls five days of genuine 5-minute bars from yfinance —
around 4,500 real observations across the watchlist. That gives the Gold layer real rolling
windows, the anomaly engine real outliers to find, and the dashboard a real chart, without a
single synthetic row.

Crypto pairs (`BTC-USD`, `ETH-USD`) trade continuously, so even at 03:00 on a Sunday the engine
has genuinely live instruments to poll. Demo mode is only needed if you want all seven rows of
the board moving at once.
