"""
toxicity.py — order-flow toxicity gauge for the DMM quoting engine's kill-switch.

WHAT IT IS
    A streaming detector of *imminent volatility expansion* — the condition under which a resting
    two-sided quote is about to be run over by informed flow. When it fires, the quoter should widen
    or pull (step aside before the adverse fill lands), which is exactly what keeps adverse selection
    near zero on a market-making book.

WHY THESE FEATURES (empirical, not hand-tuned)
    The engine's original toxicity score was a hand-weighted composite |imbalance|*3 + |drift|*100 +
    volume/100. A head-to-head on real order-level MBO data (22 ES sessions + 23 NQ sessions, 5-min
    bars, predicting whether the NEXT bar lands in the top tercile of range) showed:

        feature (univariate AUC)      ES       NQ
        arrival (order intensity)   0.852    0.868   <- strongest
        fleeting_frac (<100ms)      0.843    0.864
        mid_range (drift)           0.846    0.842
        volume                      0.841    0.851
        imbalance                   0.625    0.682   <- the old gauge's main term, and the WEAKEST

        gauge (time-series CV AUC)    ES       NQ
        current {imb,drift,vol}     0.857    0.856
        TRIO {arrival,range,fleet}  0.866    0.868   <- this module (+0.009 / +0.012 over current)

    So this gauge KEEPS the one strong term the old one had (mid drift/range), DROPS the near-useless
    imbalance term, and ADDS the two strongest signals — order-arrival intensity and the fraction of
    orders that flicker and cancel in under 100ms (HFT probing / quote instability). Weights are a
    logistic fit on per-instrument-standardized features pooled across ES+NQ:

        tox_logit = 0.300*z(arrival) + 0.986*z(mid_range) + 0.920*z(fleeting_frac) - 0.832

    z() is standardization against a rolling history, so the gauge adapts to each instrument's own
    scale (essential — it will be deployed on a brand-new compute-futures book with unknown activity
    levels; recalibrate the weights on that book's own data once there is history).

REQUIRES order-level (MBO / L3) feed data for the fleeting_frac and arrival terms. If the venue feed
is book-only (L2, no order IDs), those terms degrade to neutral (z=0) and the gauge falls back to the
mid-range term alone — still useful, but the edge over the old gauge comes from the order-level terms,
so confirm the feed exposes order IDs.
"""
from __future__ import annotations
import collections, math

# --- validated weights (per-instrument z-scored logistic, ES+NQ pooled) ---
W_ARRIVAL = 0.300
W_RANGE = 0.986
W_FLEET = 0.920
INTERCEPT = -0.832

FLEET_NS = 100_000_000            # 100 ms — an order dying faster than this is "fleeting"
DEFAULT_WINDOW_NS = 300 * 1_000_000_000   # 5-min feature window (matches calibration bar)
DEFAULT_HISTORY = 288            # rolling samples for standardization (~1 trading day of 5-min bars)
WARMUP = 30                      # samples before the gauge is trusted (std unreliable before this)


class _Roll:
    """Rolling mean/std over the last `n` pushed values (Welford-free, simple + robust)."""
    def __init__(self, n):
        self.buf = collections.deque(maxlen=n)
    def push(self, x):
        if x is not None and not (isinstance(x, float) and math.isnan(x)):
            self.buf.append(x)
    def z(self, x):
        if x is None or len(self.buf) < WARMUP:
            return 0.0
        m = sum(self.buf) / len(self.buf)
        var = sum((v - m) ** 2 for v in self.buf) / len(self.buf)
        sd = math.sqrt(var)
        return 0.0 if sd < 1e-12 else (x - m) / sd
    def ready(self):
        return len(self.buf) >= WARMUP


