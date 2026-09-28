"""Minimal causal-window relaxation repair on top of a frozen **unsat** audit.

An auditor reads an unsat wraparound-timestamp audit and submits a *repair
request* (tagged with a ``repair_id``).  The service widens the original
causal windows by the minimum total integer amount that lets the recorded
counter readings admit one real timeline, without rewriting the source audit
or its read result.

Optimization model
------------------
For a candidate wrap-count vector ``k`` (anchor fixed at 0, every event
``k_e >= 0``) constraint ``c: src -> dst = [lo, hi]`` must be widened by

    x_c(k) = max(0, lo - (t_dst - t_src), (t_dst - t_src) - hi)

ticks, where ``t_dst - t_src = r_c + M(k_dst - k_src)`` and ``r_c`` is the
counter residue of the difference (anchor counters are the known anchor
tick).  The objective is

    min  sum_c x_c        over non-negative integer wrap counts.

Each ``x_c`` is a convex piecewise-linear function of the single ordered
difference ``k_dst - k_src`` (two integer ramps meeting at the original
window), so the whole objective is a convex/submodular pairwise energy on
the ordered labels ``k``.  It is solved exactly -- no pseudo-polynomial
search -- as one minimum cut (Ishikawa construction):

  * every event gets an ordered chain of wrap-count label thresholds; a
    threshold on the source side means "wrap count >= that label";
  * each ramp contributes non-negative finite-difference capacities between
    the two endpoint chains and the source/sink boundary columns;
  * ``k_e >= 0`` is the native label domain and the anchor is pinned to 0.

Tight per-node label domains come from propagating the instance in which
every window is widened by the feasible all-``k = 0`` seed cost: that box
contains the all-0 vector and every timeline that can beat the seed, so the
global optimum is inside it (and the graph stays tiny for <= 12 events).

Canonical selection is folded into the cut capacities with mixed-radix
big-integer weights -- the minimum cut simultaneously minimizes

  0. the total extension (most significant),
  1. the relaxation vector in constraint-id order,
  2. the wrap-count / timeline vector in event-id order.

The radix ``B`` exceeds every individual component and each absolute wrap
count, so no place can carry and the lexicographic comparison is exact.

The result reports, per constraint, the original window, the relaxed window,
the outward direction actually consumed (``lower``/``upper``/``none``) and
the recomputed tick difference of the canonical timeline, so every number
can be re-checked against the frozen source.
"""

from __future__ import annotations

import collections
import sys
from collections import deque

from .solver import (
    Constraint,
    _Propagator,
    _build_edges,
    _validate,
    ValidationError,
)

sys.setrecursionlimit(100000)


class SourceNotUnsat(Exception):
    """A repair can only cite an existing audit whose status is ``unsat``."""

    def __init__(self, audit_no, status):
        super().__init__(
            f"source audit {audit_no} has status {status!r}; only 'unsat' "
            f"audits can be repaired")
        self.audit_no = audit_no
        self.status = status


# ---------------------------------------------------------------------------
# Dinic maximum flow (exact integer capacities)
# ---------------------------------------------------------------------------

class _Dinic:
    def __init__(self, n):
        self.n = n
        self.g = [[] for _ in range(n)]

    def add_edge(self, u, v, cap):
        if cap <= 0:
            return
        self.g[u].append([v, cap, len(self.g[v])])
        self.g[v].append([u, 0, len(self.g[u]) - 1])

    def max_flow(self, s, t):
        n, g = self.n, self.g
        flow = 0
        while True:
            level = [-1] * n
            level[s] = 0
            q = deque([s])
            while q:
                u = q.popleft()
                for v, cap, _rev in g[u]:
                    if cap > 0 and level[v] < 0:
                        level[v] = level[u] + 1
                        q.append(v)
            if level[t] < 0:
                self.level = level
                return flow
            it = [0] * n
            # Upper bound on the remaining flow is the source capacity.
            push_bound = sum(e[1] for e in g[s])

            def dfs(u, pushed):
                if u == t:
                    return pushed
                while it[u] < len(g[u]):
                    e = g[u][it[u]]
                    v, cap, rev = e
                    if cap > 0 and level[v] == level[u] + 1:
                        d = dfs(v, min(pushed, cap))
                        if d:
                            e[1] -= d
                            g[v][rev][1] += d
                            return d
                    it[u] += 1
                return 0

            while push_bound:
                f = dfs(s, push_bound)
                if not f:
                    break
                push_bound -= f
                flow += f


