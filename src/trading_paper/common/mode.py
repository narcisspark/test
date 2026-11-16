"""Fail-closed execution mode parsing; live mode is deliberately unsupported."""

from __future__ import annotations

from enum import StrEnum


class ExecutionMode(StrEnum):
    PAPER = "PAPER"


def require_paper_mode(value: str = "PAPER") -> ExecutionMode:
    normalized = value.strip().upper()
    if normalized != ExecutionMode.PAPER:
        raise ValueError("Only PAPER mode is supported; live execution is not implemented.")
    return ExecutionMode.PAPER
