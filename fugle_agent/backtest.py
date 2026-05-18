"""Minimal long-only backtester for daily-bar strategies.

Designed to be tiny, readable, and dependency-free.  Strategies are passed
as a callable that, given the bars-so-far, returns a target position
(0 = flat, 1 = full long).  Two ready-made strategies are provided:
SMA crossover and RSI mean-reversion.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Sequence

from .indicators import rsi, sma


@dataclass
class Trade:
    entry_date: str
    exit_date: str
    entry_price: float
    exit_price: float
    return_pct: float


@dataclass
class BacktestResult:
    symbol: str
    strategy: str
    n_bars: int
    n_trades: int
    total_return_pct: float
    buy_hold_return_pct: float
    win_rate_pct: float
    max_drawdown_pct: float
    trades: list[Trade]

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "strategy": self.strategy,
            "n_bars": self.n_bars,
            "n_trades": self.n_trades,
            "total_return_pct": round(self.total_return_pct, 2),
            "buy_hold_return_pct": round(self.buy_hold_return_pct, 2),
            "win_rate_pct": round(self.win_rate_pct, 2),
            "max_drawdown_pct": round(self.max_drawdown_pct, 2),
            "trades": [t.__dict__ for t in self.trades[-20:]],  # last 20 only
        }


def _simulate(
    bars: Sequence[dict],
    positions: Sequence[int],
    *,
    fee_bps: float = 5.0,
) -> tuple[list[Trade], list[float]]:
    """Walk the bars, opening/closing positions when target flips 0<->1.

    Trades execute at next bar's open to avoid look-ahead.  Fees are
    charged each side as ``fee_bps / 10_000`` of notional.
    """
    trades: list[Trade] = []
    equity = 1.0
    equity_curve = [equity]
    in_pos = False
    entry_price = 0.0
    entry_date = ""
    fee = fee_bps / 10_000.0
    for i in range(1, len(bars)):
        target = positions[i - 1]  # signal computed on bar i-1, traded at open of bar i
        price = bars[i]["open"]
        if target == 1 and not in_pos:
            in_pos = True
            entry_price = price * (1 + fee)
            entry_date = bars[i]["date"]
        elif target == 0 and in_pos:
            exit_price = price * (1 - fee)
            ret = (exit_price - entry_price) / entry_price
            equity *= (1 + ret)
            trades.append(Trade(entry_date, bars[i]["date"], round(entry_price, 4),
                                round(exit_price, 4), round(ret * 100, 2)))
            in_pos = False
        # mark to market on close
        if in_pos:
            mtm = (bars[i]["close"] - entry_price) / entry_price
            equity_curve.append(equity * (1 + mtm))
        else:
            equity_curve.append(equity)
    # close any open position at last bar
    if in_pos:
        exit_price = bars[-1]["close"] * (1 - fee)
        ret = (exit_price - entry_price) / entry_price
        equity *= (1 + ret)
        trades.append(Trade(entry_date, bars[-1]["date"], round(entry_price, 4),
                            round(exit_price, 4), round(ret * 100, 2)))
        equity_curve[-1] = equity
    return trades, equity_curve


def _max_drawdown(equity_curve: Sequence[float]) -> float:
    peak = equity_curve[0]
    mdd = 0.0
    for v in equity_curve:
        peak = max(peak, v)
        dd = (v - peak) / peak
        mdd = min(mdd, dd)
    return mdd * 100.0


def _stats(symbol: str, name: str, bars: Sequence[dict], trades: list[Trade],
           equity_curve: list[float]) -> BacktestResult:
    bh = (bars[-1]["close"] - bars[0]["close"]) / bars[0]["close"] * 100.0
    wins = sum(1 for t in trades if t.return_pct > 0)
    win_rate = (wins / len(trades) * 100.0) if trades else 0.0
    total = (equity_curve[-1] - 1.0) * 100.0
    return BacktestResult(
        symbol=symbol,
        strategy=name,
        n_bars=len(bars),
        n_trades=len(trades),
        total_return_pct=total,
        buy_hold_return_pct=bh,
        win_rate_pct=win_rate,
        max_drawdown_pct=_max_drawdown(equity_curve),
        trades=trades,
    )


# ---------- strategies ----------

def sma_crossover(bars: Sequence[dict], *, fast: int = 20, slow: int = 60,
                  symbol: str = "?", fee_bps: float = 5.0) -> BacktestResult:
    """Long when fast SMA > slow SMA, flat otherwise."""
    if fast >= slow:
        raise ValueError("fast window must be < slow window")
    closes = [b["close"] for b in bars]
    f = sma(closes, fast)
    s = sma(closes, slow)
    positions: list[int] = []
    for fi, si in zip(f, s):
        if fi is None or si is None:
            positions.append(0)
        else:
            positions.append(1 if fi > si else 0)
    trades, curve = _simulate(bars, positions, fee_bps=fee_bps)
    return _stats(symbol, f"sma_cross({fast}/{slow})", bars, trades, curve)


def rsi_mean_reversion(bars: Sequence[dict], *, window: int = 14,
                       lower: float = 30.0, upper: float = 55.0,
                       symbol: str = "?", fee_bps: float = 5.0) -> BacktestResult:
    """Long when RSI dips below `lower`, exit when it rises above `upper`."""
    closes = [b["close"] for b in bars]
    r = rsi(closes, window)
    positions: list[int] = []
    holding = 0
    for v in r:
        if v is None:
            positions.append(0)
            continue
        if v < lower:
            holding = 1
        elif v > upper:
            holding = 0
        positions.append(holding)
    trades, curve = _simulate(bars, positions, fee_bps=fee_bps)
    return _stats(symbol, f"rsi_meanrev({window},{lower}/{upper})", bars, trades, curve)
