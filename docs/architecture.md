# Crypto Paper Trading architecture

The existing AWS schedule remains the Upbit public-data experiment. N2 domestic equities are isolated in a read-only Stage 0 adapter that retrieves its app credentials from SSM SecureString. It has no account, balance, order, cancel, or replace calls. Its quote and 5-minute data are not connected to a stock entry engine until quote freshness, market-status codes, and bar completion semantics have been verified during market hours. See [N2 capability evidence](provider-capabilities.md).

## Public data boundary

`providers.upbit_public.PublicQuotationClient` hardcodes the official public Quotation host and allows only:

- REST `GET /v1/market/all`
- REST `GET /v1/candles/minutes/1`
- REST `GET /v1/orderbook` (current public ask/bid for Lambda polling)
- WebSocket `wss://api.upbit.com/websocket/v1`
- public stream types `ticker`, `trade`, `orderbook`, `candle.1m`

The REST opener disables environment proxy discovery. The client accepts no credentials, sends no Authorization header, and has no Exchange/private path or method. The WebSocket client disables proxy discovery, sends one public subscription request, and uses protocol ping/pong every 30 seconds.

## Event flow

The current AWS deployment uses the [minute-polling Lambda flow](aws-lambda-run.md). It restores state from private S3, evaluates new completed REST candles, then samples the current REST orderbook for virtual fills. It never infers fills from minute highs/lows. A DynamoDB lease serializes overlapping invocations; a source-data journal precedes the state checkpoint. The AWS SDK uses the function's IAM role solely for this storage, lease, logs and stopping its schedule. The market-data client still accepts no exchange credentials.

The original local WebSocket mode follows this flow:

1. Fetch current KRW pair list; reject unsupported or caution-marked pairs.
2. Fetch up to 200 1-minute candles for indicator warm-up; exclude the latest REST candle because it may be incomplete.
3. Subscribe once to public candles, trades, ticker, and orderbook for the configured pair list.
4. When the candle timestamp advances, finalize the previous candle, compute the same A/B/C signal inputs, and queue virtual entries.
5. A pending signal can fill only from a later exchange-timestamped quote. Buy uses ask, sell uses bid; last-price fallback adds a configurable half-spread and slippage.
6. Each strategy has separate cash, positions, pending signals, risk state, and ledger. All three see the same source events.
7. Append timestamped raw events and finalized candles. On Ctrl-C or duration stop, save JSON and Markdown reports without force-closing a position when no fresh quote is available.

## Freshness and recovery

An event with exchange time older than the configured quote-age limit is rejected. No new entry is made while disconnected or from stale data. On disconnect, existing virtual positions remain open and use the last mark with its age visible. Reconnect delay starts at one second and doubles with small jitter up to the configured cap. The connection attempt stays well under the documented public connection rate.

## Risk and strategy time

KST date changes reset the daily loss/trade counters and the VWAP/indicator strategy history. Positions can continue across midnight because crypto markets never close. Strategy C defines its opening range as the first 15 KST minute bars after midnight. The 1-minute candle stream updates only when a trade changes the candle, so candle transition detection may be delayed during quiet periods.

## Offline replay

`paper replay-live` replays the persisted JSONL events in recorded arrival order, including REST warm-up candles, WebSocket exchange timestamps, and receipt timestamps. The replay reconstructs signal and virtual fill decisions without network access. `simulate --input` also supports a simpler complete-bar CSV replay; it cannot reproduce archived quote-level fills.

## Not modeled

No Exchange/Private API, actual order placement, cancellations, account balances, coin deposits/withdrawals, market impact, queue position, partial depth fills, service-wide outages, or guarantee of fill. Slippage and fees are configurable assumptions; quote sizes are received but current Paper sizing does not simulate partial fills against them. Feed updates are saved for later replay and auditing, not used as investment recommendations.
