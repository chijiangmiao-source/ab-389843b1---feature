"""Wraparound timestamp timeline solver.

Model
-----
Every event ``e`` reports a counter value ``c_e`` taken modulo ``M``.  The real
(absolute) tick of the event is

    t_e = c_e + M * k_e ,   k_e integer, k_e >= 0

where ``k_e`` is the integer wrap count (device counters start at epoch 0).
Identical counter values therefore do **not** imply identical times.

The anchor has a known absolute tick ``A`` (no wrap variable).

A precedence constraint ``src -> dst = [lo, hi]`` means the closed interval

    lo <= t_dst - t_src <= hi .

Substituting the tick model turns every constraint into *integer difference
constraints* on the wrap counts ``k_e`` (bounds divided by ``M`` with exact
integer ceil/floor rounding):

    event -> event :  ceil((lo + c_s - c_d)/M) <= k_d - k_s
                                                 <= floor((hi + c_s - c_d)/M)
    anchor-> event :  ceil((lo + A - c_d)/M) <= k_d <= floor((hi + A - c_d)/M)
    event -> anchor:  ceil((lo + c_s - A)/M) <= -k_s <= floor((hi + c_s - A)/M)

Additionally every event is connected to a synthetic epoch node ``Z`` (pinned
at 0) through ``k_e >= 0`` (edge Z -> e with bounds [0, +inf)).

Feasibility and tightest per-variable bounds are computed by simultaneous
lower/upper bound propagation (longest/shortest path relaxation).  Because the
epoch node is pinned and every event is connected to it, every variable ends
with finite bounds, so the solution set is finite and:

  * the instance is **unique** iff low_e == high_e for every event;
  * the lexicographically smallest / second-smallest feasible wrap-count
    vectors (events ordered by id) are obtained by greedy prefix extension
    with re-propagation — feasible values of a variable under a fixed prefix
    form a contiguous integer interval, so no scanning is needed;
  * exact min/max of any pairwise difference ``t_a - t_b`` over all solutions
    is computed by binary search with feasibility-oracle calls, which
    identifies the first unstable precedence relation.

All arithmetic is exact integer arithmetic; no floats anywhere.
"""

from __future__ import annotations

from dataclasses import dataclass
from itertools import combinations

MAX_EVENTS = 12
EPOCH = "__epoch__"  # synthetic node pinned at 0, encodes k_e >= 0


class ValidationError(Exception):
    """Raised for malformed or semantically invalid audit payloads."""


@dataclass(frozen=True)
class Constraint:
    id: str
    src: str
    dst: str
    lo: int
    hi: int


