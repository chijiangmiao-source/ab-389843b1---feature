"""Minimum outward window-relaxation repair for unsat audits.

An auditor that has read an audit whose conclusion is ``unsat`` may open a
repair against it.  A repair keeps every original record (modulus, counters,
anchor, wrap counts ``k_e >= 0``) and only widens constraint windows outward:

    constraint i  src -> dst = [lo_i, hi_i]
        lo_i' = lo_i - x_i ,   hi_i' = hi_i + y_i ,   x_i, y_i >= 0 integers

so that the original records can lie on one true timeline.  The objective is

    minimize   sum_i (x_i + y_i)          (total expansion, integer ticks)

subject to the count modulus and ``k_e >= 0``.  Ties are adjudicated, in
order, by:

  1. the lexicographically smallest relaxation vector ``(x_i, y_i)`` taken in
     constraint-id order; then
  2. the lexicographically smallest wrap-count vector (canonical timeline)
     taken in event-id order.

Reduction (exact integer arithmetic, no third-party dependencies)
-----------------------------------------------------------------
For any fixed wrap-count vector ``k`` the cheapest repair is forced:

    x_i(k) = max(0, lo_i - (t_dst - t_src))
    y_i(k) = max(0, (t_dst - t_src) - hi_i)

so the optimum is ``min_{k>=0}`` of a sum of discrete convex (hinge)
functions of node potentials.  Each hinge's discrete convex conjugate is a
pair of directed arcs whose two constant marginal-cost tiers correspond to
the two integer residues at the window endpoints; minimizing the weighted
sum of conjugates is an integer **min-cost circulation** on the event graph
with a ground node ``G`` (anchor pinned at 0), where an infinite-capacity
zero-cost arc ``e -> G`` encodes ``k_e >= 0``.

The lexicographic tie-break is encoded as one weighted primal
``B*S + sum_i (wx_i*x_i + wy_i*y_i)`` with super-increasing base-``R``
weights, so every relaxed hinge is replicated that many times (arc
capacities scale by the same weight).  The optimum circulation cost is one
mixed-radix number whose digits are, from least significant,
``y_last, x_last, ..., y_first, x_first, S``; the digits are decoded by
successive division.  The canonical timeline is then obtained by re-solving
the relaxed windows with the frozen audit solver, whose enumeration returns
the lexicographically smallest feasible wrap-count vector.

Endpoint residues for a constraint u -> v (the anchor's residue is its known
absolute tick):  ``a = lo + r_u - r_v``, ``b = hi + r_u - r_v``,

    L = ceil(a / M) ,   d = a - M*(L-1) in [1, M]
    U = floor(b / M),   e = b - M*U     in [0, M-1]

    lower-hinge arc v -> u : qx*(M-d) units at cost -(L-1), qx*d at -L
    upper-hinge arc u -> v : qy*(M-e) units at cost U,     qy*e at U+1

where qx/qy are the super-increasing hinge weights (all 1 for the plain
minimum; the S digit is added into every weight).
"""

from __future__ import annotations

from .solver import _validate, solve

GROUND = 0  # node index of the anchor / epoch, pinned to k = 0


class RepairError(Exception):
    """Repairs can only be opened against audits with status unsat."""


# ---------------------------------------------------------------------------
# Residual min-cost circulation (integer arcs, bulk augmentation)
# ---------------------------------------------------------------------------

class _Circulation:
    """Residual network with negative-cycle cancellation.

    Each adjacency entry is ``[to, rev, cap, cost]``; ``rev`` is the index of
    the reverse entry in ``adj[to]``.  Capacities and costs are arbitrary
    Python integers (weights can be very large super-increasing values).
    """

    def __init__(self, n):
        self.n = n
        self.adj = [[] for _ in range(n)]
        self.cost = 0  # accumulated cost of all augmented flow

    def add_arc(self, u, v, cap, cost):
        if cap <= 0:
            return
        fwd = [v, len(self.adj[v]), cap, cost]
        rev = [u, len(self.adj[u]), 0, -cost]
        self.adj[u].append(fwd)
        self.adj[v].append(rev)

    def _push(self, arcs, flow):
        for arc in arcs:
            arc[2] -= flow
            self.adj[arc[0]][arc[1]][2] += flow
            self.cost += flow * arc[3]

    def _negative_cycle(self):
        """A reachable negative residual cycle as a list of arcs, or None.

        Bellman-Ford seeded with distance 0 at every node finds any cycle;
        the predecessor walk of a node relaxed on the n-th pass reconstructs
        it.
        """
        n = self.n
        dist = [0] * n
        pred = [None] * n
        relaxed = None
        for _ in range(n):
            relaxed = None
            for u in range(n):
                du = dist[u]
                for arc in self.adj[u]:
                    v, _rev, cap, cost = arc
                    if cap > 0 and dist[v] > du + cost:
                        dist[v] = du + cost
                        pred[v] = (u, arc)
                        relaxed = v
            if relaxed is None:
                return None
        node = relaxed
        for _ in range(n):
            node = pred[node][0]
        cycle = []
        start = node
        while True:
            u, arc = pred[node]
            cycle.append(arc)
            node = u
            if node == start:
                break
        cycle.reverse()
        return cycle

    def solve(self):
        """Cancel negative cycles until the residual has none (min cost)."""
        guard = 0
        while True:
            cycle = self._negative_cycle()
            if cycle is None:
                return
            self._push(cycle, min(arc[2] for arc in cycle))
            guard += 1
            if guard > 1_000_000:
                raise AssertionError("circulation did not converge")


