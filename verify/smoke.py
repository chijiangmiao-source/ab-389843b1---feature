#!/usr/bin/env python3
"""HTTP smoke acceptance for the wraparound audit service.

Exercises the live API end to end:
  * health check
  * unique wrap expansion (spec example: B expands to 103)
  * ambiguous case: first two canonical timelines + first unstable precedence
  * unsat case: recomputable bidirectional conflict chain
  * idempotent records: replay returns the original audit number, a changed
    payload is rejected (409) and creates no new record
  * frozen record retrieval by audit number
  * minimal window-relaxation repairs on frozen unsat audits:
      - single-window repair (one tick),
      - canonical adjudication of equal-cost candidates,
      - illegal source rejection (non-unsat / unknown audit),
      - idempotent replay by repair_id, frozen repair retrieval,
      - compatible reads of the original (unchanged) audit.

Exit code 0 iff every check passes.
"""

import json
import os
import sys
import time
import urllib.error
import urllib.request
import uuid

BASE_URL = os.environ.get("BASE_URL", "http://audit:8080").rstrip("/")
TIMEOUT = 10

failures = []
checks = 0


def check(name, cond, detail=""):
    global checks
    checks += 1
    if cond:
        print(f"PASS {name}")
    else:
        failures.append(name)
        print(f"FAIL {name} {detail}")


