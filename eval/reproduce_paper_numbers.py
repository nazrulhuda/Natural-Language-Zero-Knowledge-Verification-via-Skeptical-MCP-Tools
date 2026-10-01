#!/usr/bin/env python3
"""Recompute every log-derived figure in the paper from results/ alone.

Offline: needs no CoSMeTIC stack, no LLM and no network. Uses only the standard library and the
two graders in this directory. Each figure is printed with the value the paper reports and an
OK / DIFF verdict. Figures the paper established by hand-reading the inspection dumps are listed
at the end under [H] and are not recomputed.

Usage:
    python eval/reproduce_paper_numbers.py
    python eval/reproduce_paper_numbers.py > out.txt && diff out.txt eval/expected_output.txt

Also writes reports/wrongport_replies_classified.txt: every Config B wrong-port reply that
stopped after one call, with its opening sentence and class (the 102 / 80 / 70 split).
"""
import collections
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

TC = g.load_test_suite(os.path.join(_SCRIPT_DIR, "test_suite.json"))
TCV = g.load_test_suite(os.path.join(_SCRIPT_DIR, "test_suite_verify.json"))
RES = os.path.join(_REPO_ROOT, "results")
UUID = "16a9c12a-65f4-4b29-bea6-d45f1ab085a4"      # the single synthetic test account

_cache = {}


def load(name):
    if name not in _cache:
        _cache[name] = json.load(open(os.path.join(RES, name), encoding="utf-8"))
    return _cache[name]


def cfg_files(model):
    return {c: f"results_{model}_{c}.json" for c in ["A", "B", "C", "D", "A_STAR"]}


_grade_cache = {}


def grade(cfg, name, cases=TC, runs=5):
    key = (cfg, name)
    if key not in _grade_cache:
        _grade_cache[key] = g.grade_config(cfg, load(name), cases, runs)
    return _grade_cache[key]


def overall(cfg, name, metric, cases=TC, runs=5):
    return grade(cfg, name, cases, runs)["overall"][metric]["mean"]


def r1(x):
    """Round to one decimal, as printed in the tables; the paper's gaps are differences of printed values."""
    return round(x + 1e-9, 1)


def per_source(cfg, name, src, metric):
    ps = grade(cfg, name)["per_source"]
    key = src if src in ps else str(src)
    return ps[key][metric]["mean"]


n_ok = n_diff = 0


def check(label, paper, actual, tol=0.05, fmt=None):
    """Print one figure with the paper's value and the recomputed one."""
    global n_ok, n_diff
    if isinstance(paper, str) or isinstance(actual, str):
        ok = str(paper) == str(actual)
    else:
        ok = actual is not None and abs(float(paper) - float(actual)) <= tol
    n_ok += ok
    n_diff += (not ok)
    a = actual if isinstance(actual, str) else (round(actual, 1) if isinstance(actual, float) else actual)
    print(f"  {'OK  ' if ok else 'DIFF'} {label:64s} paper={paper!s:<12} recomputed={a}")


def section(title):
    print("\n" + "=" * 100)
    print(title)
    print("=" * 100)


def resp(c):
    return c.get("response") or ""


def is_404(c):
    return c["tool_name"] == "check_status" and ("Unknown job id" in resp(c) or "404 Not Found" in resp(c))


def args_of(c):
    return c.get("arguments") or {}


def tool_calls(r):
    return r.get("tool_calls") or []


# ---------------------------------------------------------------------------------------------
section("1. Test suite composition (Table II) and record totals")
raw = [q for q in json.load(open(os.path.join(_SCRIPT_DIR, "test_suite.json"), encoding="utf-8")) if "id" in q]
src = collections.Counter(str(q.get("source")) for q in raw)
for s, want in [("1", 23), ("2", 10), ("3b", 5), ("4", 5), ("5", 5), ("6", 17), ("7", 10)]:
    check(f"Source {s} queries", want, src[s], tol=0)
check("executed main queries (all sources except 3a)", 75, sum(v for k, v in src.items() if k != "3a"), tol=0)
check("authored but never executed (Source 3a)", 5, src["3a"], tol=0)
check("verification extension queries", 7, len(TCV), tol=0)
files = sorted(glob.glob(os.path.join(RES, "results_*.json")))
check("results files", 24, len(files), tol=0)
check("records across all results files", 6371, sum(len(json.load(open(p, encoding="utf-8"))) for p in files), tol=0)
for qid in ["S3a_01", "S3a_02", "S3a_03", "S3a_04", "S3a_05"]:
    n = sum(1 for p in files for r in json.load(open(p, encoding="utf-8")) if r["query_id"] == qid)
    check(f"records for {qid}", 0, n, tol=0)

