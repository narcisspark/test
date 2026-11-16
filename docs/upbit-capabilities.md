# Upbit public Quotation capability record

Checked against official Upbit Developer Center pages on **2026-10-14 (Asia/Seoul)**. Only public Quotation endpoints and public WebSocket streams are in scope.

**2026-10-17 addendum:** Lambda minute polling also allows public `GET /v1/orderbook?markets=KRW-BTC`. The first `orderbook_units` item supplies current ask/bid and sizes; receipt time and the response millisecond timestamp are preserved. The official orderbook group allows 10 requests/sec by IP; this client remains paced at 2/sec. The service/Lambda warm-up paginates with exclusive `to` to cover the current KST session. Original local `paper run` keeps its 200-candle warm-up.

| Capability | Official contract | Client behavior |
|---|---|---|
| Product split | Quotation is public/read-only; Exchange is private and requires API key authentication. | Only Quotation routes are implemented; no private client or credentials. |
| Pair list | `GET https://api.upbit.com/v1/market/all?is_details=true`; KRW pairs can be filtered from `market`. `market_warning` is included with details. | Fetch once before live run; select current KRW pairs and exclude `CAUTION`. |
| Minute candles | `GET /v1/candles/minutes/1` with `market`, optional `count` up to 200 and exclusive `to`; only intervals with trades produce candles. | One REST warm-up request per pair, up to 200 rows; latest row is excluded as possibly incomplete. |
| Candle stream | Public WebSocket `wss://api.upbit.com/websocket/v1`, type `candle.1m`, `codes`; updates occur when the candle changes, not necessarily every second. | Detect new candle start and then finalize the prior candle. |
| Ticker/trade/orderbook | Same public WebSocket endpoint; `ticker`, `trade`, `orderbook`. Timestamps are exchange milliseconds. Top orderbook unit carries first ask/bid prices and sizes. | Use later ask for virtual buys, later bid for exits; last quote fallback applies configured extra spread. |
| Keepalive | WebSocket supports Ping/Pong; server idle timeout is 120 seconds. | `websockets` protocol ping every 30 seconds and reconnect with bounded exponential backoff. |
| REST rate limits | `market`, `candle`, `ticker`, `orderbook` groups: up to 10 requests/sec each, measured by IP. | Local REST requests are paced at 2/sec per client, without Origin header. |
| WebSocket rate limits | Public `websocket-connect`: up to 5/sec per IP. `websocket-message`: up to 5/sec and 100/min per connection. | One connection and one subscription message per run; retry backoff starts at 1 second. |
| Restrictions | Policies may change and additional restrictions can apply. Candle rows with no trades are absent. | Logs timestamps and gaps; it does not synthesize missing candles. |

## Official sources

- [API overview and Public/Private split](https://docs.upbit.com/kr/reference/api-overview)
- [Public WebSocket guide, endpoints, request format, keepalive](https://docs.upbit.com/kr/reference/websocket-guide)
- [Minute candle REST endpoint](https://docs.upbit.com/kr/reference/list-candles-minutes)
- [Public REST orderbook endpoint](https://docs.upbit.com/kr/reference/list-orderbooks)
- [Market/pair list REST endpoint](https://docs.upbit.com/kr/reference/list-trading-pairs)
- [WebSocket candle](https://docs.upbit.com/kr/reference/websocket-candle)
- [WebSocket ticker](https://docs.upbit.com/kr/reference/websocket-ticker)
- [WebSocket orderbook](https://docs.upbit.com/kr/reference/websocket-orderbook)
- [Rate limits](https://docs.upbit.com/kr/v1.6.4/reference/rate-limits)

## Open limitations

Market-specific fee rates are not inferred from the public quotation API and remain a user-editable assumption in `config/crypto-paper.json`. The client's quote-only fills do not test account permissions, real fills, partial fills, or the exchange's private API. Pair availability and documentation limits can change after this review.
