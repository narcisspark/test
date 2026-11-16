from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any


SUMMARY_FIELDS = (
    "strategy", "initial_cash_krw", "final_equity_krw", "net_pnl_krw", "net_return_pct",
    "max_drawdown_pct", "closed_trades", "win_rate_pct", "average_closed_trade_net_pnl_krw",
    "profit_factor", "total_costs_krw", "classification",
)
TRADE_FIELDS = (
    "strategy", "symbol", "market", "signal_time", "entry_time", "exit_time", "quantity",
    "entry_price_krw", "exit_price_krw", "gross_market_move_pnl_krw", "gross_price_pnl_krw",
    "commission_krw", "sell_tax_krw", "estimated_spread_slippage_krw", "total_costs_krw",
    "net_pnl_krw", "net_return_pct", "exit_reason",
    "signal_reason", "synthetic_demo",
)


def write_reports(result: dict[str, Any], output_dir: str | Path) -> dict[str, str]:
    destination = Path(output_dir)
    destination.mkdir(parents=True, exist_ok=True)
    summary_path = destination / "summary.csv"
    trades_path = destination / "trades.csv"
    json_path = destination / "report.json"
    markdown_path = destination / "report.md"
    _write_csv(summary_path, [result["strategies"][key] for key in ("A", "B", "C")], SUMMARY_FIELDS)
    _write_csv(trades_path, result["trades"], TRADE_FIELDS)
    json_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    markdown_path.write_text(_markdown(result), encoding="utf-8")
    return {"summary_csv": str(summary_path), "trades_csv": str(trades_path), "report_json": str(json_path), "report_markdown": str(markdown_path)}


def _write_csv(path: Path, rows: list[dict[str, Any]], fields: tuple[str, ...]) -> None:
    with path.open("w", newline="", encoding="utf-8-sig") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def _markdown(result: dict[str, Any]) -> str:
    demo = result["synthetic_demo"]
    warning = (
        "> # SYNTHETIC/DEMO — 성과 증거가 아닙니다\n> 이 데이터는 전략의 입출력 흐름을 보여주기 위해 결정론적으로 생성했습니다. "
        "수익성, 실거래 체결 가능성, 미래 성과를 주장하거나 검증하지 않습니다.\n\n"
        if demo else
        "> 과거 데이터 재생 결과이며 시뮬레이션입니다. 미래 성과 또는 실거래 체결 결과를 뜻하지 않습니다.\n\n"
    )
    lines = [
        "# 로컬 단타 전략 비교 리포트", "", warning,
        f"- 구분: **{result['classification']}**",
        f"- 완성 입력 행: {result['input_rows_complete']}",
        f"- 구간: {result['first_timestamp']} ~ {result['last_timestamp']}",
        f"- 종목: {', '.join(result['symbols'])}",
        f"- 세션: {', '.join(result['sessions'])}",
        f"- 전략별 가상 초기자금: KRW {result['config']['initial_cash_krw']:,}",
        "", "## 비교 요약", "",
        "| 전략 | 종료 평가액 | 순손익 | 수익률 | 최대낙폭 | 종료 거래 | 승률 | 비용 |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for strategy_id in ("A", "B", "C"):
        row = result["strategies"][strategy_id]
        lines.append(
            f"| {strategy_id} | {row['final_equity_krw']:,.0f} | {row['net_pnl_krw']:,.0f} | "
            f"{row['net_return_pct']:.3f}% | {row['max_drawdown_pct']:.3f}% | {row['closed_trades']} | "
            f"{row['win_rate_pct']:.1f}% | {row['total_costs_krw']:,.0f} |"
        )
    lines.extend([
        "", "## 전략 정의", "",
        "- **A**: EMA5>EMA20, EMA20 상승, 종가>세션 VWAP, 직전 6개 고가 돌파, 직전 20개 완성봉 평균 대비 거래량 배수 확인.",
        "- **B**: 종가가 세션 VWAP보다 설정 비율만큼 낮고 종가 반등이 확인되면 진입 후보.",
        "- **C**: 첫 설정 구간의 고가를 종가가 상향 교차하면 진입 후보.",
        "", "## 체결·위험 가정", "",
        "신호는 완성봉에서 계산하고 다음 같은 종목 관측봉의 시가에 진입합니다. 매수·매도 스프레드와 슬리피지, 설정 수수료를 비용으로 적용합니다. 동일 봉에서 손절과 익절이 모두 닿으면 손절 우선입니다. 코인 연속 replay는 날짜 경계에서 강제 청산하지 않습니다.",
        "", "## 해석 한계", "",
        "이 replay는 봉 내부의 가격 경로, 실제 bid/ask, 호가 잔량, 시장 충격, 부분 체결, 미체결을 재현하지 않습니다. OHLCV를 사용하므로 실제 체결 가능성을 보장하지 않으며, 수수료·스프레드·슬리피지는 예시 가정입니다. KRW 코인 거래 세금은 이 설정에서 0으로 두며 별도 세금 검토를 포함하지 않습니다.",
        "",
    ])
    return "\n".join(lines)