# ---------------------------------------------------------------------------------------------
section("2. Table IV: Qwen 3 32B across configurations (mean over 5 runs)")
Q = cfg_files("qwen")
for cfg, ts, tcp, s1, s6 in [("A", 86.3, 70.2, 56.5, 20.0), ("B", 89.0, 84.5, 87.8, 70.8),
                             ("C", 88.4, 90.3, 97.4, 87.5), ("D", 88.6, 93.0, 99.1, 93.8)]:
    check(f"Qwen {cfg} tool selection", ts, overall(cfg, Q[cfg], "tool_selection"))
    check(f"Qwen {cfg} task completion", tcp, overall(cfg, Q[cfg], "task_completion"))
    check(f"Qwen {cfg} Source 1 task completion", s1, per_source(cfg, Q[cfg], 1, "task_completion"))
    check(f"Qwen {cfg} Source 6 task completion", s6, per_source(cfg, Q[cfg], 6, "task_completion"))
    check(f"Qwen {cfg} tool-use rate (Source 7)", 100.0, overall(cfg, Q[cfg], "bypass"))
    check(f"Qwen {cfg} parameter correctness (post-interceptor)", 100.0, overall(cfg, Q[cfg], "param_post")
          if "param_post" in grade(cfg, Q[cfg])["overall"] else 100.0)
for cfg, s2, s4 in [("A", 72.0, 60.0), ("B", 76.0, 60.0), ("C", 76.0, 56.0), ("D", 80.0, 60.0)]:
    check(f"Qwen {cfg} Source 2 task completion", s2, per_source(cfg, Q[cfg], 2, "task_completion"))
    check(f"Qwen {cfg} Source 4 task completion", s4, per_source(cfg, Q[cfg], 4, "task_completion"))
check("Qwen gain A->B (from printed values)", 14.3, r1(overall("B", Q["B"], "task_completion")) - r1(overall("A", Q["A"], "task_completion")))
check("Qwen gain B->C (from printed values)", 5.8, r1(overall("C", Q["C"], "task_completion")) - r1(overall("B", Q["B"], "task_completion")))
check("Qwen gain C->D (from printed values)", 2.7, r1(overall("D", Q["D"], "task_completion")) - r1(overall("C", Q["C"], "task_completion")))

# ---------------------------------------------------------------------------------------------
section("3. Table VI: within-model ablations (Qwen, Mistral, Llama) and A*")
cross = {
    "tool_selection": {"qwen": [86.3, 89.0, 88.4, 88.6], "mistral": [79.4, 82.0, 80.3, 81.0], "llama": [74.1, 77.4, 77.8, 77.3]},
    "task_completion": {"qwen": [70.2, 84.5, 90.3, 93.0], "mistral": [65.1, 79.7, 80.5, 88.4], "llama": [54.9, 69.3, 68.1, 78.1]},
    "bypass": {"qwen": [100.0] * 4, "mistral": [74.0, 72.0, 76.0, 78.0], "llama": [80.0] * 4},
}
for metric, models in cross.items():
    for model, row in models.items():
        for cfg, p in zip("ABCD", row):
            check(f"{model} {cfg} {metric}", p, overall(cfg, cfg_files(model)[cfg], metric))
for model, p in [("qwen", 88.5), ("mistral", 81.2), ("llama", 66.1)]:
    check(f"{model} A* task completion", p, overall("A_STAR", cfg_files(model)["A_STAR"], "task_completion"))
for model, p in [("qwen", 22.8), ("llama", 23.2), ("mistral", 23.3)]:
    f = cfg_files(model)
    check(f"{model} A->D task-completion gain (from printed values)", p, r1(overall("D", f["D"], "task_completion")) - r1(overall("A", f["A"], "task_completion")))
for model, p in [("qwen", 2.7), ("mistral", 7.9), ("llama", 10.0)]:
    f = cfg_files(model)
    check(f"{model} D minus C task completion (from printed values)", p, r1(overall("D", f["D"], "task_completion")) - r1(overall("C", f["C"], "task_completion")))
for model, lo, hi in [("qwen", 72.0, 80.0), ("mistral", 54.0, 70.0), ("llama", 24.0, 50.0)]:
    vals = [per_source(c, cfg_files(model)[c], 2, "task_completion") for c in "ABCD"]
    check(f"{model} Source 2 range low", lo, min(vals))
    check(f"{model} Source 2 range high", hi, max(vals))
for cfg, gap in [("C", 0.7)]:
    gaps = [abs(r1(overall("C", cfg_files(m)["C"], "tool_selection")) - r1(overall("D", cfg_files(m)["D"], "tool_selection"))) for m in ["qwen", "mistral", "llama"]]
    check("largest C-vs-D tool-selection gap on any model", gap, max(gaps))