class OrderFlowToxicityGauge:
    """
    Feed it order-level events and periodic BBO mids; call score()/should_kill() to gate quoting.

        g = OrderFlowToxicityGauge()
        g.on_add(oid, ts_ns); g.on_remove(oid, ts_ns)      # order lifecycle (MBO)
        g.on_book(mid, ts_ns)                               # BBO mid samples
        if g.should_kill(ts_ns): pull_quotes()

    All timestamps are integer nanoseconds. Thread-unsafe by design (one gauge per instrument thread).
    """
    def __init__(self, window_ns=DEFAULT_WINDOW_NS, history=DEFAULT_HISTORY, kill_prob=0.66):
        self.window = window_ns
        self.kill_prob = kill_prob            # default 0.66 ~ the top-tercile expansion the fit targets
        self._births = {}                     # order_id -> birth ts
        self._adds = collections.deque()      # add timestamps in window (arrival)
        self._deaths = collections.deque()    # (death_ts, is_fleeting) in window
        self._mids = collections.deque()      # (ts, mid) in window
        self._seen_orders = False             # False -> book-only feed, degrade order-level terms
        self._roll_arr = _Roll(history)
        self._roll_rng = _Roll(history)
        self._roll_flt = _Roll(history)

    # ---- event hooks ----
    def on_add(self, order_id, ts):
        self._seen_orders = True
        self._births[order_id] = ts
        self._adds.append(ts)

    def on_remove(self, order_id, ts):
        """Order left the book (cancel or fill) — record its lifetime for the fleeting fraction."""
        b = self._births.pop(order_id, None)
        if b is not None:
            self._deaths.append((ts, 1 if (ts - b) < FLEET_NS else 0))

    def on_book(self, mid, ts):
        if mid is not None:
            self._mids.append((ts, mid))

    # ---- internals ----
    def _evict(self, now):
        cut = now - self.window
        while self._adds and self._adds[0] < cut:
            self._adds.popleft()
        while self._deaths and self._deaths[0][0] < cut:
            self._deaths.popleft()
        while self._mids and self._mids[0][0] < cut:
            self._mids.popleft()

    def _raw(self, now):
        self._evict(now)
        arrival = float(len(self._adds)) if self._seen_orders else None
        if self._seen_orders and self._deaths:
            fleeting = sum(f for _, f in self._deaths) / len(self._deaths)
        else:
            fleeting = None
        if self._mids:
            ms = [m for _, m in self._mids]
            mid_range = max(ms) - min(ms)
        else:
            mid_range = None
        return arrival, mid_range, fleeting

    # ---- scoring ----
    def score(self, now, learn=True):
        """Return P(imminent expansion) in [0,1]. `learn` pushes the current features into the
        rolling standardizer (call with learn=False for a read-only probe)."""
        arrival, mid_range, fleeting = self._raw(now)
        za = self._roll_arr.z(arrival)
        zr = self._roll_rng.z(mid_range)
        zf = self._roll_flt.z(fleeting)
        if learn:
            self._roll_arr.push(arrival)
            self._roll_rng.push(mid_range)
            self._roll_flt.push(fleeting)
        logit = INTERCEPT + W_ARRIVAL * za + W_RANGE * zr + W_FLEET * zf
        return 1.0 / (1.0 + math.exp(-logit))

    def ready(self):
        """True once the standardizer has enough history to be trusted."""
        return self._roll_rng.ready()

    def should_kill(self, now, threshold=None):
        """Kill/step-aside signal. Never fires during warm-up (std not yet trustworthy)."""
        if not self.ready():
            return False
        return self.score(now) >= (self.kill_prob if threshold is None else threshold)


if __name__ == "__main__":
    # Self-test: a long calm regime, then a toxic burst (order storm + fast cancels + price run).
    # The gauge should sit low through calm and cross the kill line in the burst.
    S = 1_000_000_000
    g = OrderFlowToxicityGauge()
    t = 0
    oid = 0
    calm_scores, burst_scores = [], []
    # 120 calm 5-min windows: modest arrivals, few fleeting, tiny mid moves
    for w in range(120):
        base = t
        for k in range(50):                       # 50 adds/window
            oid += 1; g.on_add(oid, base + k * S)
            g.on_remove(oid, base + k * S + 500_000_000)   # 500ms life -> not fleeting
        for k in range(60):
            g.on_book(100.0 + (k % 3) * 0.25, base + k * S)   # ~0.5 range
        t += 300 * S
        s = g.score(t)
        if g.ready():
            calm_scores.append(s)
    # 6 toxic windows: 10x arrivals, mostly fleeting (<100ms), big price run
    for w in range(6):
        base = t
        for k in range(500):                      # order storm
            oid += 1; g.on_add(oid, base + k * 100_000_000)
            g.on_remove(oid, base + k * 100_000_000 + 20_000_000)  # 20ms -> fleeting
        for k in range(60):
            g.on_book(100.0 + k * 0.5, base + k * S)          # ~30 range, price running
        t += 300 * S
        burst_scores.append(g.score(t))
    cmax = max(calm_scores); bmax = max(burst_scores)
    print("calm  max P(toxic) = %.3f" % cmax)
    print("burst max P(toxic) = %.3f" % bmax)
    assert cmax < 0.5, "calm regime should stay well below the kill line"
    assert bmax > g.kill_prob, "toxic burst should cross the kill line"
    print("PASS — gauge stays calm in benign flow and fires on the toxic burst.")
