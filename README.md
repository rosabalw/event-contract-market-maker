# event-contract-market-maker

A research market-making engine for **binary event contracts** on Polymarket and Kalshi. It rests two-sided quotes against live order books, manages inventory with dynamic skew, pulls quotes under toxic flow, and measures the one thing you can't derive analytically — **how badly adverse selection bites**.

Paper-only: sends no real orders and needs no API key. The point isn't P&L on a demo; it's an honest measurement harness for the microstructure and reward economics of event-contract liquidity provision.

---

## Why this exists

Providing liquidity on prediction markets looks profitable on paper (you earn incentives + rebates for resting) and quietly isn't (informed flow picks you off). The gap between those two is **adverse selection**, and it can't be modeled from first principles — it has to be measured against real books. This engine is built to measure it *pessimistically*, so the number you get is a floor, not a fantasy.

---

## Architecture

A per-market state machine driven by live BBO + trade data:

```
        ┌────────┐  book valid       ┌────────┐
        │  SCAN  │ ────────────────▶ │ QUOTE  │  rest two-sided at BBO
        └────────┘                   └───┬────┘
                                         │ |inventory| > soft cap
                          toxicity ≥ θ    ▼
        ┌────────┐  cooldown + calm  ┌────────┐
        │  KILL  │ ◀──────────────── │  LEAN  │  drop the inventory-adding side
        └────────┘                   └────────┘
          flatten via taker, pull all quotes
```

- **QUOTE** — rest bid at last bestBid, ask at last bestAsk.
- **LEAN** — above a soft inventory cap, stop quoting the side that would add to the position (inventory skew).
- **KILL** — when a toxicity score crosses threshold, flatten and pull all quotes for a cooldown window. This is what keeps adverse selection near zero.

## Key components

**Toxicity gauge (the kill trigger).** A composite of top-of-book imbalance, mid-price drift, and trade-volume burst — a lightweight, latency-friendly cousin of VPIN/OFI. When it spikes (a real BTC move, a news event), the engine steps out of the way *before* the informed fill compounds.

**Order-flow toxicity gauge (`toxicity.py`), the data-validated successor.** On order-level (MBO) feeds, a head-to-head on 22 ES + 23 NQ sessions replaced the hand-weighted score with an empirically-fit one: **order-arrival intensity + mid-range + the fraction of orders that flicker and cancel in under 100ms.** It *keeps* the one strong term the original had (mid drift), *drops* the near-useless imbalance term (univariate AUC 0.63–0.68 vs 0.84–0.87 for the others), and *adds* the two strongest signals. Time-series-CV AUC for predicting next-bar volatility expansion: **0.866 / 0.868 (ES / NQ) vs 0.857 / 0.856** for the original — a consistent +0.009 to +0.012. Ships with a self-test; recalibrate weights on the target venue's own book.

**Adverse-selection–honest fill model.** Resting quotes fill *only when price trades through them* — your bid fills when the next bestBid drops below it, leaving you long into a falling market (the toxic fill). This deliberately captures the cost you're exposed to instead of assuming benign mid fills.

**Full economic decomposition.** Every session tracks, separately: liquidity-incentive accrual, maker rebate per fill (`rebate = Θ·size·p·(1−p)`, Θ = −0.0125), realized inventory P&L + **mark-to-market on held inventory**, and taker fees paid to flatten on kills.

**Real-time API integration.** Keyless pipelines against Polymarket (CLOB / gateway BBO / incentives) and Kalshi (REST/WS).

---

## Engineering notes (the part I'm proudest of)

- **Caught a P&L sign-inversion by reconciliation.** The realized-inventory accumulator had an inverted sign that silently negated every inventory result. I found it by replaying one market's fills through independent cashflow accounting (`model = −162`, `true cashflow = +162` — exact negatives), fixed the one-line convention bug, and added a self-check. See [`src/reconcile.py`](src/reconcile.py) — the standalone version of that check. The lesson: never trust a derived P&L number you haven't reconciled against raw cashflows.
- **Verified exchange conventions empirically, not from docs.** Confirmed the data feed's trade-aggressor side encoding via the tick rule (96% of upticks were one side) *before* trusting the aggressive-flow signal — which turned out to be inverted relative to the docs.
- **Mark-to-market, not realized-only**, so the reported net can't hide risk sitting on the book.
- **Domain due-diligence on the incentive itself.** Reverse-engineered the liquidity-incentive scoring (discount-factor × resting size vs. target, per-second sampling, pro-rata pool) and distinguished the offshore (Polygon) paid-to-rest program from the US-regulated venue's incentive structure — the "is this reward actually real, live, and accessible?" question that matters more than the quoting logic.

---

## Repo structure

```
event-contract-market-maker/
├── README.md
├── requirements.txt
└── src/
    ├── quoter.py         # the engine: state machine, toxicity kill-switch, honest fills, economics
    ├── toxicity.py       # order-flow toxicity gauge (arrival + mid-range + fleeting-order fraction), ES/NQ-validated
    ├── size_scaling.py   # net-vs-capital curve (capacity + market-impact model)
    └── reconcile.py      # cashflow reconciliation — the sign-bug catcher (runnable, self-testing)
```

## Running it

```bash
pip install -r requirements.txt
python src/quoter.py --seconds 780     # one paper session against live books (keyless)
python src/toxicity.py                 # order-flow toxicity gauge self-test
python src/reconcile.py                # cashflow reconciliation self-test
python src/size_scaling.py             # net/$ scaling curve from accumulated fills
```

No API key required — all market-data endpoints are public/keyless.

---

## What it measures (honest results)

On quiet crypto (BTC) event markets, across a full day including a violent BTC session, **realized adverse selection stayed within cents** — the kill-switch fired through every toxic window and the booth stepped aside rather than getting run over. The toxicity dynamics and the reward/adverse-selection accounting are the real, reusable findings; the specific reward *program* it was sized against turned out to run on the offshore (US-geoblocked) venue, which is itself the most important thing the project surfaced.

**This is research, not a live trading system.** Its value is the measurement discipline: an honest adverse-selection harness, a toxicity kill-switch that demonstrably works, and reconciliation habits that catch the bugs backtests hide.