def _ceil_div(a: int, b: int) -> int:
    return -((-a) // b)


# ---------------------------------------------------------------------------
# Repair construction
# ---------------------------------------------------------------------------

def _residue(endpoint, anchor_tick, residues):
    return anchor_tick if endpoint == "anchor" else residues[endpoint]


def _violations_at_zero(constraints, anchor_tick, residues):
    """Total expansion required when every wrap count k_e is 0."""
    total = 0
    for c in constraints:
        delta = _residue(c.dst, anchor_tick, residues) \
            - _residue(c.src, anchor_tick, residues)
        total += max(0, c.lo - delta) + max(0, delta - c.hi)
    return total


def repair(payload, source_result):
    """Compute the minimum outward window relaxation of an unsat audit.

    Returns a frozen repair result: the canonical timeline, per-constraint
    original/relaxed windows, relaxation direction, recomputed differences,
    the decoded minimum total expansion and recomputation evidence.  Raises
    RepairError if the source audit is not unsat.
    """
    if not isinstance(source_result, dict) \
            or source_result.get("status") != "unsat":
        raise RepairError("repairs can only be opened against unsat audits")

    modulus, anchor_tick, events, constraints = _validate(payload)
    residues = {e["id"]: e["counter"] for e in events}
    event_ids = sorted(residues)
    cons = sorted(constraints, key=lambda c: c.id)  # tie-break order
    m = len(cons)

    # -- super-increasing weights -------------------------------------------
    # Every optimal component (x_i, y_i) and the total S are <= the cost at
    # k = 0, call it cap0; base R = cap0 + 1 makes the mixed-radix encoding
    # of (S, x_1, y_1, ..., x_m, y_m) unique.
    cap0 = _violations_at_zero(cons, anchor_tick, residues)
    radix = cap0 + 1
    # An unsat instance cannot be feasible at k=0, so cap0 >= 1; radix >= 2
    # is what keeps the mixed-radix encoding unambiguous.
    if cap0 < 1:
        raise AssertionError("unsat source reported zero violations at k=0")
    wS = radix ** (2 * m)
    weights = []  # per constraint: (qx, qy), qx/qy include the S digit
    for i in range(1, m + 1):
        wx = radix ** (2 * (m - i) + 1)
        wy = radix ** (2 * (m - i))
        weights.append((wS + wx, wS + wy))

    # -- circulation network --------------------------------------------------
    index = {eid: i + 1 for i, eid in enumerate(event_ids)}

    def node_of(endpoint):
        return GROUND if endpoint == "anchor" else index[endpoint]

    flow = _Circulation(len(event_ids) + 1)
    hinge_capacity = 0
    for q, c in enumerate(cons):
        u, v = node_of(c.src), node_of(c.dst)
        r_u = _residue(c.src, anchor_tick, residues)
        r_v = _residue(c.dst, anchor_tick, residues)
        a = c.lo + r_u - r_v
        b = c.hi + r_u - r_v
        L = _ceil_div(a, modulus)
        d = a - modulus * (L - 1)                 # in [1, M]
        U = b // modulus
        e = b - modulus * U                       # in [0, M-1]
        qx, qy = weights[q]
        flow.add_arc(v, u, qx * (modulus - d), -(L - 1))
        flow.add_arc(v, u, qx * d, -L)
        flow.add_arc(u, v, qy * (modulus - e), U)
        flow.add_arc(u, v, qy * e, U + 1)
        hinge_capacity += (qx + qy) * modulus

    # k_e >= 0: the gate must absorb every flow the hinges can carry at the
    # optimum, so its capacity strictly exceeds the total hinge capacity.
    gate_cap = hinge_capacity + 1
    for eid in event_ids:
        flow.add_arc(index[eid], GROUND, gate_cap, 0)

    flow.solve()
    encoded_cost = -flow.cost  # conjugate minimum == weighted primal min

    # -- decode the mixed-radix digits (least significant first) --------------
    extensions = []
    remaining = encoded_cost
    for i in range(m - 1, -1, -1):
        y_i = remaining % radix
        remaining //= radix
        x_i = remaining % radix
        remaining //= radix
        extensions.append((x_i, y_i))
    extensions.reverse()
    total = remaining
    if total < 0:
        raise AssertionError("repair decoded a negative total expansion")

    # -- per-constraint rows --------------------------------------------------
    window_rows = []
    relaxation_vector = []
    for c, (x_i, y_i) in zip(cons, extensions):
        if x_i and y_i:
            direction = "both"
        elif x_i:
            direction = "lower"
        elif y_i:
            direction = "upper"
        else:
            direction = "none"
        row = {
            "constraint_id": c.id,
            "src": c.src,
            "dst": c.dst,
            "original_window": [c.lo, c.hi],
            "relaxed_window": [c.lo - x_i, c.hi + y_i],
            "extend_lower": x_i,
            "extend_upper": y_i,
            "direction": direction,
        }
        window_rows.append(row)
        relaxation_vector.append(
            {"constraint_id": c.id, "extension": [x_i, y_i]})

    # -- recompute the canonical timeline with the frozen audit solver --------
    relaxed_payload = {
        "modulus": modulus,
        "anchor": {"id": "anchor", "tick": anchor_tick},
        "events": [{"id": e["id"], "counter": e["counter"]} for e in events],
        "constraints": [
            {"id": row["constraint_id"], "src": row["src"], "dst": row["dst"],
             "window": row["relaxed_window"]}
            for row in window_rows
        ],
    }
    verification = solve(relaxed_payload)
    if verification["status"] == "unsat":
        raise AssertionError("relaxed instance is still infeasible")
    if verification["status"] == "unique":
        canonical = verification["timeline"]
        wrap_counts = dict(verification["evidence"]["wrap_counts"])
        derivations = verification["evidence"]["derivations"]
        alt_timeline = None
    else:
        canonical = verification["timelines"][0]
        wrap_counts = {e["event"]: e["wrap_count"] for e in
                       canonical["entries"] if e["event"] != "anchor"}
        derivations = None
        alt_timeline = verification["timelines"][1]

    # Fill the recomputed differences from the canonical timeline.
    tick_of = {"anchor": anchor_tick}
    for entry in canonical["entries"]:
        tick_of[entry["event"]] = entry["tick"]
    summed = 0
    for row in window_rows:
        delta = tick_of[row["dst"]] - tick_of[row["src"]]
        row["recomputed_difference"] = delta
        lo2, hi2 = row["relaxed_window"]
        if not (lo2 <= delta <= hi2):
            raise AssertionError(
                f"recomputed difference {delta} outside relaxed window "
                f"{[lo2, hi2]} on {row['constraint_id']}")
        summed += row["extend_lower"] + row["extend_upper"]
    if summed != total:
        raise AssertionError("per-constraint extensions do not sum to total")

    # Cross-check the weighted primal encoded by the decoded digits.
    weighted_check = wS * total
    for (qx, qy), (x_i, y_i) in zip(weights, extensions):
        weighted_check += (qx - wS) * x_i + (qy - wS) * y_i
    if weighted_check != encoded_cost:
        raise AssertionError(
            f"optimality cross-check failed: {weighted_check} != "
            f"{encoded_cost}")

    return {
        "status": "repaired",
        "modulus": modulus,
        "total_expansion": total,
        "canonical_timeline": canonical,
        "alternate_timeline": alt_timeline,
        "wrap_counts": wrap_counts,
        "relaxation_vector": relaxation_vector,
        "windows": window_rows,
        "feasibility_verification": {
            "relaxed_status": verification["status"],
            "feasible": True,
            "relaxed_result": verification,
        },
        "derivations": derivations,
        "optimality_evidence": {
            "method": "integer_min_cost_circulation",
            "radix": radix,
            "encoded_cost": encoded_cost,
            "weighted_primal": weighted_check,
            "strong_duality": weighted_check == encoded_cost,
            "tie_break_constraint_ids": [c.id for c in cons],
            "tie_break_event_ids": event_ids,
            "hinge_tiers": [
                {
                    "constraint_id": c.id,
                    "L": _ceil_div(c.lo
                                  + _residue(c.src, anchor_tick, residues)
                                  - _residue(c.dst, anchor_tick, residues),
                                  modulus),
                    "U": (c.hi
                          + _residue(c.src, anchor_tick, residues)
                          - _residue(c.dst, anchor_tick, residues)) // modulus,
                }
                for c in cons
            ],
        },
    }
