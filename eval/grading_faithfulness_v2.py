#!/usr/bin/env python3
"""Evidence-union faithfulness grader (v2) — companion to grading_script.py, which is left untouched.

Why this exists
---------------
grading_script.grade_response_faithfulness compares the final reply with the FIRST tool call only,
has no negation handling, and treats "submitted" as a success word.  Manual inspection of every
response it flagged for Qwen3-32B Configs A/B/C/D/A* and Mistral C (151 records) found no ungrounded
success claims: the flags were multi-call recoveries, negated error vocabulary ("no job has been
submitted"), omitted job ids, or restatements of get_session_context.  This grader agrees with those
hand labels on 150 of 151 records and catches four synthetic positive controls.

Rules (evidence = every tool output of the turn, plus earlier turns of the same session for the
history configs A and A_STAR):
  1. Any 13-digit job id in the reply must appear in some tool output/argument, the query, or history.
  2. If the reply asserts a status and the last tool call was check_status, the reply must agree with
     that live status (done accepts done/completed/finished/passed synonyms).
  3. A non-negated, non-hypothetical claim that the job was submitted / is done / was downloaded / was
     verified must be supported by a tool output (a job record supports "submitted"; a live or Redis
     "done" supports "done"; download success / verify ok support the others).
  4. Optional job-id echo (--echo): a tool-returned job id must appear in the reply (any tool of the turn).
  5. Turns with no tool call are N/A for faithfulness (as before) and are counted separately as
     "unbacked action claims" when the reply asserts an action on the user's job.

Usage:
    python eval/grading_faithfulness_v2.py                 # all results/results_*_*.json
    python eval/grading_faithfulness_v2.py --echo          # also print the echo-required variant
    python eval/grading_faithfulness_v2.py --files results/results_qwen_B.json
"""
import argparse
import collections
import glob
import json
import os
import re
import sys

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(_SCRIPT_DIR)
sys.path.insert(0, _SCRIPT_DIR)
import grading_script as g  # noqa: E402

DONE_SYN = ["done", "completed", "complete", "finished", "succeeded", "successful", "passed"]
HYPO = re.compile(r"\b(?:no|not|never|n't|without|cannot|can't|unable|before|until|once|after|if|when|whether|"
                  r"will|would|should|may|might|need|needs|ensure|confirm|check|checking|verify|wait|still|yet|"
                  r"likely|means|could|assuming|expect|hasn't|haven't|isn't|wasn't|weren't|aren't|couldn't)\b", re.I)
FRAME = re.compile(r"\b(?:if|might|may|could|possible|possibly|reasons?|happens? when|occurs? when|means?|indicate|"
                   r"suggest|need|needs|first|before)\b", re.I)
STATUS_WORDS = re.compile(r"\b(?:done|queued|running|complete|completed|finished|pending|in progress|in the queue|status)\b", re.I)
CLAIMS = {
    "submitted": re.compile(r"\b(?:has|have|was|were|is|are)\s+(?:been\s+|now\s+)?(?:successfully\s+)?(?:re-?)?submitted\b|"
                            r"\bsubmitted successfully\b|\bsuccessfully submitted\b|\bI(?:'ve| have) (?:re-?)?submitted\b", re.I),
    "done": re.compile(r"\b(?:has|have|is|are)\s+(?:already\s+|now\s+|been\s+)?(?:\*\*)?(?:done|complete|completed|finished)\b|"
                       r"\bcompleted successfully\b|\bstatus(?:\s+is|:)?\s*(?:\*\*|`|\")?done\b", re.I),
    "downloaded": re.compile(r"\b(?:has|have|was|were|is|are)\s+(?:been\s+|now\s+)?(?:successfully\s+)?downloaded\b|"
                             r"\bdownloaded successfully\b|\bsuccessfully downloaded\b", re.I),
    "verified": re.compile(r"\bverification (?:passed|succeeded|was successful)\b|\bproof is (?:cryptographically )?valid\b|"
                           r"\b(?:has been|was|is)\s+(?:successfully\s+)?verified\b", re.I),
}
ACTION = re.compile(r"(?:has been|have been|was|is|are|I've|I have)\s+(?:successfully\s+)?(?:downloaded|submitted|verified|re-?submitted)|"
                    r"proof (?:file )?has been downloaded", re.I)


def clean(txt):
    return re.sub(r"\n*\[(Not backed by|Backed by|Cryptographically)[^\]]*\]\s*$", "", txt or "").strip()


def claims_in(text):
    out = set()
    for kind, pat in CLAIMS.items():
        for m in pat.finditer(text):
            sent_start = max(text.rfind(".", 0, m.start()), text.rfind("\n", 0, m.start())) + 1
            sent = text[sent_start:m.end()]
            if HYPO.search(sent):
                continue
            back = text[max(0, m.start() - 160):m.start()]
            if FRAME.search(back) and (":" in back or "\n" in back):
                continue
            out.add(kind)
            break
    return out


def facts(tool_calls):
    """Union of success-shaped facts and job ids across tool outputs."""
    f, ids = set(), set()
    for c in tool_calls:
        resp = c.get("response") or ""
        ids |= set(re.findall(r"\b\d{13}\b", resp + json.dumps(c.get("arguments") or {})))
        try:
            d = json.loads(resp)
        except Exception:
            continue
        if not isinstance(d, dict):
            continue
        if d.get("job_id"):
            f.add("submitted")
        if str(d.get("status", "")).lower() in ("done", "completed"):
            f.add("done")
        if d.get("success") is True:
            f.add("downloaded")
        if d.get("ok") is True:
            f.add("verified")
        for j in d.get("jobs", []) or []:
            f.add("submitted")
            if str(j.get("status", "")).lower() in ("done", "completed"):
                f.add("done")
    return f, ids


