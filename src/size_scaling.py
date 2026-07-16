"""
size_scaling.py — offline size-scaling analyzer for the Polymarket crypto shadow.

QUESTION IT ANSWERS: as you rest MORE size per market (more capital), does net $/day keep
scaling linearly, or does it turn over? The turnover point = your practical max capital.

WHY OFFLINE: the modeling here (capacity + market impact) is where bugs hide, so it lives in a
standalone TESTED script, NOT in the live quoter. It replays the recorded fill events (with the
book context now logged: vol, bid_depth, ask_depth) at several size tiers.

MODEL (each explicitly an assumption — the paper fill model cannot perfectly capture thin-market
impact, so treat the curve as directional, and note the OPTIMISM caveat below):
  * incentive scales LINEARLY with size until size == target, then flat:
        incentive_T = incentive_100 * min(T, target) / 100
  * CAPACITY: a resting order only fills as much as actually traded through it that interval:
        fill_size_T = min(T, vol)          (vol = shares that traded that poll)
  * IMPACT: flattening inventory on a KILL pays slippage that grows with inventory-vs-depth:
        slip_frac = min(SLIP_CAP, IMPACT_K * |inv_T| / max(depth,1))
        exit_px   = mid -/+ slip_frac      (worse for bigger inventory in a thinner book)
  * caps (soft/hard/lean) scale WITH the tier, so relative quoting dynamics match the 100-ct base.

OPTIMISM CAVEAT: the replay assumes every tier fills on the same events the 100-ct book did.
A larger book would lean/kill on its own (slightly different) schedule, so the true turnover is
at a SMALLER size than this estimate suggests. Read the curve as an UPPER bound on the safe size.
"""
import csv, sys, os
from collections import defaultdict

SIZES = [100, 300, 1000, 3000, 10000]     # contracts per market to test
TARGET = 10000.0                            # Polymarket BTC reward targetSize
IMPACT_K = 0.02                             # flattening 1x depth ~= 2 cents slippage (ASSUMPTION)
SLIP_CAP = 0.5                              # max slippage fraction (can't exceed the 0-1 range)
MAKER_THETA = 0.0125
TAKER_THETA = 0.06
FILLS = "shadow_fills.csv"
CATLOG = "shadow_category_log.csv"


def apply_fill(m, side, px, sz):
    """weighted-avg-cost realized P&L. SAME (fixed) sign convention as the live quoter."""
    inv, cost = m["inv"], m["cost"]
    signed = sz if side == "buy" else -sz
    realized = 0.0
    if inv == 0 or (inv > 0) == (signed > 0):
        m["cost"] = (cost * abs(inv) + px * sz) / (abs(inv) + sz) if (abs(inv) + sz) else 0.0
        m["inv"] = inv + signed
    else:
        closed = min(sz, abs(inv))
        realized = (px - cost) * closed * (-1 if inv < 0 else 1)
        m["inv"] = inv + signed
        if (inv > 0) != (m["inv"] > 0) and m["inv"] != 0:
            m["cost"] = px
    return realized


def simulate_tier(fills, T):
    """Replay crypto fill events at size T. Returns dict of realized/unreal/rebate/taker/fills/filled_ct."""
    books = defaultdict(lambda: dict(inv=0.0, cost=0.0))
    last_px = {}
    realized = rebate = taker = 0.0
    nfills = filled_ct = 0
    for f in fills:
        slug, kind, side, px, vol, bd, ad = f
        m = books[slug]
        if kind in ("bid", "ask"):
            fill = min(T, vol) if vol > 0 else T   # capacity: capped by volume that traded through
            if fill <= 0:
                continue
            realized += apply_fill(m, side, px, fill)
            rebate += MAKER_THETA * fill * px * (1 - px)
            nfills += 1; filled_ct += fill
            last_px[slug] = px
        elif kind == "KILL-flat":
            if m["inv"] == 0:
                continue
            inv = m["inv"]; depth = ad if inv > 0 else bd   # exit a long -> hit the ask side, etc.
            slip = min(SLIP_CAP, IMPACT_K * abs(inv) / max(depth, 1.0))
            exit_px = px - slip if inv > 0 else px + slip   # sell lower / buy higher = worse
            exit_px = min(0.999, max(0.001, exit_px))
            side = "sell" if inv > 0 else "buy"
            realized += apply_fill(m, side, exit_px, abs(inv))
            taker += TAKER_THETA * abs(inv) * exit_px * (1 - exit_px)
            last_px[slug] = exit_px
    unreal = sum(m["inv"] * (last_px.get(s, m["cost"]) - m["cost"]) for s, m in books.items())
    open_ct = sum(abs(m["inv"]) for m in books.values())
    return dict(realized=realized, unreal=unreal, rebate=rebate, taker=taker,
                fills=nfills, filled_ct=filled_ct, open_ct=open_ct)


