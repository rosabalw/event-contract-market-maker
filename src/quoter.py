"""
Polymarket US SHADOW market-making quoter (Phase 1 of QUOTER_SPEC.md).
PAPER ONLY — sends NO real orders, needs NO API key. Runs the full state machine (SCAN/QUOTE/LEAN/KILL)
against the LIVE keyless book and SIMULATES fills honestly, so we measure the one thing we can't derive
analytically: how badly adverse selection bites the incentive drip on the quiet crypto pools. This is the
honest-fill gauntlet (the discipline that killed the grid mirage) applied to the Polymarket booth BEFORE any
real capital.

Honest fill model (BBO-only, deliberately captures adverse selection):
  My resting bid sits at last poll's bestBid; ask at last poll's bestAsk. Next poll:
    - if the new bestBid drops BELOW my bid price -> price traded down THROUGH my bid -> I got hit (bought),
      and I'm now long at a price above the market = an adverse fill (this is exactly the cost we're measuring).
    - if the new bestAsk rises ABOVE my ask price -> I sold into a rising market = adverse the other way.
  Rare in quiet markets (low volume) = I mostly rest and accrue; the fills that DO happen are the toxic ones.

Income streams tracked separately: incentive (accrued per poll from my score-share of the per-second pool),
maker rebate (+0.0125*size*p*(1-p) per fill), spread capture + inventory P&L (realized), taker fees (paid to
flatten on KILL). Reports incentive TWO ways: _book (share vs the CURRENT thin book = today's near-ceiling) and
_floor (share = my_size/targetSize = where it settles once competition fills the target). Truth is between and
decays book->floor as makers arrive.

State persists across scheduled runs in shadow_state.json; fills + per-session P&L append to shadow_quoter_log.csv.
Run continuously:  python shadow_quoter.py --seconds 540   (default 540s = ~9min, fits a 15-min scheduled slot).
Test quickly:      python shadow_quoter.py --seconds 40
x64 Py311. Keyless endpoints need a User-Agent header.
"""
import urllib.request, json, csv, os, sys, time, datetime, collections, re

INC = "https://api.prod.polymarketexchange.com/v1/incentives"
GW = "https://gateway.polymarket.us"
UA = {"User-Agent": "Mozilla/5.0 (shadow-quoter; read-only)"}
STATE = "shadow_state.json"
LOG = "shadow_quoter_log.csv"

# ---- config (QUOTER_SPEC.md defaults) ----
POLL_SEC = 8
MY_SIZE = 100.0
MIN_POOL = 200.0
# CRYPTO-ONLY 2026-07-15: POL/CUL/MAC dropped — after sign-fix they were positive but tiny-reward directional
# noise (POL one-market oscillation, MAC an unrealized paper mark, CUL negligible), NOT a reward-farming edge.
# CRY(BTC) is the only category with a real harvestable reward pool. CLI(weather)/SPR(sports) stay excluded.
ALLOW_CATS = ("CRY",)
PER_CAT = {"CRY": 13}                 # crypto-only
MAX_MARKETS = sum(PER_CAT.values())
MIN_DAYS_TO_RESOLUTION = 3.0          # skip markets resolving within N days — binaries gap to 0/1 near expiry = max adverse
MAX_TOX = 4.0                        # kill threshold (logger toxicity units); calibrate from logged distribution
COOLDOWN = 900                       # 15 min calm before re-arming after a kill
SOFT_CAP = 100.0                     # lean above this |inventory|
HARD_CAP = 300.0                     # one-sided quoting only above this
TAKER_THETA = 0.06                   # taker fee coefficient (to flatten on kill)
MAKER_THETA = 0.0125                 # maker rebate coefficient (earned on fills)


def get(url):
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=20) as r:
            return json.load(r)
    except Exception as e:
        return {"_err": str(e)[:120]}


def val(x):
    if isinstance(x, dict): x = x.get("value", 0)
    try: return float(x)
    except Exception: return 0.0


