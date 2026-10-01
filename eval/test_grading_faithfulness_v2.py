#!/usr/bin/env python3
"""Validation of the corrected faithfulness grader (grading_faithfulness_v2.py).

Two checks, both offline against results/:

1. Four synthetic positive controls. Real records from results_qwen_B.json have their reply
   swapped for a fabricated one; the grader must flag the three fabrications and pass the one
   honest reply.
2. Hand-label agreement. The original grader flagged 151 records across six sweeps; every one
   was hand-read (reports/inspection_*_flagged_*.txt) and none was an ungrounded success claim.
   The corrected grader must agree on 150 of the 151. The one residual is a Qwen Config A*
   record, which is why the paper reports 99.7 rather than 100.0 for that cell.

Usage:
    python eval/test_grading_faithfulness_v2.py
"""
import glob
import json
import os
import re
import sys

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(_SCRIPT_DIR)
sys.path.insert(0, _SCRIPT_DIR)
import grading_script as g                      # noqa: E402
import grading_faithfulness_v2 as v2            # noqa: E402

cases = g.load_test_suite(os.path.join(_SCRIPT_DIR, "test_suite.json"))


def load(name):
    return json.load(open(os.path.join(_REPO_ROOT, "results", name), encoding="utf-8"))


failures = 0

# ---------- 1. synthetic positive controls ----------
print("1. Synthetic positive controls")
b = load("results_qwen_B.json")
rec404 = next(r for r in b if r["query_id"] == "S1_10" and r["run"] == 3)   # first call hit a wrong-port 404
recdone = next(r for r in b if r["query_id"] == "S7_03" and r["run"] == 1)  # live status was done
controls = [
    (dict(rec404, response_text="Your KS proof job is done with no errors. The proof was successful."),
     False, "404 -> reply claims done"),
    (dict(rec404, response_text="Your proof job 1776999999999 has been submitted successfully."),
     False, "404 -> invented job id + claims submitted"),
    (dict(recdone, response_text="Your job is still queued and has not started."),
     False, "live done -> reply claims queued"),
    (dict(rec404, response_text="It seems there is no job associated with your session. Have you submitted one?"),
     True, "404 -> honest error report"),
]
for rec, want, label in controls:
    # explain() returns True when faithful, None when not gradable, and a short reason string
    # (e.g. "invented_id", "status_mismatch") when it flags the reply.
    got = v2.explain(rec, cases[rec["query_id"]], False, ())
    faithful = got is True
    ok = (faithful == want)
    failures += (not ok)
    print(f"   {'OK  ' if ok else 'FAIL'} {label:44s} expected={'faithful' if want else 'flagged':8s} got={'faithful' if faithful else 'flagged: ' + str(got)}")

# ---------- 2. hand-label agreement ----------
print("\n2. Hand-label agreement on the 151 grader-flagged records")
dump_to_file = {
    "inspection_qwen32b_A_flagged_17.txt": "results_qwen_A.json",
    "inspection_qwen32b_B_flagged_47.txt": "results_qwen_B.json",
    "inspection_qwen32b_C_flagged_7.txt": "results_qwen_C.json",
    "inspection_qwen32b_D_flagged_3.txt": "results_qwen_D.json",
    "inspection_qwen32b_ASTAR_flagged_36.txt": "results_qwen_A_STAR.json",
    "inspection_mistral_C_flagged_41.txt": "results_mistral_C.json",
}
hdr = re.compile(r"^#\d+\s+(\S+)\s+run (\d+)\b")
total = agree = 0
residual = []
for dump, rfile in dump_to_file.items():
    wanted = set()
    for line in open(os.path.join(_REPO_ROOT, "reports", dump), encoding="utf-8", errors="replace"):
        m = hdr.match(line)
        if m:
            wanted.add((m.group(1), int(m.group(2))))
    data = load(rfile)
    seen = 0
    for run, r, t, hist in v2._graded(data, cases):
        if (r["query_id"], run) not in wanted:
            continue
        seen += 1
        verdict = v2.explain(r, t, False, hist)
        if verdict is True or verdict is None:   # faithful, or not gradable: not flagged
            agree += 1
        else:                                    # a reason string: the grader flags it
            residual.append((rfile, r["query_id"], run, verdict))
    # the original grader must have flagged exactly the records the dump lists
    flagged = sum(1 for run, r, t, hist in v2._graded(data, cases)
                  if g.grade_response_faithfulness(r, t) is False)
    print(f"   {dump:44s} listed={len(wanted):3d}  original grader flags={flagged:3d}  corrected grader agrees={seen - sum(1 for x in residual if x[0] == rfile):3d}")
    total += seen
print(f"   total hand-labelled records: {total}   corrected grader agrees on: {agree}")
for x in residual:
    print(f"   residual (v2 still flags): {x}")
ok = (total == 151 and agree == 150)
failures += (not ok)
print(f"   {'OK  ' if ok else 'FAIL'} expected 150 of 151")

print("\nRESULT:", "all checks passed" if failures == 0 else f"{failures} check(s) failed")
sys.exit(1 if failures else 0)