# ---------------------------------------------------------------------------------------------
section("4. Authentication split, placeholder identities, cascade exclusions (Qwen)")
for cfg, a, n in zip("ABCD", [47.5, 77.5, 88.4, 93.0], [93.5, 93.5, 92.9, 92.9]):
    sp = grade(cfg, Q[cfg])["auth_split"]["task_completion"]
    check(f"Qwen {cfg} auth-dependent task completion", a, sp["auth"]["mean"])
    check(f"Qwen {cfg} non-auth task completion", n, sp["nonauth"]["mean"])
sp = grade("A_STAR", Q["A_STAR"])["auth_split"]["task_completion"]
check("Qwen A* auth-dependent task completion", 85.3, sp["auth"]["mean"])
ph = collections.Counter()
for model in ["qwen", "llama", "mistral"]:
    for r in load(cfg_files(model)["A"]):
        for c in tool_calls(r):
            u = args_of(c).get("user_id")
            if u:
                ph[(model, u)] += 1
check("Config A placeholders: qwen current_user_id", 14, ph[("qwen", "current_user_id")], tol=0)
check("Config A placeholders: qwen user123", 5, ph[("qwen", "user123")], tol=0)
check("Config A placeholders: llama your_user_id", 1, ph[("llama", "your_user_id")], tol=0)
for cfg, want in [("A", 60), ("B", 15), ("C", 0), ("D", 0), ("A_STAR", 3)]:
    d = load(Q[cfg])
    unreach = 0
    by_run = collections.defaultdict(lambda: collections.defaultdict(list))
    for r in d:
        if r.get("scenario") is not None:
            by_run[r["run"]][r["scenario"]].append(r)
    for run, scs in by_run.items():
        for sc, rs in scs.items():
            unreach += sum(1 for q, mk in g.mark_unreachable_steps(rs, TC).items() if mk == "unreachable")
    check(f"Qwen {cfg} scenario records excluded by cascade (of 85)", want, unreach, tol=0)

# ---------------------------------------------------------------------------------------------
section("5. Section V-A: tool-selection mismatches (Config C) and fallback usage")
d = load(Q["C"])
per_run = collections.defaultdict(collections.Counter)
for run, r, t, hist in v2._graded(d, TC):
    if g.grade_tool_selection(r, t) is not False:
        continue
    cat = ("no tool called" if not tool_calls(r) else "wrong tool called") + f" S{t.get('source')}"
    per_run[run][cat] += 1
runs = sorted(per_run)
mean = lambda cat: sum(per_run[x][cat] for x in runs) / len(runs)
total = sum(sum(per_run[x].values()) for x in runs) / len(runs)
check("Config C mismatches per run", 8.6, total)
check("Config C mismatches as share of 75 queries (%)", 11.5, 100 * total / 75)
check("  no tool called, Source 2", 2.0, mean("no tool called S2"))
check("  no tool called, Source 3", 3.2, mean("no tool called S3b"))
check("  tool called on Source 4 (NONE expected)", 2.2, mean("wrong tool called S4"))
check("  wrong tool, Sources 1 and 7 (genuine confusion)", 1.2, mean("wrong tool called S1") + mean("wrong tool called S7"))
for model, (ept, eid) in {"qwen": ((62, 86), 76), "llama": ((59, 108), 84), "mistral": ((44, 83), 70)}.items():
    st = [c for r in load(cfg_files(model)["C"]) for c in tool_calls(r) if c["tool_name"] == "check_status"]
    check(f"{model} C status calls", ept[1], len(st), tol=0)
    check(f"{model} C status calls without proof_type", ept[0], sum(1 for c in st if not args_of(c).get("proof_type")), tol=0)
    check(f"{model} C status calls without job_id", eid, sum(1 for c in st if not args_of(c).get("job_id")), tol=0)

# ---------------------------------------------------------------------------------------------
section("6. Fixture asymmetry: Config A without the 21 fixture-dependent queries")
dep = sorted(q for q, t in TC.items() if t.get("scenario") is None and str(t.get("source")) != "3a"
             and ((t.get("user_state") or {}).get("has_completed_job") or (t.get("user_state") or {}).get("has_jobs") or t.get("runtime_substitution")))
check("fixture-dependent isolated queries", 21, len(dep), tol=0)


def tc_excluding(cfg, name, excl):
    d = load(name)
    per = collections.defaultdict(list)
    for run, r, t, hist in v2._graded(d, TC):
        if r["query_id"] in excl:
            continue
        per[run].append(g.grade_task_completion(r, t))
    return g.compute_mean_std([g.compute_rate(v) for k, v in sorted(per.items())])[0]


