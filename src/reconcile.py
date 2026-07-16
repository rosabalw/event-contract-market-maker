"""
reconcile.py — cashflow reconciliation for inventory P&L.

The habit this encodes: never trust a *derived* P&L number you haven't reconciled against raw
cashflows. A realized-P&L accumulator can carry a subtle sign or bookkeeping error that silently
negates results while every unit test still passes. The independent check is simple and unforgeable —
a closed round-trip's P&L must equal (cash from sells) − (cash from buys).

This is the exact check that caught a sign-inversion in the market-making engine's inventory
accumulator: replaying one market's fills gave model = −162 while true cashflow = +162 — exact
negatives, i.e., the realized formula was returning the negative of the true P&L.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import List, Tuple


def apply_fill(book: dict, side: str, px: float, size: float) -> float:
    """Weighted-average-cost realized P&L. Correct sign convention:
    long (inv>0) profits when px>cost -> (px-cost); short (inv<0) profits when px<cost -> (cost-px)."""
    inv, cost = book["inv"], book["cost"]
    signed = size if side == "buy" else -size
    realized = 0.0
    if inv == 0 or (inv > 0) == (signed > 0):                 # opening / adding
        book["cost"] = (cost * abs(inv) + px * size) / (abs(inv) + size)
        book["inv"] = inv + signed
    else:                                                     # reducing / flipping
        closed = min(size, abs(inv))
        realized = (px - cost) * closed * (-1 if inv < 0 else 1)
        book["inv"] = inv + signed
        if (inv > 0) != (book["inv"] > 0) and book["inv"] != 0:
            book["cost"] = px
    return realized


def reconcile(fills: List[Tuple[str, float, float]], tol: float = 1e-9) -> dict:
    """fills: list of (side, price, size). Returns model realized (from apply_fill) vs independent
    cashflow, plus a pass/fail. For a book that ends flat, the two MUST be equal."""
    book = {"inv": 0.0, "cost": 0.0}
    model_realized = cash = 0.0
    last_px = 0.0
    for side, px, size in fills:
        model_realized += apply_fill(book, side, px, size)
        cash += (px * size) if side == "sell" else -(px * size)
        last_px = px
    true_pnl = cash + book["inv"] * last_px          # cashflow + open inventory marked at last price
    diff = abs(model_realized - true_pnl)
    return {
        "model_realized": round(model_realized, 6),
        "true_cashflow_pnl": round(true_pnl, 6),
        "open_inventory": book["inv"],
        "reconciled": diff <= tol,
        "discrepancy": round(diff, 6),
    }


if __name__ == "__main__":
    # A choppy, mean-reverting round-trip that ends flat — realized P&L must equal net cashflow.
    fills = [
        ("buy", 0.34, 100), ("buy", 0.31, 100), ("sell", 0.31, 200),   # long, closed at a small loss
        ("sell", 0.36, 100), ("buy", 0.03, 100),                        # short, covered much lower (profit)
        ("buy", 0.02, 200), ("sell", 0.39, 200),                        # long low, sold high (profit)
    ]
    r = reconcile(fills)
    print("reconciliation:")
    for k, v in r.items():
        print(f"  {k:20} {v}")
    assert r["reconciled"], (
        "P&L accumulator does not reconcile against cashflow — check the realized-P&L sign convention."
    )
    print("\nPASS — model realized P&L matches independent cashflow accounting.")