# ---------------------------------------------------------------------------
# Convex ramp finite differences
# ---------------------------------------------------------------------------

def _ramp_left(A, M, xs, xd):
    """max(0, A + M*xs - M*xd): the window's lower-end ramp as a function of
    the two endpoint wrap counts."""
    z = A + M * xs - M * xd
    return z if z > 0 else 0


def _ramp_right(B, M, xs, xd):
    """max(0, M*xd - M*xs - B): the window's upper-end ramp."""
    z = M * xd - M * xs - B
    return z if z > 0 else 0


# ---------------------------------------------------------------------------
# Minimum-cut optimizer
# ---------------------------------------------------------------------------

def _seed_amounts(constraints, residues, anchor_tick):
    """Feasible relaxation (all events at k = 0) and its total cost."""
    def tick_of(endpoint):
        return anchor_tick if endpoint == "anchor" else residues[endpoint]

    amounts, total = {}, 0
    for c in constraints:
        delta = tick_of(c.dst) - tick_of(c.src)
        x = max(0, c.lo - delta, delta - c.hi)
        amounts[c.id] = x
        total += x
    return amounts, total


def _residue(c, residues, anchor_tick):
    """Zero-wrap tick difference t_dst - t_src of a constraint."""
    if c.src == "anchor":
        return residues[c.dst] - anchor_tick
    if c.dst == "anchor":
        return anchor_tick - residues[c.src]
    return residues[c.dst] - residues[c.src]


def _cost_of_k(constraints, residues, anchor_tick, M, kvec):
    """Total window violation of one non-negative wrap-count vector."""
    def tick_of(endpoint):
        return anchor_tick if endpoint == "anchor" \
            else residues[endpoint] + M * kvec[endpoint]

    total = 0
    for c in constraints:
        delta = tick_of(c.dst) - tick_of(c.src)
        total += max(0, c.lo - delta, delta - c.hi)
    return total