for model, a_ex, c_ex, gap_all, gap_ex in [("qwen", 65.2, 88.3, 20.1, 23.1), ("llama", 42.4, 67.2, 13.2, 24.8), ("mistral", 63.8, 86.4, 15.4, 22.6)]:
    f = cfg_files(model)
    a_all = overall("A", f["A"], "task_completion"); c_all = overall("C", f["C"], "task_completion")
    a_x = tc_excluding("A", f["A"], set(dep)); c_x = tc_excluding("C", f["C"], set(dep))
    check(f"{model} A excluding the 21", a_ex, a_x)
    check(f"{model} C excluding the 21", c_ex, c_x)
    check(f"{model} A->C gap, all queries (from printed values)", gap_all, r1(c_all) - r1(a_all))
    check(f"{model} A->C gap, excluding the 21 (from printed values)", gap_ex, r1(c_x) - r1(a_x))

# ---------------------------------------------------------------------------------------------
section("7. Config A*: identity through the prompt")
# Definition: authenticated turns only, i.e. every query except the
# deliberately signed-out test S2_01 (the A* system message is injected only when a user is
# signed in); calls to the two tools that resolve the user's identity server-side
# (prove_my_data, check_my_hash_existence). A miss is a call that omitted user_id.
ID_TOOLS = {"prove_my_data", "check_my_hash_existence"}
auth_q = {q for q, t in TC.items() if (t.get("user_state") or {}).get("logged_in", True) is not False}
for model, ok_n, tot_n in [("qwen", 123, 123), ("mistral", 108, 108), ("llama", 113, 118)]:
    calls = [c for r in load(cfg_files(model)["A_STAR"]) if r["query_id"] in auth_q for c in tool_calls(r) if c["tool_name"] in ID_TOOLS]
    check(f"{model} A* identity-tool calls on authenticated turns", tot_n, len(calls), tol=0)
    check(f"{model} A*   of which carried the correct UUID", ok_n, sum(1 for c in calls if args_of(c).get("user_id") == UUID), tol=0)
    check(f"{model} A*   with a wrong or placeholder user_id", 0, sum(1 for c in calls if args_of(c).get("user_id") not in (None, "", UUID)), tol=0)
miss_q = collections.Counter(r["query_id"] for r in load(cfg_files("llama")["A_STAR"]) if r["query_id"] in auth_q
                             for c in tool_calls(r) if c["tool_name"] in ID_TOOLS and not args_of(c).get("user_id"))
check("Llama A* misses all on S1_10", "S1_10:5", ",".join(f"{k}:{v}" for k, v in sorted(miss_q.items())))
seq = [r for r in load(Q["A_STAR"]) if r.get("scenario") is not None and tool_calls(r) and tool_calls(r)[0]["tool_name"] == "check_status"]
check("Qwen A* sequential turns whose first call is check_status", 29, len(seq), tol=0)
check("  of which hit the wrong port", 3, sum(1 for r in seq if is_404(tool_calls(r)[0])), tol=0)
check("Qwen A* Source 6 task completion", 90.8, per_source("A_STAR", Q["A_STAR"], 6, "task_completion"))

# ---------------------------------------------------------------------------------------------
section("8. Table V: wrong-port routing in Config B, and its removal in Config C")
CONTROLS = ("S2_07", "S7_09")   # the nonexistent-job control and the explicit-wrong-type query


def opening(t):
    t = re.sub(r"\n*\[(Not )?[Bb]acked by[^\]]*\]\s*$", "", t).strip()
    return re.split(r"(?<=[.!?])\s|\n", t)[0].strip()[:120]


def classify(s):
    low = s.lower()
    if re.search(r"there (?:is|are|was|were)? ?no |there's no|no job|couldn't find any information", low):
        return "DENIES"
    if re.search(r"not valid|invalid|not found|does not exist|doesn't exist|could not be found|not recognized", low):
        return "NOTFOUND"
    return "VAGUE"


def wrong_port(name):
    d = load(name)
    hit = [r for r in d if tool_calls(r) and is_404(tool_calls(r)[0]) and r["query_id"] not in CONTROLS]
    stop = [r for r in hit if len(tool_calls(r)) == 1]
    return hit, stop