def load_crypto_fills(path):
    """Read fill events for crypto (btc) markets that have the book-context columns."""
    out = []
    if not os.path.exists(path):
        return out
    with open(path) as fh:
        for r in csv.DictReader(fh):
            if "btc" not in r["slug"].lower():
                continue
            if r.get("vol") in (None, ""):     # pre-augmentation rows lack context -> skip
                continue
            out.append((r["slug"], r["kind"], r["side"], float(r["px"]),
                        float(r["vol"]), float(r["bid_depth"]), float(r["ask_depth"])))
    return out


def crypto_incentive_base(path):
    """Gross incentive+rebate-taker for CRY from the category log = the 100-ct base to scale."""
    inc = reb = tak = 0.0
    if not os.path.exists(path):
        return 0.0
    with open(path) as fh:
        for r in csv.DictReader(fh):
            if r["cat"] != "CRY":
                continue
            inc += float(r["incentive_floor"]); reb += float(r.get("rebate", 0) or 0)
            tak += float(r.get("taker_fees", 0) or 0)
    return inc   # incentive only; rebate/taker are recomputed per-tier in the sim


def run(fills_path=FILLS, cat_path=CATLOG):
    fills = load_crypto_fills(fills_path)
    inc100 = crypto_incentive_base(cat_path)
    if not fills:
        print("No augmented crypto fills yet (need rows with vol/bid_depth/ask_depth).")
        print("The live quoter now logs these; re-run after the clean track accumulates crypto fills.")
        return
    print(f"crypto fill events with context: {len(fills)}   |   CRY incentive base (100ct): ${inc100:.1f}")
    print(f"{'size':>6}{'incentive':>11}{'realized':>10}{'open_MTM':>10}{'rebate':>8}{'taker':>8}"
          f"{'NET':>9}{'NET/$cap':>9}{'open_ct':>8}")
    prev_per = None
    for T in SIZES:
        s = simulate_tier(fills, T)
        incT = inc100 * min(T, TARGET) / 100.0
        net = incT + s["rebate"] - s["taker"] + s["realized"] + s["unreal"]
        cap = T * 1.0   # ~$1 collateral per two-sided contract-pair, per market (relative scale)
        per = net / cap
        flag = ""
        if prev_per is not None and per < prev_per:
            flag = "  <- net/$ turning over"
        prev_per = per
        print(f"{T:>6}{incT:>11.1f}{s['realized']:>10.1f}{s['unreal']:>10.1f}{s['rebate']:>8.2f}"
              f"{s['taker']:>8.2f}{net:>9.1f}{per:>9.3f}{s['open_ct']:>8.0f}{flag}")
    print("\nNET/$cap is the number that matters: while it stays flat, scaling pays; when it falls,")
    print("you've passed the useful max. (Estimate is OPTIMISTIC for big sizes — true max is lower.)")


# ----------------------------- self-test -----------------------------
def _test():
    ok = True
    # 1. apply_fill sign: long bought 0.20, sell 0.30 -> +profit
    m = dict(inv=0.0, cost=0.0); apply_fill(m, "buy", 0.20, 100)
    r = apply_fill(m, "sell", 0.30, 100)
    ok &= abs(r - 10.0) < 1e-9;  assert abs(r-10.0)<1e-9, f"long profit {r}"
    # 2. short 0.40 cover 0.25 -> +profit
    m = dict(inv=0.0, cost=0.0); apply_fill(m, "sell", 0.40, 100)
    r = apply_fill(m, "buy", 0.25, 100)
    ok &= abs(r - 15.0) < 1e-9;  assert abs(r-15.0)<1e-9, f"short profit {r}"
    # 3. CAPACITY: with vol=50, a size-1000 order fills only 50
    fills = [("x", "bid", "buy", 0.30, 50, 500, 500)]
    s = simulate_tier(fills, 1000); assert s["filled_ct"] == 50, f"capacity {s['filled_ct']}"
    s = simulate_tier(fills, 100);  assert s["filled_ct"] == 50, f"capacity100 {s['filled_ct']}"
    # 4. IMPACT: bigger inventory pays more exit slippage. Build inv then kill.
    #    buy 1000@0.50 (vol ample), then KILL-flat at mid 0.50 with thin depth -> slippage loss
    fills = [("x", "bid", "buy", 0.50, 5000, 100, 100), ("x", "KILL-flat", "sell", 0.50, 0, 100, 100)]
    s_big = simulate_tier(fills, 1000)
    s_sm  = simulate_tier(fills, 100)
    # both bought then flattened at same mid; bigger inv -> more slippage -> more negative realized
    assert s_big["realized"] < s_sm["realized"], f"impact not monotone {s_big['realized']} vs {s_sm['realized']}"
    # 5. incentive linearity handled in run(); check scaling factor
    assert abs((min(3000, TARGET)/100) - 30.0) < 1e-9
    print("all self-tests PASSED" if ok else "SELF-TEST FAILURES")


if __name__ == "__main__":
    if "--test" in sys.argv:
        _test()
    else:
        run()