def _tree_seed_cost(constraints, residues, anchor_tick, M, event_ids):
    """Cost of a scale-independent feasible wrap-count vector.

    Walk a spanning tree rooted at the anchor and, for every tree edge,
    assign the child the grid wrap difference closest to the edge window
    (a tree edge is then violated by at most M/2 ticks, however large the
    window numbers are).  Non-tree edges are bounded by the accumulated
    path error, so the total stays O(path-length * M), independent of the
    absolute window scale.  Wrap counts are clamped to the physical
    k_e >= 0 domain.
    """
    adj = collections.defaultdict(list)
    for c in constraints:
        adj[c.src].append((c.dst, c))
        adj[c.dst].append((c.src, c))

    kvec = {"anchor": 0}
    queue = collections.deque(["anchor"])
    seen = {"anchor"}
    while queue:
        parent = queue.popleft()
        for child, c in adj[parent]:
            if child in seen:
                continue
            seen.add(child)
            queue.append(child)
            r = _residue(c, residues, anchor_tick)
            # t_dst - t_src = r + M*(k_dst - k_src); the smallest grid
            # difference >= lo is d1, the next smaller is d1 - 1; one of
            # the two is closest to the window.
            d1 = -(-(c.lo - r) // M)  # ceil
            if c.src == parent and c.dst == child:
                candidates = [kvec[parent] + d1, kvec[parent] + d1 - 1]
            else:  # edge runs child -> parent
                candidates = [kvec[parent] - d1, kvec[parent] - d1 + 1]
            best_k, best_viol = None, None
            for kv in candidates:
                kv = max(0, kv)
                k_trial = dict(kvec)
                k_trial[child] = kv
                viol = _cost_of_k([c], residues, anchor_tick, M, k_trial)
                if best_viol is None or viol < best_viol:
                    best_k, best_viol = kv, viol
            kvec[child] = best_k

    k_events = {e: kvec.get(e, 0) for e in event_ids}
    return _cost_of_k(constraints, residues, anchor_tick, M, k_events)


def optimize(modulus, anchor_tick, events, constraints):
    """Exact minimum relaxation via one minimum cut.

    Returns ``(amounts, total, kvec, event_ids, residues)``.
    """
    M = modulus
    residues = {e["id"]: e["counter"] for e in events}
    event_ids = sorted(residues)
    constraints = sorted(constraints, key=lambda c: c.id)
    m, n = len(constraints), len(event_ids)

    _seed, seed0 = _seed_amounts(constraints, residues, anchor_tick)
    assert seed0 > 0, "repair() must only be called on an unsat source"
    tree_seed = _tree_seed_cost(constraints, residues, anchor_tick,
                                M, event_ids)

    # Tight per-node label domains.  Relaxing every window uniformly by S
    # makes the propagated box contain every timeline whose total violation
    # is <= S (each individual violation is then <= S), hence it contains a
    # global optimum.  Prefer the smaller scale-independent tree-seed cost,
    # but only if its uniformly relaxed instance is itself feasible;
    # otherwise fall back to the all-k=0 seed cost (always feasible).
    def label_box(S):
        wide = [Constraint(c.id, c.src, c.dst, c.lo - S, c.hi + S)
                for c in constraints]
        bb, ee, _r = _build_edges(M, anchor_tick, events, wide)
        pp = _Propagator(bb, ee, track=False)
        if pp.run():
            return pp
        return None

    S = min(seed0, tree_seed)
    prop = label_box(S)
    if prop is None:
        # The tighter tree-seed box is not itself a feasible instance; use
        # the always-feasible all-k=0 seed cost for the label box.
        S = seed0
        prop = label_box(S)
    assert prop is not None, "internal: seed0 relaxation must be feasible"
    seed_cost = S
    pidx = prop.idx
    lab_lo = {e: prop.low[pidx[e]] for e in event_ids}
    lab_hi = {e: prop.high[pidx[e]] for e in event_ids}
    width = max(lab_hi[e] - lab_lo[e] for e in event_ids)
    max_k = max(lab_hi.values(), default=0)
    B = max(width, max_k, seed_cost) + 1  # mixed radix: components below B

    # Lexicographic encoding of the cut value:
    #   total_cost * B**(n+m)
    #     + sum_i x_ci * B**(n + m-1-i)   (relax vector, constraint order)
    #     + sum_j k_ej * B**(n-1-j)       (wrap vector, event order)
    # Every component is below B and each place strictly dominates all
    # lower places, so minimizing the weighted sum applies exactly:
    # minimum total first, then relax vector, then wrap vector.  Ramp i is
    # therefore weighted by W0 + its relax place; wrap unary edges carry the
    # wrap places.
    W0 = B ** (n + m)
    w_constraint = [B ** (n + m - 1 - i) for i in range(m)]
    w_event = [B ** (n - 1 - j) for j in range(n)]
    ramp_weight = [W0 + w_constraint[i] for i in range(m)]

    # -- graph ---------------------------------------------------------------
    # Threshold p of event e on the SOURCE side means k_e >= lab_lo[e] + p.
    # Ordering edges make the source-side thresholds a prefix 1..level.
    vmap = {}
    graph_nodes = 0
    for eid in event_ids:
        W = lab_hi[eid] - lab_lo[eid]
        for l in range(1, W + 1):
            vmap[(eid, l)] = graph_nodes
            graph_nodes += 1
    source, sink = graph_nodes, graph_nodes + 1

    def V(eid, l):
        return vmap[(eid, l)]

    finite_sum = 0
    dinic = _Dinic(sink + 1)

    def fin_edge(u, v, cap):
        nonlocal finite_sum
        if cap > 0:
            dinic.add_edge(u, v, cap)
            finite_sum += cap

    def lo_of(nid):
        return 0 if nid == "anchor" else lab_lo[nid]

    def hi_of(nid):
        return 0 if nid == "anchor" else lab_hi[nid]

    # Wrap-vector tie-break unary weights.
    for j, eid in enumerate(event_ids):
        for l in range(1, lab_hi[eid] - lab_lo[eid] + 1):
            fin_edge(V(eid, l), sink, w_event[j])

    # Convex ramp expansion per constraint.  Levels are absolute wrap
    # counts; X_p / Y_q select src/dst thresholds.  Every coefficient below
    # is non-negative (a submodular mixed difference or a telescoped
    # boundary increment), with the ramp's box constant on source->sink.
    for ci, c in enumerate(constraints):
        w = ramp_weight[ci]
        r = _residue(c, residues, anchor_tick)
        A = c.lo - r   # lower ramp L = max(0, A + M k_s - M k_d)
        Bc = c.hi - r  # upper ramp R = max(0, M k_d - M k_s - Bc)
        s_id, d_id = c.src, c.dst
        ls, hs = lo_of(s_id), hi_of(s_id)
        ld, hd = lo_of(d_id), hi_of(d_id)
        Ws, Wd = hs - ls, hd - ld

        def L(ks, kd):
            return w * _ramp_left(A, M, ks, kd)

        def Rr(ks, kd):
            return w * _ramp_right(Bc, M, ks, kd)

        # -- lower ramp: increasing in k_s, decreasing in k_d ---------------
        fin_edge(source, sink, L(ls, hd))             # box constant
        for p in range(1, Ws + 1):                    # unary on X_p
            ks = ls + p
            fin_edge(V(s_id, p), sink, L(ks, hd) - L(ks - 1, hd))
        for q in range(1, Wd + 1):                    # unary on (1 - Y_q)
            kd = ld + q
            fin_edge(source, V(d_id, q), L(ls, kd - 1) - L(ls, kd))
        for p in range(1, Ws + 1):
            for q in range(1, Wd + 1):
                ks, kd = ls + p, ld + q
                delta = (L(ks - 1, kd) + L(ks, kd - 1)
                         - L(ks - 1, kd - 1) - L(ks, kd))
                fin_edge(V(s_id, p), V(d_id, q), delta)

        # -- upper ramp: decreasing in k_s, increasing in k_d ---------------
        fin_edge(source, sink, Rr(hs, ld))            # box constant
        for q in range(1, Wd + 1):                    # unary on Y_q
            kd = ld + q
            fin_edge(V(d_id, q), sink, Rr(hs, kd) - Rr(hs, kd - 1))
        for p in range(1, Ws + 1):                    # unary on (1 - X_p)
            ks = ls + p
            fin_edge(source, V(s_id, p), Rr(ks - 1, ld) - Rr(ks, ld))
        for p in range(1, Ws + 1):
            for q in range(1, Wd + 1):
                ks, kd = ls + p, ld + q
                delta = (Rr(ks - 1, kd) + Rr(ks, kd - 1)
                         - Rr(ks - 1, kd - 1) - Rr(ks, kd))
                fin_edge(V(d_id, q), V(s_id, p), delta)

    # Infinite ordering edges, strictly above every finite cut.
    INF = finite_sum + 1
    for eid in event_ids:
        W = lab_hi[eid] - lab_lo[eid]
        for l in range(1, W):
            dinic.add_edge(V(eid, l + 1), V(eid, l), INF)

    dinic.max_flow(source, sink)

    # Source-reachable thresholds give the chosen wrap counts.
    reachable = dinic.level
    kvec = {}
    for eid in event_ids:
        level = 0
        W = lab_hi[eid] - lab_lo[eid]
        for l in range(1, W + 1):
            if reachable[V(eid, l)] >= 0:
                level = l
        kvec[eid] = lab_lo[eid] + level

    # Derive the canonical relaxation vector from the winning timeline.
    def tick_of(endpoint):
        return anchor_tick if endpoint == "anchor" \
            else residues[endpoint] + M * kvec[endpoint]

    amounts = {}
    total = 0
    for c in constraints:
        delta = tick_of(c.dst) - tick_of(c.src)
        x = max(0, c.lo - delta, delta - c.hi)
        amounts[c.id] = x
        total += x
    return amounts, total, kvec, event_ids, residues


# ---------------------------------------------------------------------------
# Evidence rendering
# ---------------------------------------------------------------------------

def _build_timeline(anchor_tick, residues, event_ids, kvec, modulus):
    entries = [{"event": "anchor", "tick": anchor_tick,
                "wrap_count": None, "counter": None}]
    for eid in event_ids:
        entries.append({
            "event": eid,
            "tick": residues[eid] + modulus * kvec[eid],
            "wrap_count": kvec[eid],
            "counter": residues[eid],
        })
    return {"entries": entries}


def _per_constraint_report(constraints, amounts, anchor_tick, residues,
                           kvec, modulus):
    """Per-constraint original window, relaxation, direction and difference.

    ``direction`` records which outer end of the original closed window the
    canonical timeline falls beyond (``lower``: below lo, ``upper``: above
    hi, ``none``: inside the original window); ``outward_gap`` is the
    positive integer distance beyond that end (0 for ``none``).
    """
    ticks = {eid: residues[eid] + modulus * kvec[eid] for eid in residues}

    def tick_of(endpoint):
        return anchor_tick if endpoint == "anchor" else ticks[endpoint]

    report = []
    for c in constraints:  # already sorted by id
        x = amounts.get(c.id, 0)
        delta = tick_of(c.dst) - tick_of(c.src)
        gap_lower = c.lo - delta   # > 0 iff the timeline sits below lo
        gap_upper = delta - c.hi   # > 0 iff the timeline sits above hi
        if gap_lower > 0:
            direction, gap = "lower", gap_lower
        elif gap_upper > 0:
            direction, gap = "upper", gap_upper
        else:
            direction, gap = "none", 0
        report.append({
            "id": c.id,
            "src": c.src,
            "dst": c.dst,
            "original_window": [c.lo, c.hi],
            "relaxed_window": [c.lo - x, c.hi + x],
            "extension": x,
            "direction": direction,
            "recomputed_difference": delta,
            "outward_gap": gap,
        })
    return report


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def _validate_repair_payload(payload):
    """Repair body carries no alterable parameters besides the repair id.

    Unknown fields are rejected so a reused repair_id cannot smuggle a
    changed payload past idempotency.
    """
    if not isinstance(payload, dict):
        raise ValidationError("repair payload must be a JSON object")
    extra = set(payload)
    if extra:
        raise ValidationError(
            f"unknown repair payload fields: {sorted(extra)}")


def repair(source_record, payload):
    """Build the canonical minimum repair of a frozen unsat audit."""
    _validate_repair_payload(payload)

    result = source_record.get("result") or {}
    if result.get("status") != "unsat":
        raise SourceNotUnsat(source_record.get("audit_no"),
                             result.get("status"))

    frozen_input = source_record["input"]
    modulus, anchor_tick, events, raw_constraints = _validate(frozen_input)
    constraints = sorted(raw_constraints, key=lambda c: c.id)
    positions = [c.id for c in constraints]

    amounts, total, kvec, event_ids, residues = optimize(
        modulus, anchor_tick, events, constraints)

    timeline = _build_timeline(anchor_tick, residues, event_ids, kvec,
                               modulus)
    report = _per_constraint_report(constraints, amounts, anchor_tick,
                                    residues, kvec, modulus)

    return {
        "status": "repaired",
        "total_extension": total,
        "extension_unit": "tick",
        "modulus": modulus,
        "timeline": timeline,
        "constraints": report,
        "canonical_decision": {
            "rule": "min total extension, then relax vector by constraint "
                    "id order, then wrap vector by event id order",
            "constraint_order": positions,
            "relax_vector": [amounts[cid] for cid in positions],
            "event_order": event_ids,
            "wrap_vector": [kvec[eid] for eid in event_ids],
        },
        "source": {
            "audit_no": source_record["audit_no"],
            "request_id": source_record.get("request_id"),
            "payload_hash": source_record.get("payload_hash"),
            "original_status": "unsat",
            "conflict": result.get("conflict"),
        },
    }