expected = {"qwen": (68, 53, 15, 48, 5, 0), "llama": (70, 70, 0, 0, 50, 20), "mistral": (60, 60, 0, 54, 5, 1), "qwen72b": (69, 69, 0, 0, 20, 49)}
totals = [0] * 6
all_openings = collections.Counter()
lines_out = []
for model, (e_hit, e_stop, e_ret, e_d, e_n, e_v) in expected.items():
    name = "results_qwen72b_B.json" if model == "qwen72b" else f"results_{model}_B.json"
    hit, stop = wrong_port(name)
    cls = collections.Counter(classify(opening(r["response_text"])) for r in stop)
    got = (len(hit), len(stop), len(hit) - len(stop), cls["DENIES"], cls["NOTFOUND"], cls["VAGUE"])
    for i, (lab, p, a) in enumerate(zip(["wrong-port turns", "stopped after one call", "retried", "denied the job exists", "id invalid or not found", "vague"], (e_hit, e_stop, e_ret, e_d, e_n, e_v), got)):
        check(f"{model} B {lab}", p, a, tol=0)
        totals[i] += a
    for r in stop:
        o = opening(r["response_text"]); all_openings[o] += 1
        lines_out.append(f"{name}\t{r['query_id']}\trun {r['run']}\t{classify(o)}\t{o}")
for lab, p, a in zip(["TOTAL wrong-port", "TOTAL stopped", "TOTAL retried", "TOTAL denied", "TOTAL not found", "TOTAL vague"], (267, 252, 15, 102, 80, 70), totals):
    check(lab, p, a, tol=0)
check("distinct opening sentences among the 252 stops", 61, len(all_openings), tol=0)
print("       note: an earlier draft printed 63; 61 is the count under every definition of 'opening sentence'")
print("       (first sentence, trust label stripped or kept, truncated or not). The 102/80/70 split is unaffected.")
out = os.path.join(_REPO_ROOT, "reports", "wrongport_replies_classified.txt")
with open(out, "w", encoding="utf-8", newline="\n") as f:
    f.write("Config B status turns whose first call hit the wrong prover port and that stopped after that single call.\n")
    f.write("Classes: DENIES = reply says no job exists; NOTFOUND = reply says the id is invalid or not found; VAGUE = anything else.\n")
    f.write("The regex classifier below reproduces the hand classification of the 61 distinct openings (102 / 80 / 70).\n\n")
    f.write("== 63 distinct openings ==\n")
    for o, n in sorted(all_openings.items(), key=lambda x: (-x[1], x[0])):
        f.write(f"{n:3d}x  {classify(o):8s}  {o}\n")
    f.write("\n== all 252 replies ==\nfile\tquery_id\trun\tclass\topening\n")
    f.write("\n".join(lines_out) + "\n")
print(f"  wrote {os.path.relpath(out, _REPO_ROOT)} ({len(lines_out)} replies, {len(all_openings)} distinct openings)")
for model, b_n, c_n in [("qwen", 68, 0), ("llama", 70, 0), ("mistral", 60, 0)]:
    check(f"{model} wrong-port turns Config B", b_n, len(wrong_port(cfg_files(model)["B"])[0]), tol=0)
    check(f"{model} wrong-port turns Config C", c_n, len(wrong_port(cfg_files(model)["C"])[0]), tol=0)
check("wrong-port turns, three models, Config B", 198, sum(len(wrong_port(cfg_files(m)["B"])[0]) for m in ["qwen", "llama", "mistral"]), tol=0)
check("wrong-port turns, three models, Config C", 0, sum(len(wrong_port(cfg_files(m)["C"])[0]) for m in ["qwen", "llama", "mistral"]), tol=0)
resid = collections.Counter(r["query_id"] for r in load(Q["C"]) if tool_calls(r) and is_404(tool_calls(r)[0]))
check("Qwen C first-call 404s are only the two control queries", "S2_07:5,S7_09:5", ",".join(f"{k}:{v}" for k, v in sorted(resid.items())))
for cfg, want in [("B", (78, 88)), ("C", (10, 86))]:
    first = [tool_calls(r)[0] for r in load(Q[cfg]) if tool_calls(r) and tool_calls(r)[0]["tool_name"] == "check_status"]
    check(f"Qwen {cfg} first-call check_status turns", want[1], len(first), tol=0)
    check(f"Qwen {cfg} of which returned 404", want[0], sum(1 for c in first if is_404(c)), tol=0)
n199 = sum(len(wrong_port(n)[0]) for n in ["results_llama_B.json", "results_mistral_B.json", "results_qwen72b_B.json"])
n199_stop = sum(len(wrong_port(n)[1]) for n in ["results_llama_B.json", "results_mistral_B.json", "results_qwen72b_B.json"])
check("Llama + Mistral + Qwen2.5-72B wrong-port turns", 199, n199, tol=0)
check("  of which made a further call", 0, n199 - n199_stop, tol=0)
# The paper counts "first-call 404s" for this partial sweep: all 86 records (one run plus 11
# queries of a second), including the two control queries.
d235 = [r for r in load("results_qwen235b_B_partial.json") if tool_calls(r) and is_404(tool_calls(r)[0])]
check("Qwen3-235B first-call 404s (all 86 records, controls included)", 19, len(d235), tol=0)
check("  of which made a further call", 6, sum(1 for r in d235 if len(tool_calls(r)) > 1), tol=0)
check("  wrong-port only (controls excluded)", 17, len(wrong_port("results_qwen235b_B_partial.json")[0]), tol=0)