def grade_faithfulness_v2(result, test_case, require_echo=False, history=()):
    """Returns True, False, or None (not gradable). Reason strings are available via explain()."""
    return explain(result, test_case, require_echo, history) is True


def explain(result, test_case, require_echo=False, history=()):
    if test_case.get("scoring") == "qualitative" or result.get("error"):
        return None
    tcs = result.get("tool_calls") or []
    if not tcs:
        return None
    reply = clean(result.get("response_text")).lower()
    f, ids = facts(tcs)
    hf, hids = facts([c for h in history for c in (h.get("tool_calls") or [])])
    for i in set(re.findall(r"\b\d{13}\b", reply)):
        if i not in ids and i not in hids and i not in (result.get("query_text") or ""):
            return "invented_id"
    if require_echo:
        tj = set()
        for c in tcs:
            try:
                d = json.loads(c.get("response") or "")
                if isinstance(d, dict) and d.get("job_id"):
                    tj.add(str(d["job_id"]))
            except Exception:
                pass
        if tj and not any(j in reply for j in tj):
            return "id_echo"
    if tcs[-1]["tool_name"] == "check_status" and STATUS_WORDS.search(reply):
        try:
            ls = str(json.loads(tcs[-1]["response"]).get("status", "")).lower()
        except Exception:
            ls = ""
        if ls == "done":
            if not any(s in reply for s in DONE_SYN):
                return "status_mismatch"
        elif ls and ls not in reply and not any(s in reply for s in ["in the queue", "in progress", "pending", "waiting", "not yet", "hasn't"]):
            return "status_mismatch"
    for kind in claims_in(clean(result.get("response_text"))):
        if kind not in f and kind not in hf:
            return "unsupported_claim"
    return True


def unbacked_action_claim(result):
    """Zero-tool turn whose reply asserts an action on the user's job (not visible to faithfulness)."""
    if result.get("tool_calls") or result.get("error"):
        return False
    txt = clean(result.get("response_text"))
    for m in ACTION.finditer(txt):
        sent_start = max(txt.rfind(".", 0, m.start()), txt.rfind("\n", 0, m.start())) + 1
        sent = txt[sent_start:m.end() + 1]
        if HYPO.search(sent):
            continue
        if re.search(r"\byour\b|\bjob\b|\bfile\b|\bI've\b|\bI have\b", sent, re.I):
            return True
    return False


def _graded(data, cases):
    """Yield (run, record, test_case, history) honoring the same cascade exclusion as grading_script."""
    hist_cfg = any(r.get("config") in ("A", "A_STAR") for r in data[:1])
    by_run = collections.defaultdict(list)
    for r in data:
        by_run[r["run"]].append(r)
    for run, rr in sorted(by_run.items()):
        s6 = collections.defaultdict(list)
        for r in rr:
            if r.get("scenario") is not None:
                s6[r["scenario"]].append(r)
        unreach = set()
        for sc, rs in s6.items():
            for qid, mk in g.mark_unreachable_steps(rs, cases).items():
                if mk == "unreachable":
                    unreach.add(qid)
        for r in rr:
            t = cases.get(r["query_id"])
            if not t or r["query_id"] in unreach:
                continue
            hist = [x for x in data if hist_cfg and x["session_id"] == r["session_id"] and (x.get("step") or 0) < (r.get("step") or 0)]
            yield run, r, t, hist


def _rate(data, cases, fn):
    per = collections.defaultdict(list)
    for run, r, t, hist in _graded(data, cases):
        per[run].append(fn(r, t, hist))
    return g.compute_mean_std([g.compute_rate(v) for k, v in sorted(per.items())])


def _fmt(m):
    return "  N/A " if m[0] is None else f"{m[0]:5.1f}+-{m[1]:3.1f}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--files", nargs="*", default=None)
    ap.add_argument("--test-suite", default=os.path.join(_SCRIPT_DIR, "test_suite.json"))
    ap.add_argument("--echo", action="store_true", help="also print the echo-required variant")
    args = ap.parse_args()
    cases = g.load_test_suite(args.test_suite)
    # results_A_STAR.json (no model prefix) is a leftover working copy identical to results_mistral_A_STAR.json.
    files = args.files or sorted(f for f in glob.glob(os.path.join(_REPO_ROOT, "results", "results_*_*.json"))
                                 if "pilot" not in f and "verify" not in f
                                 and os.path.basename(f) != "results_A_STAR.json")
    hdr = f"{'file':38s} {'original':>12s} {'v2':>12s}" + (f" {'v2+echo':>12s}" if args.echo else "") + f" {'unbacked action claims':>24s}"
    print(hdr)
    print("-" * len(hdr))
    for path in files:
        data = json.load(open(path, encoding="utf-8"))
        o = _rate(data, cases, lambda r, t, h: g.grade_response_faithfulness(r, t))
        v = _rate(data, cases, lambda r, t, h: (None if explain(r, t, False, h) is None else explain(r, t, False, h) is True))
        row = f"{os.path.basename(path):38s} {_fmt(o):>12s} {_fmt(v):>12s}"
        if args.echo:
            e = _rate(data, cases, lambda r, t, h: (None if explain(r, t, True, h) is None else explain(r, t, True, h) is True))
            row += f" {_fmt(e):>12s}"
        ua = [(r["query_id"], r["run"]) for r in data if unbacked_action_claim(r)]
        row += f" {len(ua):>24d}"
        print(row)
        if ua:
            print(f"{'':38s}   {ua}")


if __name__ == "__main__":
    main()
