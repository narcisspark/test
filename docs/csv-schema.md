# Offline OHLCV CSV format

The `simulate` command reads a UTF-8 or UTF-8-with-BOM CSV. Required columns:

```csv
timestamp,symbol,market,open,high,low,close,volume,complete,turnover_krw
2026-10-14T09:30:00+09:00,KRW-BTC,KRW,100000000,100010000,99990000,100005000,1.25,true,125005000
```

| Column | Rule |
|---|---|
| `timestamp` | ISO-8601 timestamp with explicit UTC offset or `Z`; candle start time |
| `symbol` | Upbit pair such as `KRW-BTC` |
| `market` | `KRW` for digital assets; equity replay additionally accepts `KOSPI`/`KOSDAQ` |
| `open`, `high`, `low`, `close` | Positive KRW prices with `high >= open/close/low` and `low <= open/close/high` |
| `volume` | Non-negative base-asset quantity traded during the candle |
| `complete` | `true`/`false` or `1`/`0`; false rows are discarded before signals/replay |
| `turnover_krw` | Optional. Candle traded value in KRW. When absent, VWAP uses typical price `(high+low+close)/3 × volume` as an estimate. |

Input is sorted by exchange timestamp and symbol. Duplicate symbol/timestamp pairs, missing required columns, invalid timezone, invalid OHLC, unknown market, or empty completed input stops the replay. Gaps stay gaps; the simulator does not invent candles. Strategy indicators use completed bars only. Offline OHLC replay fills on the next bar's opening value with configured costs; for saved quote-level live data use `paper replay-live` instead.