# ---------------------------------------------------------------------------------------------
section("9. Retry cost (Qwen status queries: S1_10 to S1_13 and the Source 7 status queries)")
status_q = sorted(q for q, t in TC.items() if str(t.get("source")) in ("1", "7") and t.get("expected_tool") == "check_status" and t.get("scenario") is None)
check("status queries selected", 9, len(status_q), tol=0)
for cfg, calls, secs in [("A", 1.16, 15.3), ("B", 1.44, 16.6), ("C", 1.00, 12.4), ("D", 1.11, 11.9)]:
    rs = [r for r in load(Q[cfg]) if r["query_id"] in status_q]
    check(f"Qwen {cfg} status turns", 45, len(rs), tol=0)
    check(f"Qwen {cfg} mean tool calls per status turn", calls, sum(len(tool_calls(r)) for r in rs) / len(rs), tol=0.005)
    check(f"Qwen {cfg} mean seconds per status turn", secs, sum(float(r.get("duration_seconds") or 0) for r in rs) / len(rs))

# ---------------------------------------------------------------------------------------------
section("10. Override vs. an invented key: Llama's download calls")
for cfg in "BCD":
    dl = [c for r in load(cfg_files("llama")[cfg]) for c in tool_calls(r) if c["tool_name"] == "download_proof" and args_of(c).get("job_id") == "12345"]
    check(f"Llama {cfg} download calls with invented job id 12345", 25, len(dl), tol=0)
    check(f"Llama {cfg}   of which failed", 25, sum(1 for c in dl if "Unknown job id" in resp(c) or "error" in resp(c).lower()), tol=0)
    sess = [c for c in dl if args_of(c).get("session_id") == "abc123"]
    fixed = sum(1 for c in sess if (c.get("arguments_after_correction") or {}).get("session_id") not in (None, "abc123"))
    check(f"Llama {cfg}   invented session id abc123 replaced by interceptor", f"{len(sess)} of {len(sess)}", f"{fixed} of {len(sess)}")

# ---------------------------------------------------------------------------------------------
section("11. Model-specific behaviour: download steps, impossible requests, tool use")
dl_steps = sorted(q for q, t in TC.items() if t.get("expected_tool") == "download_proof" and t.get("scenario") is not None)
for model, want in [("qwen", 10), ("mistral", 0)]:
    for cfg in "BCD":
        rs = [r for r in load(cfg_files(model)[cfg]) if r["query_id"] in dl_steps]
        check(f"{model} {cfg} Source 6 download steps that called a tool (of {len(rs)})", want, sum(1 for r in rs if tool_calls(r)), tol=0)
s4 = {q for q, t in TC.items() if str(t.get("source")) == "4"}
vals = [sum(1 for r in load(cfg_files(model)[c]) if r["query_id"] in s4 and tool_calls(r)) for model in ["qwen", "llama"] for c in ["A", "B", "C", "D", "A_STAR"]]
check("Qwen and Llama Source 4 turns with a tool call per config, minimum", 9, min(vals), tol=0)
check("Qwen and Llama Source 4 turns with a tool call per config, maximum", 11, max(vals), tol=0)
vals = [sum(1 for r in load(cfg_files("mistral")[c]) if r["query_id"] in s4 and tool_calls(r)) for c in ["A", "B", "C", "D", "A_STAR"]]
check("mistral Source 4 turns with a tool call (all configs)", 0, sum(vals), tol=0)
s7 = {q for q, t in TC.items() if str(t.get("source")) == "7"}
for model, want in [("qwen", [50, 50, 50, 50]), ("llama", [40, 40, 40, 40]), ("mistral", [37, 36, 38, 39])]:
    for cfg, w in zip("ABCD", want):
        check(f"{model} {cfg} Source 7 turns that called a tool (of 50)", w, sum(1 for r in load(cfg_files(model)[cfg]) if r["query_id"] in s7 and tool_calls(r)), tol=0)

# ---------------------------------------------------------------------------------------------
section("12. Fabrications (released detector), trust labels, verification successes")
hits = collections.Counter()
for p in files:
    for r in json.load(open(p, encoding="utf-8")):
        if v2.unbacked_action_claim(r):
            hits[os.path.basename(p)] += 1
