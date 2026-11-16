# AlphaScalp actual-price paper experiment

This experiment compares the archive's two different RSI/Bollinger entry implementations against the existing A/B/C strategies using the same live Upbit public feed.

## Candidate definitions

- **D — UI rule:** on completed 1-minute candles, buy when RSI(14) is below 42 **or** the close is within 0.8% above the lower Bollinger band (20, 2), and the close is above the previous candle's close. The archive's UI uses a 0.9% stop, 1.5% take profit, and a 0.5% trailing stop after a +0.75% rise.
- **E — server daemon rule:** same RSI/Bollinger condition without the rebound confirmation. Its archive defaults use a 1.0% stop, 1.5% take profit, and the same trailing settings.
- Neither version includes the volume surge condition claimed in the archive's explanatory text. Both source paths use different entry rules, so they are measured separately.

Signals use completed real 1-minute candles and are filled only on a later valid public ask. Exits are checked on fresh public bid/last quotes. This avoids look-ahead and replaces the archive's random prices with real market data. It does not recreate the archive's claimed 1-second behavior because the archive did not define a real 1-second candle/feed implementation.

## Shared experiment controls

The config retains A/B/C as controls. Every strategy starts with the same virtual KRW balance and sees the same BTC feed. All strategies use the project's shared risk caps; D/E override stop/target/trailing rules as above. Fee, slippage, stale-quote, and daily loss settings are in `config/crypto-paper-alpha-scalp.json`.

The run uses only public Upbit Quotation data and writes paper fills locally. It never loads API keys or calls private account/order endpoints. Results are an exploratory forward paper sample, not proof of a durable edge. Compare net P&L after costs, drawdown, trade count, time in market, and quote gaps; do not select a strategy from win rate alone.

Run with a timezone-aware deadline:

```bash
PYTHONPATH=src python3.11 -m trading_paper paper run-service \
  --markets KRW-BTC \
  --config config/crypto-paper-alpha-scalp.json \
  --run-id alpha-scalp-paper-week-YYYYMMDD \
  --deadline 2026-11-10T20:35:00+09:00 \
  --checkpoint-seconds 30 \
  --output-dir outputs/live-paper-service
```