def _ceil_div(a: int, b: int) -> int:
    """ceil(a/b) for b > 0, exact integer arithmetic."""
    return -((-a) // b)


# ---------------------------------------------------------------------------
# Bound propagation
# ---------------------------------------------------------------------------

class _Propagator:
    """Simultaneous lower/upper bound propagation over difference constraints.

    Nodes: EPOCH plus one per event.  ``low[i]``/``high[i]`` are the tightest
    bounds known for node ``i`` (EPOCH pinned to 0).  ``None`` means unbounded.

    Edge (u, v, lo, hi, tag) means  lo <= x_v - x_u <= hi  with lo/hi possibly
    None for -inf/+inf.

    With tracking enabled, ``low_src[i]``/``high_src[i]`` record the
    (predecessor, edge-index, direction) that last improved each bound; these
    chains are the recomputable evidence derivations.
    """

    def __init__(self, nodes, edges, track=False):
        self.nodes = list(nodes)
        self.idx = {n: i for i, n in enumerate(self.nodes)}
        self.edges = edges
        self.track = track
        n = len(self.nodes)
        self.low = [None] * n
        self.high = [None] * n
        z = self.idx[EPOCH]
        self.low[z] = 0
        self.high[z] = 0
        self.low_src = [None] * n
        self.high_src = [None] * n
        self.conflict = None

    def _path_to(self, node, which):
        """Derivation chain of low/high[node] back towards EPOCH.

        Returns (steps, cycle_steps): ``steps`` is a list of (edge_index,
        direction) ordered from the chain start to ``node``; ``cycle_steps``
        is non-empty when the predecessor walk loops and contains only the
        steps of that loop (a positive-weight cycle drove the bound).
        """
        src = self.low_src if which == "low" else self.high_src
        steps = []
        cur = node
        seen = {}
        repeat_at = None
        while True:
            if cur in seen:
                repeat_at = seen[cur]
                break
            seen[cur] = len(steps)
            rec = src[self.idx[cur]]
            if rec is None:
                break
            prev, edge_idx, direction = rec
            steps.append((edge_idx, direction))
            cur = prev
        steps.reverse()
        cycle_steps = []
        if repeat_at is not None:
            # steps was built node..towards EPOCH; the loop occupies the
            # indices [repeat_at, end) in that pre-reverse ordering, which
            # after reversing become the first entries back to EPOCH...
            # compute from the reversed list directly:
            loop_len = len(steps) - repeat_at
            cycle_steps = steps[:loop_len]
        return steps, cycle_steps

    def _check_node(self, node):
        i = self.idx[node]
        lo, hi = self.low[i], self.high[i]
        if lo is not None and hi is not None and lo > hi:
            if self.track:
                low_path, low_cycle = self._path_to(node, "low")
                high_path, high_cycle = self._path_to(node, "high")
                self.conflict = {
                    "kind": "bound_contradiction",
                    "node": node,
                    "low_path": low_path,
                    "high_path": high_path,
                    "low_cycle": low_cycle,
                    "high_cycle": high_cycle,
                    "cyclic": bool(low_cycle or high_cycle),
                }
            else:
                self.conflict = {"kind": "bound_contradiction", "node": node}
            return True
        return False

    def run(self):
        """Propagate until fixpoint.  Returns True if feasible."""
        n = len(self.nodes)
        # Longest simple path has <= n-1 edges; still-productive relaxation
        # after n+1 full rounds implies a positive-weight cycle => infeasible.
        for _rnd in range(n + 2):
            changed = False
            for ei, (u, v, lo, hi, _tag) in enumerate(self.edges):
                iu, iv = self.idx[u], self.idx[v]
                # x_v >= x_u + lo
                if lo is not None and self.low[iu] is not None:
                    cand = self.low[iu] + lo
                    if self.low[iv] is None or cand > self.low[iv]:
                        self.low[iv] = cand
                        if self.track:
                            self.low_src[iv] = (u, ei, "forward")
                        changed = True
                        if self._check_node(v):
                            return False
                # x_u <= x_v - lo
                if lo is not None and self.high[iv] is not None:
                    cand = self.high[iv] - lo
                    if self.high[iu] is None or cand < self.high[iu]:
                        self.high[iu] = cand
                        if self.track:
                            self.high_src[iu] = (v, ei, "backward")
                        changed = True
                        if self._check_node(u):
                            return False
                # x_v <= x_u + hi
                if hi is not None and self.high[iu] is not None:
                    cand = self.high[iu] + hi
                    if self.high[iv] is None or cand < self.high[iv]:
                        self.high[iv] = cand
                        if self.track:
                            self.high_src[iv] = (u, ei, "forward")
                        changed = True
                        if self._check_node(v):
                            return False
                # x_u >= x_v - hi
                if hi is not None and self.low[iv] is not None:
                    cand = self.low[iv] - hi
                    if self.low[iu] is None or cand > self.low[iu]:
                        self.low[iu] = cand
                        if self.track:
                            self.low_src[iu] = (v, ei, "backward")
                        changed = True
                        if self._check_node(u):
                            return False
            if not changed:
                return True
        self.conflict = self._cycle_conflict()
        return False

    def _cycle_conflict(self):
        """Extract a positive cycle from predecessor chains (low or high)."""
        for which in ("low", "high"):
            src = self.low_src if which == "low" else self.high_src
            for start in self.nodes:
                seq = []
                pos = {}
                cur = start
                while cur is not None and cur not in pos:
                    pos[cur] = len(seq)
                    seq.append(cur)
                    rec = src[self.idx[cur]]
                    if rec is None:
                        break
                    cur = rec[0]
                if cur is not None and cur in pos and cur != EPOCH:
                    cycle_nodes = seq[pos[cur]:]
                    edge_idx = []
                    for nd in cycle_nodes:
                        rec = src[self.idx[nd]]
                        if rec is not None:
                            edge_idx.append(rec[1])
                    return {
                        "kind": "positive_cycle",
                        "node": cur,
                        "cycle_nodes": cycle_nodes,
                        "cycle_edge_indices": edge_idx,
                    }
        return {"kind": "positive_cycle", "node": None,
                "cycle_nodes": [], "cycle_edge_indices": []}


# ---------------------------------------------------------------------------
# Edge construction
# ---------------------------------------------------------------------------

def _build_edges(modulus, anchor_tick, events, constraints):
    """Translate constraints into integer difference constraints on k_e.

    Returns (nodes, edges, edge_records); edge_records[i] describes edge i
    for evidence rendering.
    """
    residues = {e["id"]: e["counter"] for e in events}
    nodes = [EPOCH] + sorted(residues)
    edges = []
    records = []

    # Synthetic epoch edges: k_e >= 0 (device counters start at epoch 0).
    for eid in sorted(residues):
        edges.append((EPOCH, eid, 0, None, ("epoch", eid)))
        records.append({
            "kind": "epoch",
            "event": eid,
            "text": f"k({eid}) >= 0 (wrap count is non-negative)",
        })

    A = anchor_tick
    M = modulus
    for c in constraints:
        s, d = c.src, c.dst
        if s == "anchor":
            cd = residues[d]
            lo_k = _ceil_div(c.lo + A - cd, M)
            hi_k = (c.hi + A - cd) // M
            edges.append((EPOCH, d, lo_k, hi_k, ("constraint", c.id)))
            records.append({
                "kind": "anchor_unary",
                "constraint_id": c.id,
                "event": d,
                "k_lo": lo_k,
                "k_hi": hi_k,
                "text": (f"{c.id}: {c.lo} <= t({d}) - {A} <= {c.hi}  =>  "
                         f"{lo_k} <= k({d}) <= {hi_k}"),
            })
        elif d == "anchor":
            cs = residues[s]
            lo_k = _ceil_div(c.lo + cs - A, M)
            hi_k = (c.hi + cs - A) // M
            # lo_k <= -k_s <= hi_k  ==  -hi_k <= k_s <= -lo_k
            edges.append((EPOCH, s, -hi_k, -lo_k, ("constraint", c.id)))
            records.append({
                "kind": "anchor_unary",
                "constraint_id": c.id,
                "event": s,
                "k_lo": -hi_k,
                "k_hi": -lo_k,
                "text": (f"{c.id}: {c.lo} <= {A} - t({s}) <= {c.hi}  =>  "
                         f"{-hi_k} <= k({s}) <= {-lo_k}"),
            })
        else:
            cs, cd = residues[s], residues[d]
            lo_d = c.lo + cs - cd   # <= M*(k_d - k_s)
            hi_d = c.hi + cs - cd   # >= M*(k_d - k_s)
            k_lo = _ceil_div(lo_d, M)
            k_hi = hi_d // M
            edges.append((s, d, k_lo, k_hi, ("constraint", c.id)))
            records.append({
                "kind": "difference",
                "constraint_id": c.id,
                "src": s,
                "dst": d,
                "delta_lo": k_lo,
                "delta_hi": k_hi,
                "text": (f"{c.id}: {c.lo} <= t({d}) - t({s}) <= {c.hi}  =>  "
                         f"{k_lo} <= k({d}) - k({s}) <= {k_hi}"),
            })
    return nodes, edges, records


# ---------------------------------------------------------------------------
# Evidence rendering
# ---------------------------------------------------------------------------

def _render_steps(steps, edge_records):
    out = []
    for edge_idx, direction in steps:
        rec = edge_records[edge_idx]
        step = {"via": rec["text"], "direction": direction}
        if rec["kind"] == "epoch":
            step["constraint_id"] = None
        elif rec["kind"] == "anchor_unary":
            step["constraint_id"] = rec["constraint_id"]
            step["k_bound"] = [rec["k_lo"], rec["k_hi"]]
        else:
            step["constraint_id"] = rec["constraint_id"]
            step["k_delta_range"] = [rec["delta_lo"], rec["delta_hi"]]
        out.append(step)
    return out


def _path_k_range(steps, edge_records):
    """[k_lo, k_hi] summed along a chain from EPOCH, plus the end event.

    Backward steps negate (and swap) the edge's k-delta range.
    """
    k_lo = 0
    k_hi = 0
    end_event = None
    for edge_idx, direction in steps:
        rec = edge_records[edge_idx]
        if rec["kind"] == "epoch":
            d_lo, d_hi = 0, None
            end_event = rec["event"]
        elif rec["kind"] == "anchor_unary":
            d_lo, d_hi = rec["k_lo"], rec["k_hi"]
            end_event = rec["event"]
        else:
            d_lo, d_hi = rec["delta_lo"], rec["delta_hi"]
            end_event = rec["dst"] if direction == "forward" else rec["src"]
        if direction == "backward":
            d_lo, d_hi = (-d_hi if d_hi is not None else None,
                          -d_lo if d_lo is not None else None)
        k_lo = None if (k_lo is None or d_lo is None) else k_lo + d_lo
        k_hi = None if (k_hi is None or d_hi is None) else k_hi + d_hi
    return k_lo, k_hi, end_event


def _build_conflict(conf, edge_records, residues, modulus):
    """Turn a propagator conflict into a recomputable conflict chain."""
    if conf["kind"] == "positive_cycle":
        ids = []
        for ei in conf.get("cycle_edge_indices", []):
            rec = edge_records[ei]
            cid = rec.get("constraint_id")
            ids.append(cid if cid is not None else f"epoch:{rec.get('event')}")
        return {
            "type": "positive_cycle",
            "summary": "constraints around a cycle demand a strictly positive "
                       "total wrap count, which is impossible",
            "cycle_nodes": conf.get("cycle_nodes", []),
            "constraint_chain": ids,
        }

    node = conf["node"]
    low_steps = _render_steps(conf["low_path"], edge_records)
    high_steps = _render_steps(conf["high_path"], edge_records)

    chain_ids = []
    for st in low_steps + high_steps:
        cid = st.get("constraint_id")
        if cid is not None and cid not in chain_ids:
            chain_ids.append(cid)

    # Cyclic derivation: constraints around a loop demand a strictly positive
    # total wrap count, which no assignment can satisfy.
    if conf.get("cyclic"):
        cycle_steps = conf["low_cycle"] or conf["high_cycle"]
        cycle_ids = []
        for st in _render_steps(cycle_steps, edge_records):
            cid = st.get("constraint_id")
            if cid is not None and cid not in cycle_ids:
                cycle_ids.append(cid)
        return {
            "type": "positive_cycle",
            "event": node,
            "summary": "constraints around a cycle demand a strictly positive "
                       "total wrap count, which is impossible",
            "constraint_chain": cycle_ids,
            "lower_bound_derivation": {"steps": low_steps},
            "upper_bound_derivation": {"steps": high_steps},
        }

    lo_k, _lo_hi, lo_ev = _path_k_range(conf["low_path"], edge_records)
    _hi_lo, hi_k, hi_ev = _path_k_range(conf["high_path"], edge_records)

    # Modular residue conflict: a single anchor<->event constraint whose
    # integer k-window is empty (no wrap count can satisfy it).
    if len(chain_ids) <= 1 and node != EPOCH:
        for rec in edge_records:
            if rec["kind"] == "anchor_unary" and rec["event"] == node \
                    and rec["k_lo"] > rec["k_hi"]:
                return {
                    "type": "modular_residue_conflict",
                    "event": node,
                    "constraint_id": rec["constraint_id"],
                    "constraint_chain": [rec["constraint_id"]],
                    "detail": rec["text"],
                    "empty_k_window": [rec["k_lo"], rec["k_hi"]],
                    "summary": (
                        f"constraint {rec['constraint_id']} requires "
                        f"{rec['k_lo']} <= k({node}) <= {rec['k_hi']}: no "
                        f"integer wrap count exists"),
                }

    def tick(event, k):
        return None if (event is None or k is None) \
            else residues[event] + modulus * k

    return {
        "type": "bound_contradiction",
        "event": node,
        "summary": (f"constraint chain forces k({node}) >= {lo_k} and "
                    f"k({node}) <= {hi_k} simultaneously"),
        "constraint_chain": chain_ids,
        "lower_bound_derivation": {
            "steps": low_steps,
            "derived_event": lo_ev,
            "derived_k_min": lo_k,
            "derived_tick_min": tick(lo_ev, lo_k),
        },
        "upper_bound_derivation": {
            "steps": high_steps,
            "derived_event": hi_ev,
            "derived_k_max": hi_k,
            "derived_tick_max": tick(hi_ev, hi_k),
        },
    }


# ---------------------------------------------------------------------------
# Solution enumeration helpers
# ---------------------------------------------------------------------------

def _propagate_with_prefix(nodes, edges, prefix):
    p = _Propagator(nodes, edges, track=False)
    for eid, val in prefix.items():
        i = p.idx[eid]
        p.low[i] = val
        p.high[i] = val
    ok = p.run()
    return ok, p


def _lex_solution(nodes, edges, event_ids, prefix):
    """Lexicographically smallest feasible wrap-count vector extending prefix.

    Feasible values of a variable under a fixed prefix form the contiguous
    integer interval [low, high] given by propagation, so the greedy choice is
    simply the current lower bound.
    """
    assignment = dict(prefix)
    for eid in event_ids:
        if eid in assignment:
            continue
        ok, p = _propagate_with_prefix(nodes, edges, assignment)
        assert ok, "feasible instance became infeasible during enumeration"
        assignment[eid] = p.low[p.idx[eid]]
    return assignment


def _lex_second(nodes, edges, event_ids, k1):
    """Lexicographically second feasible vector (smallest one above k1)."""
    for j in range(len(event_ids) - 1, -1, -1):
        prefix = {eid: k1[eid] for eid in event_ids[:j]}
        eid = event_ids[j]
        ok, p = _propagate_with_prefix(nodes, edges, prefix)
        if not ok:
            continue
        hi_j = p.high[p.idx[eid]]
        if hi_j is not None and hi_j > k1[eid]:
            cand = dict(prefix)
            cand[eid] = k1[eid] + 1
            return _lex_solution(nodes, edges, event_ids, cand)
    raise AssertionError("status 'multiple' but no second solution exists")


def _difference_range(nodes, edges, a, b):
    """Exact [min, max] of k_a - k_b over all solutions (finite bounds)."""
    p = _Propagator(nodes, edges, track=False)
    p.run()
    idx = p.idx
    lo_est = p.low[idx[a]] - p.high[idx[b]]
    hi_est = p.high[idx[a]] - p.low[idx[b]]

    def feasible_with(extra_lo, extra_hi):
        e2 = list(edges)
        e2.append((b, a, extra_lo, extra_hi, ("query", None)))
        return _Propagator(nodes, e2, track=False).run()

    # min: smallest d with (k_a - k_b <= d) feasible; monotone in d.
    lo, hi = lo_est, hi_est
    while lo < hi:
        mid = (lo + hi) // 2
        if feasible_with(None, mid):
            hi = mid
        else:
            lo = mid + 1
    d_min = lo
    # max: largest d with (k_a - k_b >= d) feasible; monotone in d.
    lo, hi = lo_est, hi_est
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if feasible_with(mid, None):
            lo = mid
        else:
            hi = mid - 1
    d_max = lo
    return d_min, d_max


def _first_unstable_pair(nodes, edges, prop, event_ids, residues, modulus,
                         anchor_tick, k1, k2):
    """First pair (id order, anchor first) whose precedence is unstable.

    A pair (a, b) is stable iff every solution has t_a <= t_b (max diff <= 0)
    or every solution has t_a >= t_b (min diff >= 0); otherwise the precedence
    between them flips across solutions and the pair is unstable.
    """
    idx = prop.idx
    ordered = ["anchor"] + event_ids

    def tick_of(eid, kvec):
        if eid == "anchor":
            return anchor_tick
        return residues[eid] + modulus * kvec[eid]

    def tick_diff_bounds(a, b):
        """[min, max] of t_a - t_b over all solutions."""
        if a == "anchor":
            # A - (c_b + M k_b)
            lo = anchor_tick - residues[b] - modulus * prop.high[idx[b]]
            hi = anchor_tick - residues[b] - modulus * prop.low[idx[b]]
            return lo, hi
        if b == "anchor":
            lo = residues[a] + modulus * prop.low[idx[a]] - anchor_tick
            hi = residues[a] + modulus * prop.high[idx[a]] - anchor_tick
            return lo, hi
        d_lo, d_hi = _difference_range(nodes, edges, a, b)
        base = residues[a] - residues[b]
        return base + modulus * d_lo, base + modulus * d_hi

    for a, b in combinations(ordered, 2):
        lo_t, hi_t = tick_diff_bounds(a, b)
        if hi_t <= 0 or lo_t >= 0:
            continue  # stable precedence
        return {
            "pair": [a, b],
            "relation": "unstable",
            "tick_difference_range": [lo_t, hi_t],
            "in_timeline_1": {
                "ticks": [tick_of(a, k1), tick_of(b, k1)],
                "difference": tick_of(a, k1) - tick_of(b, k1),
            },
            "in_timeline_2": {
                "ticks": [tick_of(a, k2), tick_of(b, k2)],
                "difference": tick_of(a, k2) - tick_of(b, k2),
            },
        }
    return None


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def solve(payload):
    """Solve one audit request payload.

    Returns a dict with key ``status`` in {'unique', 'multiple', 'unsat'}
    plus status-specific evidence.  Raises ValidationError for bad input.
    """
    modulus, anchor_tick, events, constraints = _validate(payload)
    residues = {e["id"]: e["counter"] for e in events}
    event_ids = sorted(residues)

    nodes, edges, edge_records = _build_edges(
        modulus, anchor_tick, events, constraints)

    prop = _Propagator(nodes, edges, track=True)
    if not prop.run():
        return {
            "status": "unsat",
            "conflict": _build_conflict(prop.conflict, edge_records,
                                        residues, modulus),
        }

    idx = prop.idx
    lows = {e: prop.low[idx[e]] for e in event_ids}
    highs = {e: prop.high[idx[e]] for e in event_ids}

    def timeline(kvec):
        entries = [{"event": "anchor", "tick": anchor_tick,
                    "wrap_count": None, "counter": None}]
        for eid in event_ids:  # already sorted by id
            entries.append({
                "event": eid,
                "tick": residues[eid] + modulus * kvec[eid],
                "wrap_count": kvec[eid],
                "counter": residues[eid],
            })
        return {"entries": entries}

    if all(lows[e] == highs[e] for e in event_ids):
        kvec = {e: lows[e] for e in event_ids}
        return {
            "status": "unique",
            "timeline": timeline(kvec),
            "evidence": {
                "wrap_counts": dict(kvec),
                "derivations": {
                    e: _render_steps(prop._path_to(e, "low")[0], edge_records)
                    for e in event_ids
                },
            },
        }

    k1 = _lex_solution(nodes, edges, event_ids, prefix={})
    k2 = _lex_second(nodes, edges, event_ids, k1)
    return {
        "status": "multiple",
        "timelines": [timeline(k1), timeline(k2)],
        "first_unstable_precedence": _first_unstable_pair(
            nodes, edges, prop, event_ids, residues, modulus, anchor_tick,
            k1, k2),
        "evidence": {
            "wrap_count_bounds": {e: [lows[e], highs[e]] for e in event_ids},
        },
    }


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def _validate(payload):
    if not isinstance(payload, dict):
        raise ValidationError("payload must be a JSON object")

    modulus = payload.get("modulus")
    if not isinstance(modulus, int) or isinstance(modulus, bool) or modulus < 2:
        raise ValidationError("modulus must be an integer >= 2")

    anchor = payload.get("anchor")
    if not isinstance(anchor, dict):
        raise ValidationError("anchor must be an object {id, tick}")
    anchor_tick = anchor.get("tick")
    if not isinstance(anchor_tick, int) or isinstance(anchor_tick, bool) \
            or anchor_tick < 0:
        raise ValidationError("anchor.tick must be a non-negative integer")

    events = payload.get("events")
    if not isinstance(events, list) or not (1 <= len(events) <= MAX_EVENTS):
        raise ValidationError(
            f"events must be a list of 1..{MAX_EVENTS} items")
    event_ids = set()
    for e in events:
        if not isinstance(e, dict):
            raise ValidationError("each event must be an object {id, counter}")
        eid = e.get("id")
        c = e.get("counter")
        if not isinstance(eid, str) or not eid:
            raise ValidationError("event.id must be a non-empty string")
        if eid == "anchor":
            raise ValidationError("event id 'anchor' is reserved")
        if eid in event_ids:
            raise ValidationError(f"duplicate event id {eid!r}")
        event_ids.add(eid)
        if not isinstance(c, int) or isinstance(c, bool) \
                or not (0 <= c < modulus):
            raise ValidationError(
                f"event {eid!r}: counter must be an integer in [0, modulus)")

    constraints = payload.get("constraints")
    if not isinstance(constraints, list):
        raise ValidationError("constraints must be a list")
    cids = set()
    out = []
    for c in constraints:
        if not isinstance(c, dict):
            raise ValidationError(
                "each constraint must be an object {id, src, dst, window}")
        cid = c.get("id")
        if not isinstance(cid, str) or not cid:
            raise ValidationError("constraint.id must be a non-empty string")
        if cid in cids:
            raise ValidationError(f"duplicate constraint id {cid!r}")
        cids.add(cid)
        src, dst = c.get("src"), c.get("dst")
        for ep in (src, dst):
            if ep != "anchor" and ep not in event_ids:
                raise ValidationError(
                    f"constraint {cid!r}: unknown endpoint {ep!r}")
        if src == dst:
            raise ValidationError(
                f"constraint {cid!r}: self-loops are not allowed")
        if src == "anchor" and dst == "anchor":
            raise ValidationError(
                f"constraint {cid!r}: anchor-to-anchor is not allowed")
        w = c.get("window")
        if not (isinstance(w, list) and len(w) == 2
                and all(isinstance(x, int) and not isinstance(x, bool)
                        for x in w)):
            raise ValidationError(
                f"constraint {cid!r}: window must be [lo, hi] of integers")
        lo, hi = w
        if lo > hi:
            raise ValidationError(
                f"constraint {cid!r}: window lo must be <= hi")
        out.append(Constraint(cid, src, dst, lo, hi))

    # Every event must be connected to the anchor through constraints
    # (undirected reachability over the constraint graph).
    adj = {eid: set() for eid in event_ids | {"anchor"}}
    for c in out:
        adj[c.src].add(c.dst)
        adj[c.dst].add(c.src)
    visited = set()
    stack = ["anchor"]
    while stack:
        cur = stack.pop()
        if cur in visited:
            continue
        visited.add(cur)
        stack.extend(adj[cur] - visited)
    disconnected = sorted(event_ids - visited)
    if disconnected:
        raise ValidationError(
            f"events not connected to anchor via constraints: {disconnected}")

    return modulus, anchor_tick, events, out