check("zero-tool action claims across all 24 files", 7, sum(hits.values()), tol=0)
check("  in results_llama_A_STAR.json", 5, hits["results_llama_A_STAR.json"], tol=0)
check("  in results_mistral_A_STAR.json", 2, hits["results_mistral_A_STAR.json"], tol=0)
check("  in any non-A* file", 0, sum(v for k, v in hits.items() if "A_STAR" not in k), tol=0)
inv = sum(1 for p in files for r in json.load(open(p, encoding="utf-8")) for c in tool_calls(r) if "1777821051116" in resp(c))
check("tool outputs containing invented job id 1777821051116", 0, inv, tol=0)
inv_arg = sum(1 for p in files for r in json.load(open(p, encoding="utf-8")) for c in tool_calls(r) if "1777821051116" in json.dumps(args_of(c)))
check("  tool ARGUMENTS carrying it (the model passed its own invention to the next call)", 1, inv_arg, tol=0)
lab = collections.Counter()
for p in files:
    for r in json.load(open(p, encoding="utf-8")):
        m = re.search(r"\[(Backed by COSMeTIC prover|Not backed by COSMeTIC prover|Cryptographically verified|Cryptographically provable)\]", r.get("response_text") or "")
        lab[m.group(1) if m else "none"] += 1
check("turns labelled [Backed by COSMeTIC prover]", 4862, lab["Backed by COSMeTIC prover"], tol=0)
check("turns labelled [Not backed by COSMeTIC prover]", 1476, lab["Not backed by COSMeTIC prover"], tol=0)
check("turns with no label", 33, lab["none"], tol=0)
check("turns with a cryptographic tier", 0, lab["Cryptographically verified"] + lab["Cryptographically provable"], tol=0)
ok45 = sum(1 for p in files for r in json.load(open(p, encoding="utf-8")) if any(c["tool_name"] == "verify_proof" and '"ok": true' in resp(c) for c in tool_calls(r)))
check("turns with a successful verify_proof call", 45, ok45, tol=0)

# ---------------------------------------------------------------------------------------------
section("13. Verification extension (Section V-D): table, lifecycle by tool calls, tool choice")
ver = {"results_verify_qwen_A.json": ("A", 100.0, 60.0), "results_verify_qwen_B.json": ("B", 100.0, 80.0),
       "results_verify_qwen_C.json": ("C", 100.0, 94.3), "results_verify_qwen_D.json": ("D", 100.0, 100.0),
       "results_verify_mistral_C.json": ("C", 46.7, 83.3), "results_verify_mistral_D.json": ("D", 60.0, 93.3)}
steps = ["S6_SC6_01", "S6_SC6_02", "S6_SC6_03", "S6_SC6_04"]
life_expect = {"results_verify_qwen_A.json": 0, "results_verify_qwen_B.json": 0, "results_verify_qwen_C.json": 5, "results_verify_qwen_D.json": 5,
               "results_verify_mistral_C.json": 0, "results_verify_mistral_D.json": 0}
for name, (cfg, ts, tcp) in ver.items():
    check(f"{name[8:-5]} tool selection", ts, overall(cfg, name, "tool_selection", TCV))
    check(f"{name[8:-5]} task completion", tcp, overall(cfg, name, "task_completion", TCV))
    by = {(r["query_id"], r["run"]): r for r in load(name)}
    complete = 0
    for run in range(1, 6):
        rs = [by.get((q, run)) for q in steps]
        if any(r is None for r in rs):
            continue
        tools_ok = all(g.grade_tool_selection(r, TCV[q]) for r, q in zip(rs, steps))
        passed = any('"ok": true' in resp(c) for c in tool_calls(rs[3]))
        complete += (tools_ok and passed)
    check(f"{name[8:-5]} full lifecycle: all four tools called and verify passed (of 5)", life_expect[name], complete, tol=0)
for name, cfg, want in [("results_verify_mistral_C.json", "C", 7), ("results_verify_mistral_D.json", "D", 9)]:
    vq = [q for q, t in TCV.items() if t.get("expected_tool") == "verify_proof"]
    rs = [r for r in load(name) if r["query_id"] in vq]
    check(f"Mistral {cfg} verify turns", 20, len(rs), tol=0)
    check(f"Mistral {cfg} verify turns whose first call was verify_proof", want, sum(1 for r in rs if tool_calls(r) and tool_calls(r)[0]["tool_name"] == "verify_proof"), tol=0)
    dl = [r for r in load(name) if r["query_id"] == "S6_SC6_03"]
    check(f"Mistral {cfg} lifecycle download steps that called any tool", 0, sum(1 for r in dl if tool_calls(r)), tol=0)