def request(method, path, payload=None):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(BASE_URL + path, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def wait_ready(attempts=60):
    for _ in range(attempts):
        try:
            code, body = request("GET", "/health")
            if code == 200 and body.get("status") == "ok":
                return True
        except Exception:
            pass
        time.sleep(1)
    return False


def ticks(timeline):
    return {e["event"]: e["tick"] for e in timeline["entries"]}


def main():
    print(f"smoke against {BASE_URL}")
    check("health.ready", wait_ready(), "service did not become healthy")

    run = uuid.uuid4().hex[:12]

    def payload(rid, **over):
        base = {
            "request_id": rid,
            "modulus": 100,
            "anchor": {"id": "A", "tick": 95},
            "events": [{"id": "B", "counter": 3}],
            "constraints": [
                {"id": "ab", "src": "anchor", "dst": "B", "window": [8, 8]},
            ],
        }
        base.update(over)
        return base

    # -- unique expansion ----------------------------------------------------
    rid_u = f"smoke-unique-{run}"
    code, body = request("POST", "/audits", payload(rid_u))
    check("unique.created", code == 201, f"got {code}: {body}")
    check("unique.status", body.get("status") == "unique")
    tl = body.get("result", {}).get("timeline", {"entries": []})
    check("unique.b_is_103", ticks(tl).get("B") == 103, f"got {tl}")
    check("unique.anchor_is_95", ticks(tl).get("anchor") == 95)
    audit_unique = body.get("audit_no")

    # -- idempotent replay ----------------------------------------------------
    code, body2 = request("POST", "/audits", payload(rid_u))
    check("idem.replay_status", code == 200, f"got {code}")
    check("idem.same_audit_no",
          body2.get("audit_no") == audit_unique,
          f"{body2.get('audit_no')} != {audit_unique}")
    check("idem.replayed_flag", body2.get("replayed") is True)

    # -- changed payload rejected, no new record ------------------------------
    changed = payload(rid_u)
    changed["constraints"] = [dict(c) for c in changed["constraints"]]
    changed["constraints"][0]["window"] = [8, 9]
    code, body3 = request("POST", "/audits", changed)
    check("idem.conflict_409", code == 409, f"got {code}: {body3}")
    code, body4 = request("POST", "/audits", payload(rid_u))
    check("idem.still_replays", code == 200
          and body4.get("audit_no") == audit_unique)

    changed_event = payload(rid_u)
    changed_event["events"] = [{"id": "B", "counter": 4}]
    code, _ = request("POST", "/audits", changed_event)
    check("idem.changed_event_409", code == 409, f"got {code}")

    # -- ambiguous: two canonical timelines + unstable precedence -------------
    rid_a = f"smoke-ambiguous-{run}"
    amb = payload(rid_a, constraints=[
        {"id": "ab", "src": "anchor", "dst": "B", "window": [-92, 8]},
    ])
    code, body = request("POST", "/audits", amb)
    check("amb.created", code == 201, f"got {code}: {body}")
    check("amb.status", body.get("status") == "multiple")
    tls = body.get("result", {}).get("timelines", [])
    check("amb.two_timelines", len(tls) == 2)
    if len(tls) == 2:
        check("amb.first_two_canonical",
              ticks(tls[0]).get("B") == 3 and ticks(tls[1]).get("B") == 103,
              f"got {ticks(tls[0])}, {ticks(tls[1])}")
    unstable = body.get("result", {}).get("first_unstable_precedence") or {}
    check("amb.unstable_pair", unstable.get("pair") == ["anchor", "B"],
          f"got {unstable}")

    # -- unsat: recomputable bidirectional conflict chain ---------------------
    rid_c = f"smoke-unsat-{run}"
    unsat = payload(rid_c, constraints=[
        {"id": "ab", "src": "anchor", "dst": "B", "window": [8, 8]},
        {"id": "ba", "src": "B", "dst": "anchor", "window": [92, 92]},
    ])
    code, body = request("POST", "/audits", unsat)
    check("unsat.created", code == 201, f"got {code}: {body}")
    check("unsat.status", body.get("status") == "unsat")
    audit_unsat = body.get("audit_no")
    conflict = body.get("result", {}).get("conflict", {})
    check("unsat.chain", sorted(conflict.get("constraint_chain", []))
          == ["ab", "ba"], f"got {conflict.get('constraint_chain')}")
    check("unsat.recomputable",
          conflict.get("lower_bound_derivation", {}).get("derived_tick_min")
          == 103
          and conflict.get("upper_bound_derivation", {})
          .get("derived_tick_max") == 3,
          f"got {conflict}")

    # -- frozen record retrieval ----------------------------------------------
    code, rec = request("GET", f"/audits/{audit_unique}")
    check("get.frozen_200", code == 200, f"got {code}")
    check("get.frozen_input", rec.get("input", {}).get("modulus") == 100
          and rec.get("input", {}).get("events")
          == [{"id": "B", "counter": 3}])
    check("get.frozen_result", rec.get("result", {}).get("status") == "unique"
          and ticks(rec["result"]["timeline"]).get("B") == 103)
    check("get.frozen_evidence",
          rec.get("result", {}).get("evidence", {})
          .get("wrap_counts", {}).get("B") == 1)
    code, _ = request("GET", "/audits/99999999")
    check("get.missing_404", code == 404, f"got {code}")

    # -- validation ------------------------------------------------------------
    bad = payload(f"smoke-bad-{run}",
                  events=[{"id": "B", "counter": 3},
                          {"id": "Z", "counter": 1}])
    code, _ = request("POST", "/audits", bad)
    check("validation.disconnected_400", code == 400, f"got {code}")

    # -- minimal window-relaxation repairs ------------------------------------
    # 1. single-window repair: a modular residue conflict fixed by one tick.
    rid_res = f"smoke-repair-residue-{run}"
    residue = payload(
        f"smoke-unsat-residue-{run}",
        events=[{"id": "B", "counter": 4}],
        constraints=[
            {"id": "ab", "src": "anchor", "dst": "B", "window": [8, 8]}])
    code, body = request("POST", "/audits", residue)
    check("repair.source_unsat", code == 201
          and body.get("status") == "unsat", f"got {code}: {body}")
    audit_res = body.get("audit_no")
    code, body = request("POST", f"/audits/{audit_res}/repairs",
                         {"repair_id": rid_res})
    check("repair.single_created", code == 201, f"got {code}: {body}")
    result = body.get("result", {})
    check("repair.single_cost", result.get("total_extension") == 1,
          f"got {result.get('total_extension')}")
    check("repair.single_timeline",
          ticks(result.get("timeline", {})).get("B") == 104,
          f"got {result.get('timeline')}")
    rc = {c["id"]: c for c in result.get("constraints", [])}
    check("repair.single_window",
          rc.get("ab", {}).get("relaxed_window") == [7, 9]
          and rc["ab"].get("direction") == "upper"
          and rc["ab"].get("recomputed_difference") == 9,
          f"got {rc.get('ab')}")
    check("repair.frozen_source_ref",
          result.get("source", {}).get("audit_no") == audit_res
          and result["source"].get("original_status") == "unsat")
    repair_res_no = body.get("repair_no")

    # 2. canonical adjudication of equal-cost candidates.
    rid_tie = f"smoke-repair-tie-{run}"
    code, body = request("POST", f"/audits/{audit_unsat}/repairs",
                         {"repair_id": rid_tie})
    check("repair.tie_created", code == 201, f"got {code}: {body}")
    result = body.get("result", {})
    decision = result.get("canonical_decision", {})
    check("repair.tie_cost", result.get("total_extension") == 100)
    check("repair.tie_relax_vector",
          decision.get("constraint_order") == ["ab", "ba"]
          and decision.get("relax_vector") == [0, 100],
          f"got {decision}")
    check("repair.tie_timeline",
          ticks(result.get("timeline", {})).get("B") == 103
          and decision.get("wrap_vector") == [1],
          f"got {result.get('timeline')}")
    repair_tie_no = body.get("repair_no")

    # 3. illegal source rejection.
    code, _ = request("POST", f"/audits/{audit_unique}/repairs",
                      {"repair_id": f"smoke-repair-badstatus-{run}"})
    check("repair.non_unsat_409", code == 409, f"got {code}")
    code, _ = request("POST", "/audits/99999999/repairs",
                      {"repair_id": f"smoke-repair-noaudit-{run}"})
    check("repair.unknown_audit_404", code == 404, f"got {code}")
    code, _ = request("POST", f"/audits/{audit_unsat}/repairs", {})
    check("repair.missing_id_400", code == 400, f"got {code}")
    code, _ = request("POST", f"/audits/{audit_unsat}/repairs",
                      {"repair_id": rid_tie, "unexpected": 1})
    check("repair.changed_payload_400", code == 400, f"got {code}")

    # 4. idempotent replay + frozen retrieval + compatible source read.
    code, body = request("POST", f"/audits/{audit_unsat}/repairs",
                         {"repair_id": rid_tie})
    check("repair.replay_200", code == 200
          and body.get("repair_no") == repair_tie_no
          and body.get("replayed") is True, f"got {code}: {body}")
    code, rec = request("GET", f"/repairs/{repair_res_no}")
    check("repair.get_frozen", code == 200
          and rec.get("repair_id") == rid_res
          and rec.get("result", {}).get("total_extension") == 1
          and rec.get("source", {}).get("result", {}).get("status")
          == "unsat", f"got {code}: {rec}")
    code, _ = request("GET", "/repairs/99999999")
    check("repair.get_missing_404", code == 404, f"got {code}")
    code, src = request("GET", f"/audits/{audit_unsat}")
    check("repair.source_still_unsat",
          code == 200 and src.get("result", {}).get("status") == "unsat"
          and "repair" not in src, f"got {code}")

    print(f"\n{checks - len(failures)}/{checks} smoke checks passed")
    if failures:
        print("FAILED:", ", ".join(failures))
        return 1
    print("SMOKE OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
