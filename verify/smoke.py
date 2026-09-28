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
  * minimum window-relaxation repairs opened against frozen unsat audits:
      - single-window minimum repair
      - canonical adjudication among equal-cost candidates
      - illegal source rejection (unknown audit / satisfiable audit)
      - original audits remain compatible and unmodified
      - fix marker idempotency and frozen repair retrieval

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

    # -- repairs: minimum window relaxation -----------------------------------
    # (a) single-window repair: modular residue conflict needs exactly 1 tick
    rid_f1 = f"smoke-fix-src-{run}"
    single = {
        "request_id": rid_f1,
        "modulus": 100,
        "anchor": {"id": "A", "tick": 95},
        "events": [{"id": "B", "counter": 4}],
        "constraints": [
            {"id": "ab", "src": "anchor", "dst": "B", "window": [8, 8]},
        ],
    }
    code, body = request("POST", "/audits", single)
    check("repair.source_unsat_created", code == 201, f"got {code}: {body}")
    check("repair.source_is_unsat", body.get("status") == "unsat")
    single_no = body.get("audit_no")
    code, fix = request("POST", f"/audits/{single_no}/repairs",
                        {"fix_id": f"fix-single-{run}"})
    check("repair.single_created", code == 201, f"got {code}: {fix}")
    check("repair.single_status", fix.get("status") == "repaired")
    check("repair.single_total_is_1",
          fix.get("total_expansion") == 1, f"got {fix.get('total_expansion')}")
    single_repair_no = fix.get("repair_no")
    win = {w["constraint_id"]: w
           for w in fix.get("result", {}).get("windows", [])}.get("ab", {})
    check("repair.single_window",
          win.get("original_window") == [8, 8]
          and win.get("relaxed_window") == [8, 9]
          and win.get("direction") == "upper"
          and win.get("recomputed_difference") == 9,
          f"got {win}")
    check("repair.single_timeline",
          ticks(fix["result"]["canonical_timeline"]).get("B") == 104,
          f"got {fix.get('result', {}).get('canonical_timeline')}")
    check("repair.single_duality",
          fix["result"]["optimality_evidence"]["strong_duality"] is True)

    # same fix_id + same payload replays the original repair number
    code, fix2 = request("POST", f"/audits/{single_no}/repairs",
                         {"fix_id": f"fix-single-{run}"})
    check("repair.replay_status", code == 200, f"got {code}")
    check("repair.replay_same_no",
          fix2.get("repair_no") == single_repair_no,
          f"{fix2.get('repair_no')} != {single_repair_no}")
    check("repair.replay_flag", fix2.get("replayed") is True)

    # frozen repair read by number
    code, rec = request("GET", f"/repairs/{single_repair_no}")
    check("repair.get_frozen_200", code == 200, f"got {code}")
    check("repair.get_frozen_content",
          rec.get("fix_id") == f"fix-single-{run}"
          and rec.get("source_audit_no") == single_no
          and rec.get("result", {}).get("total_expansion") == 1
          and rec.get("request", {}).get("payload", {})
          .get("constraints") == single["constraints"],
          f"got {rec}")
    code, _ = request("GET", "/repairs/99999999")
    check("repair.get_missing_404", code == 404, f"got {code}")

    # (b) multiple equal-cost candidates adjudicated canonically
    rid_f2 = f"smoke-fix-eq-{run}"
    equal = {
        "request_id": rid_f2,
        "modulus": 100,
        "anchor": {"id": "A", "tick": 95},
        "events": [{"id": "B", "counter": 3}],
        "constraints": [
            {"id": "ab", "src": "anchor", "dst": "B", "window": [8, 8]},
            {"id": "ba", "src": "B", "dst": "anchor", "window": [92, 92]},
        ],
    }
    code, body = request("POST", "/audits", equal)
    eq_no = body.get("audit_no")
    code, fix = request("POST", f"/audits/{eq_no}/repairs",
                        {"fix_id": f"fix-equal-{run}"})
    check("repair.equal_created", code == 201, f"got {code}: {fix}")
    check("repair.equal_total_100",
          fix.get("total_expansion") == 100,
          f"got {fix.get('total_expansion')}")
    wins = {w["constraint_id"]: w
            for w in fix.get("result", {}).get("windows", [])}
    check("repair.equal_vector_adjucated",
          fix.get("result", {}).get("relaxation_vector")
          == [{"constraint_id": "ab", "extension": [0, 0]},
              {"constraint_id": "ba", "extension": [100, 0]}]
          and wins.get("ab", {}).get("relaxed_window") == [8, 8]
          and wins.get("ba", {}).get("relaxed_window") == [-8, 92],
          f"got {fix.get('result', {}).get('relaxation_vector')}")
    check("repair.equal_timeline",
          ticks(fix["result"]["canonical_timeline"]).get("B") == 103
          and fix["result"]["wrap_counts"].get("B") == 1,
          f"got {fix.get('result', {}).get('canonical_timeline')}")
    check("repair.equal_diffs",
          wins.get("ab", {}).get("recomputed_difference") == 8
          and wins.get("ba", {}).get("recomputed_difference") == -8,
          f"got {wins}")

    # (c) illegal sources
    code, _ = request("POST", "/audits/99999999/repairs",
                      {"fix_id": f"fix-badsrc-{run}"})
    check("repair.unknown_source_404", code == 404, f"got {code}")
    code, body = request("POST", f"/audits/{audit_unique}/repairs",
                         {"fix_id": f"fix-satsrc-{run}"})
    check("repair.satisfiable_source_409", code == 409, f"got {code}: {body}")
    code, _ = request("POST", f"/audits/{single_no}/repairs", {})
    check("repair.missing_fix_id_400", code == 400, f"got {code}")
    # fix id reuse with a changed payload creates nothing
    tampered = json.loads(json.dumps(single))
    tampered["modulus"] = 99
    code, body = request("POST", f"/audits/{single_no}/repairs",
                         {"fix_id": f"fix-tamper-{run}", "payload": tampered})
    check("repair.changed_payload_409", code == 409, f"got {code}: {body}")

    # (d) original audits stay compatible and unmodified
    code, src = request("GET", f"/audits/{single_no}")
    check("repair.source_read_compatible",
          code == 200 and src.get("result", {}).get("status") == "unsat"
          and src.get("input", {}).get("modulus") == 100
          and src.get("input", {}).get("events") == single["events"]
          and src.get("input", {}).get("constraints")
          == single["constraints"],
          f"got {code} {src}")

    print(f"\n{checks - len(failures)}/{checks} smoke checks passed")
    if failures:
        print("FAILED:", ", ".join(failures))
        return 1
    print("SMOKE OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