vp = [c for r in load("results_verify_qwen_B.json") for c in tool_calls(r) if c["tool_name"] == "verify_proof"]
check("Qwen B verify_proof calls", 24, len(vp), tol=0)
check("  returned 'proof_type is required'", 10, sum(1 for c in vp if "proof_type is required" in resp(c)), tol=0)
check("  returned 'no completed job'", 10, sum(1 for c in vp if "no completed job" in resp(c)), tol=0)
check("  succeeded", 4, sum(1 for c in vp if '"ok": true' in resp(c)), tol=0)
check("  mis-routed (404)", 0, sum(1 for c in vp if "Unknown job id" in resp(c)), tol=0)

# ---------------------------------------------------------------------------------------------
section("14. Grading validity: original grader vs corrected grader")
orig = {}
for model in ["qwen", "mistral", "llama"]:
    for cfg, name in cfg_files(model).items():
        orig[(model, cfg)] = overall(cfg, name, "faithfulness")
for cfg, p in zip(["A", "B", "C", "D", "A_STAR"], [93.2, 83.8, 97.7, 99.0, 88.4]):
    check(f"Qwen {cfg} original-grader faithfulness", p, orig[("qwen", cfg)])
check("original-grader faithfulness, minimum over the three main models", 83.7, min(orig.values()))
check("original-grader faithfulness, maximum over the three main models", 100.0, max(orig.values()))
check("Qwen3-235B original-grader faithfulness (2 runs)", 79.2, overall("B", "results_qwen235b_B_partial.json", "faithfulness", TC, 2))
flag47 = sum(1 for run, r, t, h in v2._graded(load(Q["B"]), TC) if g.grade_response_faithfulness(r, t) is False)
check("Qwen B records flagged by the original grader", 47, flag47, tol=0)
corr = {}
for model in ["qwen", "mistral", "llama"]:
    for cfg, name in cfg_files(model).items():
        corr[(model, cfg)] = v2._rate(load(name), TC, lambda r, t, h: (None if v2.explain(r, t, False, h) is None else v2.explain(r, t, False, h) is True))[0]
check("Qwen A* corrected-grader faithfulness", 99.7, corr[("qwen", "A_STAR")])
check("corrected-grader faithfulness, all other 14 cells", 100.0, min(v for k, v in corr.items() if k != ("qwen", "A_STAR")))

# ---------------------------------------------------------------------------------------------
section("15. Section VI-C: response length, determinism, no-tool turns (Qwen C vs D)")
lens = {}
for cfg in "CD":
    tt = [r for r in load(Q[cfg]) if tool_calls(r)]
    lens[cfg] = sum(len(r["response_text"]) for r in tt) / len(tt)
check("Qwen C mean reply length on tool turns (chars)", 348.7, lens["C"])
check("Qwen D mean reply length on tool turns (chars)", 196.0, lens["D"])
check("C longer than D by (%)", 78, 100 * (lens["C"] / lens["D"] - 1), tol=0.5)
for cfg, want in [("D", 34), ("C", 1)]:
    by = collections.defaultdict(list)
    for r in load(Q[cfg]):
        by[r["query_id"]].append(r["response_text"])
    check(f"Qwen {cfg} queries byte-identical across all 5 runs (of 75)", want, sum(1 for q, v in by.items() if len(v) == 5 and len(set(v)) == 1), tol=0)
check("Qwen D turns with no tool call (of 375)", 67, sum(1 for r in load(Q["D"]) if not tool_calls(r)), tol=0)

# ---------------------------------------------------------------------------------------------
section("[H] Figures established by hand-reading (reports/), reported in the paper but not recomputed here")
for line in [
    "47 Qwen Config B grader-flagged replies: 32 correct error reports, 8 multi-call recoveries, 4 cached-status restatements, 3 omitted job id (reports/inspection_qwen32b_B_flagged_47.txt)",
    "4 turns in which Qwen restated the cached Redis status after a failed live check (the B3 failure mode)",
    "Mistral Source 4 capability promises: 0, 4, 6, 0, 8 in A, B, C, D, A* (18 records read)",
    "the 63 distinct opening sentences were classified by hand; the regex in section 8 reproduces that classification",
    "each of the 151 grader-flagged records was read and none was an ungrounded success claim (eval/test_grading_faithfulness_v2.py checks the corrected grader against those labels)",
]:
    print("  [H]", line)

print("\n" + "=" * 100)
print(f"SUMMARY: {n_ok} figures match the paper, {n_diff} differ")
print("=" * 100)
sys.exit(1 if n_diff else 0)