def days_to_resolution(slug):
    """Parse the MM-DD-YYYY resolution date embedded in cpc-btc-* slugs; return days from now (or inf if unparseable)."""
    m = re.search(r"(\d{2})-(\d{2})-(\d{4})", slug)
    if not m:
        return float("inf")
    try:
        mo, dy, yr = int(m.group(1)), int(m.group(2)), int(m.group(3))
        res = datetime.datetime(yr, mo, dy, tzinfo=datetime.timezone.utc)
        return (res - datetime.datetime.now(datetime.timezone.utc)).total_seconds() / 86400.0
    except Exception:
        return float("inf")


def select_markets():
    """Pick the quiet crypto reward universe. Paginates deep (results are reward-desc). Skips markets
    resolving within MIN_DAYS_TO_RESOLUTION (near-expiry binaries gap to 0/1 = max adverse selection)."""
    out, tok, skipped_near = [], "", 0
    for _ in range(45):
        d = get(INC + "?statuses=active&orderBy=reward&orderDirection=desc&pageSize=200" + (f"&pageToken={tok}" if tok else ""))
        if not isinstance(d, dict) or "programs" not in d: break
        for p in d["programs"]:
            cat = p.get("category", "")
            if cat not in ALLOW_CATS: continue
            if days_to_resolution(p["marketSlug"]) < MIN_DAYS_TO_RESOLUTION:
                skipped_near += 1; continue          # near-expiry -> gaps to 0/1, skip
            for tp in p.get("timePeriods", []):
                if tp.get("status") != "active": continue
                pool = float(tp.get("rewardPool", 0) or 0)
                if pool < MIN_POOL: continue
                st, en = tp.get("start", ""), tp.get("end", "")
                try:
                    days = max((datetime.datetime.fromisoformat(en.replace("Z", "+00:00")) -
                                datetime.datetime.fromisoformat(st.replace("Z", "+00:00"))).total_seconds() / 86400.0, 1.0)
                except Exception:
                    days = 1.0
                out.append(dict(slug=p["marketSlug"], cat=cat, daily=pool / days,
                                target=float(tp.get("targetSize", 0) or 0) or 10000.0))
                break     # one (richest) period per market
        tok = d.get("nextPageToken", "")
        if not tok: break
    # de-dupe by slug, then take the top PER_CAT[cat] of EACH category (so small-pool categories like politics
    # get sampled and tested, not buried under the richer crypto pools)
    seen, bycat = set(), collections.defaultdict(list)
    for m in sorted(out, key=lambda m: -m["daily"]):
        if m["slug"] in seen: continue
        seen.add(m["slug"]); bycat[m["cat"]].append(m)
    sel = []
    for cat, ms in bycat.items():
        sel.extend(ms[:PER_CAT.get(cat, 5)])
    return sel


def bbo(slug):
    md = (get(f"{GW}/v1/markets/{slug}/bbo") or {}).get("marketData")
    if not md: return None
    bb, ba = val(md.get("bestBid")), val(md.get("bestAsk"))
    if not (bb and ba and ba > bb): return None
    return dict(bb=bb, ba=ba, mid=(bb + ba) / 2, bd=float(md.get("bidDepth", 0) or 0),
                ad=float(md.get("askDepth", 0) or 0), shares=val(md.get("sharesTraded")))


def load_state():
    if os.path.exists(STATE):
        try: return json.load(open(STATE, encoding="utf-8"))
        except Exception: pass
    return dict(mkts={}, pnl=dict(incentive_book=0.0, incentive_floor=0.0, rebate=0.0,
                                  inventory=0.0, taker_fees=0.0), polls=0, fills=0, kills=0, started="")


def apply_fill(m, side, px, sz):
    """update inventory + realized P&L (weighted avg cost, handles sign flips). returns realized pnl."""
    inv, cost = m["inv"], m["cost"]
    signed = sz if side == "buy" else -sz
    realized = 0.0
    if inv == 0 or (inv > 0) == (signed > 0):                 # adding to / opening position
        m["cost"] = (cost * abs(inv) + px * sz) / (abs(inv) + sz)
        m["inv"] = inv + signed
    else:                                                     # reducing / flipping
        closed = min(sz, abs(inv))
        realized = (px - cost) * closed * (-1 if inv < 0 else 1)   # SIGN-FIX 2026-07-15: short (inv<0) profits when px<cost -> (cost-px); long profits when px>cost -> (px-cost). Prior code had this inverted, negating all realized inventory P&L.
        m["inv"] = inv + signed
        if (inv > 0) != (m["inv"] > 0) and m["inv"] != 0:     # flipped through zero
            m["cost"] = px
    return realized


