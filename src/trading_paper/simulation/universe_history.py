"""Download a historically liquid, multi-coin KRW universe for paper research."""
from __future__ import annotations

import csv
import json
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from trading_paper.providers.upbit_public import PublicQuotationClient
from trading_paper.simulation.history import CSV_FIELDS, download_minute_history


def _candles_between(client: PublicQuotationClient, market: str,
                     start_utc: datetime, end_utc: datetime) -> list[dict[str, Any]]:
    cursor = end_utc
    found: dict[datetime, dict[str, Any]] = {}
    while cursor > start_utc:
        page = client.minute_candles(market, count=200, to=cursor.strftime("%Y-%m-%dT%H:%M:%SZ"))
        if not page:
            break
        oldest: datetime | None = None
        for row in page:
            stamp = datetime.fromisoformat(row["candle_date_time_utc"]).replace(tzinfo=timezone.utc)
            oldest = stamp if oldest is None else min(oldest, stamp)
            if start_utc <= stamp < end_utc:
                found[stamp] = row
        if oldest is None or oldest >= cursor:
            raise RuntimeError(f"Historical candle paging did not move backward for {market}")
        cursor = oldest
    return [found[key] for key in sorted(found)]


def rank_by_prior_day_turnover(client: PublicQuotationClient, start: datetime,
                               *, min_candles: int = 60,
                               progress: Callable[[int, int, str], None] | None = None
                               ) -> list[dict[str, Any]]:
    """Rank active KRW pairs by turnover during the 24 hours before start."""
    if start.tzinfo is None or start.utcoffset() is None:
        raise ValueError("start must include a timezone offset")
    start_utc = start.astimezone(timezone.utc)
    lookback_start = start_utc - timedelta(days=1)
    markets = client.list_krw_pairs()
    ranked: list[dict[str, Any]] = []
    for index, market in enumerate(markets, start=1):
        rows = _candles_between(client, market, lookback_start, start_utc)
        if (len(rows) >= min_candles
                and rows[0]["candle_date_time_utc"] <= (lookback_start + timedelta(hours=4)).strftime("%Y-%m-%dT%H:%M:%S")
                and rows[-1]["candle_date_time_utc"] >= (start_utc - timedelta(hours=6)).strftime("%Y-%m-%dT%H:%M:%S")):
            turnover = sum(float(row["candle_acc_trade_price"]) for row in rows)
            first = float(rows[0]["opening_price"])
            high = max(float(row["high_price"]) for row in rows)
            low = min(float(row["low_price"]) for row in rows)
            ranked.append({
                "market": market,
                "prior_24h_candles": len(rows),
                "prior_24h_active_span_hours": round(
                    (datetime.fromisoformat(rows[-1]["candle_date_time_utc"]) -
                     datetime.fromisoformat(rows[0]["candle_date_time_utc"])).total_seconds() / 3600, 2
                ),
                "prior_24h_turnover_krw": turnover,
                "prior_24h_range_pct": (high - low) / first * 100 if first else 0,
            })
        if progress is not None:
            progress(index, len(markets), market)
    ranked.sort(key=lambda row: (-row["prior_24h_turnover_krw"], row["market"]))
    return ranked


def download_liquid_universe_history(
    start: datetime,
    end: datetime,
    destination: str | Path,
    *,
    top_n: int = 10,
    client: PublicQuotationClient | None = None,
    progress: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Select top-N active markets by prior-24h turnover; always add BTC if needed."""
    if start.tzinfo is None or end.tzinfo is None or start.utcoffset() is None or end.utcoffset() is None:
        raise ValueError("start and end must include timezone offsets")
    if start >= end or top_n < 1:
        raise ValueError("Expected start < end and top_n >= 1")
    quotation = client or PublicQuotationClient(requests_per_second=2.0)

    def screen_progress(index: int, total: int, market: str) -> None:
        if progress is not None and (index % 10 == 0 or index == total):
            progress(f"Liquidity screen {index}/{total}: {market}")

    ranking = rank_by_prior_day_turnover(quotation, start, progress=screen_progress)
    ranked_markets = [row["market"] for row in ranking]
    if "KRW-BTC" not in ranked_markets:
        raise ValueError("BTC lacks enough prior-24h candles to serve as the required benchmark")
    preferred = ranked_markets[:top_n]
    if "KRW-BTC" in ranked_markets and "KRW-BTC" not in preferred:
        preferred.append("KRW-BTC")

    target = Path(destination)
    target.parent.mkdir(parents=True, exist_ok=True)
    rows_by_key: dict[tuple[str, str], dict[str, str]] = {}
    selected: list[dict[str, Any]] = []
    expected_minutes = int((end - start).total_seconds() // 60)
    with tempfile.TemporaryDirectory(prefix="coin-history-") as scratch:
        for market in preferred:
            temporary_csv = Path(scratch) / f"{market.replace('-', '_')}.csv"
            result = download_minute_history(market, start, end, temporary_csv, client=quotation)
            with temporary_csv.open("r", newline="", encoding="utf-8") as source:
                for row in csv.DictReader(source):
                    rows_by_key[(row["timestamp"], market)] = row
            selected.append({
                **next(row for row in ranking if row["market"] == market),
                "week_candles": result["candles"],
                "week_coverage_pct": round(result["candles"] / expected_minutes * 100, 2)
                if expected_minutes else 0,
            })
            if progress is not None:
                progress(f"Downloaded {market}: {result['candles']}/{expected_minutes} candles")

    with target.open("w", newline="", encoding="utf-8") as destination_file:
        writer = csv.DictWriter(destination_file, fieldnames=CSV_FIELDS)
        writer.writeheader()
        for key in sorted(rows_by_key):
            writer.writerow(rows_by_key[key])
    metadata = {
        "selection_method": "top active KRW markets by prior-24h candle turnover at the start timestamp; BTC included if outside top-N",
        "prior_24h_activity_gate": "at least 60 traded minutes, activity spanning 20 hours, and a trade candle in the final 6 hours",
        "prior_24h_start": (start.astimezone(timezone.utc) - timedelta(days=1)).isoformat(),
        "prior_24h_end_exclusive": start.astimezone(timezone.utc).isoformat(),
        "history_start": start.isoformat(),
        "history_end_exclusive": end.isoformat(),
        "requested_top_n": top_n,
        "markets_selected": selected,
        "markets_scanned_with_sufficient_prior_candles": len(ranking),
        "markets_with_any_week_candles": sum(row["week_candles"] > 0 for row in selected),
        "markets_with_95pct_week_coverage": sum(row["week_coverage_pct"] >= 95 for row in selected),
        "current_market_list_size": len(quotation.list_krw_pairs()),
        "caveat": "Current KRW listings only; delisted markets absent from today's list cannot be ranked.",
        "csv": str(target),
    }
    metadata_path = target.with_suffix(".universe.json")
    metadata_path.write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
    return metadata