def main():
    secs = 540
    if "--seconds" in sys.argv:
        try: secs = int(sys.argv[sys.argv.index("--seconds") + 1])
        except Exception: pass
    try: sys.stdout.reconfigure(encoding="utf-8")
    except Exception: pass

    S = load_state()
    now_iso = datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0).isoformat()
    if not S.get("started"): S["started"] = now_iso
    markets = select_markets()
    if not markets:
        print("no markets selected"); return
    for m in markets:
        S["mkts"].setdefault(m["slug"], dict(inv=0.0, cost=0.0, bid=None, ask=None, state="SCAN",
                                             kill_until=0.0, last_mid=None, last_shares=None))
    sess = dict(incentive_book=0.0, incentive_floor=0.0, rebate=0.0, inventory=0.0, unrealized=0.0, taker_fees=0.0, fills=0, kills=0)
    catp = collections.defaultdict(lambda: dict(incentive_floor=0.0, inventory=0.0, unrealized=0.0, rebate=0.0,
                                                taker_fees=0.0, fills=0, kills=0))   # per-category split
    fill_log = []
    t_end = time.time() + secs
    print(f"[{now_iso}] SHADOW quoter — {len(markets)} markets, {secs}s session, poll {POLL_SEC}s (PAPER, no orders)")

    while time.time() < t_end:
        tnow = time.time()
        for m in markets:
            slug, daily, target, cat = m["slug"], m["daily"], m["target"], m["cat"]
            st = S["mkts"][slug]
            b = bbo(slug)
            if not b: continue
            # toxicity (same shape as the logger)
            drift = (b["mid"] - st["last_mid"]) if st["last_mid"] is not None else 0.0
            vol = max((b["shares"] - st["last_shares"]), 0.0) if st["last_shares"] is not None else 0.0
            tot = b["bd"] + b["ad"]; imb = (b["bd"] - b["ad"]) / tot if tot else 0.0
            tox = abs(imb) * 3.0 + abs(drift) * 100.0 + vol / 100.0

            # ---- KILL / re-arm ----
            if st["state"] == "KILL":
                if tnow >= st["kill_until"] and tox < MAX_TOX:
                    st["state"] = "QUOTE"
            elif tox >= MAX_TOX and st["state"] in ("QUOTE", "LEAN"):
                if st["inv"] != 0:                            # flatten via taker at mid, pay taker fee
                    px = b["mid"]; sz = abs(st["inv"]); side = "sell" if st["inv"] > 0 else "buy"
                    r = apply_fill(st, side, px, sz)
                    fee = TAKER_THETA * sz * px * (1 - px)
                    sess["inventory"] += r; sess["taker_fees"] += fee
                    catp[cat]["inventory"] += r; catp[cat]["taker_fees"] += fee
                    fill_log.append((now_iso, cat, slug, "KILL-flat", side, round(px, 3), sz, round(r, 3), round(-fee, 3), round(vol, 1), round(b["bd"], 1), round(b["ad"], 1)))
                st["bid"] = st["ask"] = None
                st["state"] = "KILL"; st["kill_until"] = tnow + COOLDOWN
                sess["kills"] += 1; catp[cat]["kills"] += 1

            # ---- fills against LAST poll's resting quotes ----
            if st["state"] in ("QUOTE", "LEAN"):
                if st["bid"] is not None and b["bb"] < st["bid"]:     # price traded DOWN through my bid -> filled (adverse long)
                    r = apply_fill(st, "buy", st["bid"], MY_SIZE)
                    reb = MAKER_THETA * MY_SIZE * st["bid"] * (1 - st["bid"])
                    sess["inventory"] += r; sess["rebate"] += reb; sess["fills"] += 1
                    catp[cat]["inventory"] += r; catp[cat]["rebate"] += reb; catp[cat]["fills"] += 1
                    fill_log.append((now_iso, cat, slug, "bid", "buy", round(st["bid"], 3), MY_SIZE, round(r, 3), round(reb, 3), round(vol, 1), round(b["bd"], 1), round(b["ad"], 1)))
                    st["bid"] = None
                if st["ask"] is not None and b["ba"] > st["ask"]:     # price traded UP through my ask -> filled
                    r = apply_fill(st, "sell", st["ask"], MY_SIZE)
                    reb = MAKER_THETA * MY_SIZE * st["ask"] * (1 - st["ask"])
                    sess["inventory"] += r; sess["rebate"] += reb; sess["fills"] += 1
                    catp[cat]["inventory"] += r; catp[cat]["rebate"] += reb; catp[cat]["fills"] += 1
                    fill_log.append((now_iso, cat, slug, "ask", "sell", round(st["ask"], 3), MY_SIZE, round(r, 3), round(reb, 3), round(vol, 1), round(b["bd"], 1), round(b["ad"], 1)))
                    st["ask"] = None

            # ---- accrue incentive (quotes rested during this interval) + re-quote ----
            if st["state"] in ("QUOTE", "LEAN") or (st["state"] == "SCAN"):
                if st["state"] == "SCAN":
                    st["state"] = "QUOTE"
                depth = min(b["bd"], b["ad"])
                share_book = MY_SIZE / (MY_SIZE + depth) if (MY_SIZE + depth) else 0.0     # vs current thin book
                share_floor = min(1.0, MY_SIZE / target)                                    # vs target (settled)
                rate = daily / 86400.0 * POLL_SEC
                # accrue only if I actually have resting quotes (two-sided) this interval
                if st["bid"] is not None and st["ask"] is not None:
                    sess["incentive_book"] += share_book * rate
                    sess["incentive_floor"] += share_floor * rate
                    catp[cat]["incentive_floor"] += share_floor * rate
                # LEAN: above soft cap, stop quoting the inventory-adding side
                st["state"] = "LEAN" if abs(st["inv"]) > SOFT_CAP else "QUOTE"
                st["bid"] = b["bb"] if st["inv"] < HARD_CAP else None
                st["ask"] = b["ba"] if st["inv"] > -HARD_CAP else None
                if st["state"] == "LEAN":                     # lean: drop the adding side
                    if st["inv"] > 0: st["bid"] = None
                    else: st["ask"] = None
            st["last_mid"] = b["mid"]; st["last_shares"] = b["shares"]
        S["polls"] += 1
        time.sleep(POLL_SEC)

    # ---- mark OPEN inventory to market (unrealized P&L on held positions) BEFORE clearing last_mid ----
    # honest net must count risk on inventory we still hold, not just realized (closed) fills.
    # unrealized = inv * (mid - cost): long(inv>0) gains if mid>cost; short(inv<0) gains if mid<cost. Correct for both signs.
    slug2cat = {m["slug"]: m["cat"] for m in markets}
    for slug, st in S["mkts"].items():
        if st["inv"] and st["last_mid"] is not None:
            u = st["inv"] * (st["last_mid"] - st["cost"])
            sess["unrealized"] += u
            c = slug2cat.get(slug)
            if c: catp[c]["unrealized"] += u

    # ---- roll session into cumulative state + write logs ----
    for st in S["mkts"].values():                # cancel resting quotes between sessions (offline MM holds no live orders,
        st["bid"] = st["ask"] = None             # only inventory) -> avoids stale phantom fills across the scheduling gap
        st["last_mid"] = st["last_shares"] = None
    for k in ("incentive_book", "incentive_floor", "rebate", "inventory", "taker_fees"):
        S["pnl"][k] += sess[k]
    S["fills"] += sess["fills"]; S["kills"] += sess["kills"]
    json.dump(S, open(STATE, "w", encoding="utf-8"))
    net_book = sess["incentive_book"] + sess["rebate"] + sess["inventory"] - sess["taker_fees"]
    net_floor = sess["incentive_floor"] + sess["rebate"] + sess["inventory"] - sess["taker_fees"]
    fresh = not os.path.exists(LOG)
    with open(LOG, "a", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        if fresh:
            w.writerow(["ts", "session_s", "markets", "polls", "fills", "kills",
                        "incentive_book", "incentive_floor", "rebate", "inventory_pnl", "open_unreal_mtm", "taker_fees",
                        "net_book", "net_floor",
                        "cum_incentive_book", "cum_incentive_floor", "cum_rebate", "cum_inventory", "cum_taker",
                        "open_inventory_ct"])
        open_inv = sum(abs(v["inv"]) for v in S["mkts"].values())
        w.writerow([now_iso, secs, len(markets), S["polls"], sess["fills"], sess["kills"],
                    round(sess["incentive_book"], 3), round(sess["incentive_floor"], 3), round(sess["rebate"], 3),
                    round(sess["inventory"], 3), round(sess["unrealized"], 3), round(sess["taker_fees"], 3),
                    round(net_book, 3), round(net_floor, 3),
                    round(S["pnl"]["incentive_book"], 3), round(S["pnl"]["incentive_floor"], 3),
                    round(S["pnl"]["rebate"], 3), round(S["pnl"]["inventory"], 3), round(S["pnl"]["taker_fees"], 3),
                    round(open_inv, 1)])
    if fill_log:
        fresh_f = not os.path.exists("shadow_fills.csv")
        with open("shadow_fills.csv", "a", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            if fresh_f: w.writerow(["ts", "cat", "slug", "kind", "side", "px", "size", "realized_pnl", "rebate_or_fee", "vol", "bid_depth", "ask_depth"])
            w.writerows(fill_log)
    # ---- per-category split log (the crypto-vs-weather verdict for EACH category) ----
    ncat = collections.Counter(m["cat"] for m in markets)
    fresh_c = not os.path.exists("shadow_category_log.csv")
    with open("shadow_category_log.csv", "a", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        if fresh_c: w.writerow(["ts", "cat", "n_markets", "incentive_floor", "realized_inv", "open_unreal_mtm",
                                "rebate", "taker_fees", "net_flow", "net_mtm", "fills", "kills"])
        for cat, c in sorted(catp.items()):
            net_flow = c["incentive_floor"] + c["rebate"] + c["inventory"] - c["taker_fees"]   # recurring income rate (realized only)
            net_mtm = net_flow + c["unrealized"]                                                # honest net incl. mark on held inventory
            w.writerow([now_iso, cat, ncat.get(cat, 0), round(c["incentive_floor"], 3), round(c["inventory"], 3),
                        round(c["unrealized"], 3), round(c["rebate"], 3), round(c["taker_fees"], 3),
                        round(net_flow, 3), round(net_mtm, 3), c["fills"], c["kills"]])
    dur_h = secs / 3600.0
    print(f"  session: polls~{secs//POLL_SEC} fills {sess['fills']} kills {sess['kills']}")
    open_inv_ct = sum(abs(v["inv"]) for v in S["mkts"].values())
    net_mtm_floor = net_floor + sess["unrealized"]
    print(f"  incentive_floor ${sess['incentive_floor']:+.2f}  rebate ${sess['rebate']:+.2f}"
          f"  realized_inv ${sess['inventory']:+.2f}  open_MTM ${sess['unrealized']:+.2f}  taker ${sess['taker_fees']:.2f}")
    print(f"  NET(flow, realized) floor ${net_floor:+.2f} -> ${net_floor/dur_h*24:+.0f}/day"
          f"   |   NET(mark-to-market, incl held inv) floor ${net_mtm_floor:+.2f}")
    print(f"  >>> GROSS incentive is the reliable stream; NET depends on realized_inv + open_MTM (${sess['inventory']+sess['unrealized']:+.2f}) — open inv {open_inv_ct:.0f} ct still carries risk <<<")


if __name__ == "__main__":
    main()
