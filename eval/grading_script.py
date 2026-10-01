#!/usr/bin/env python3
"""Grading script for CONFIG ablation study.

Reads results_{A,B,C,D}.json + test_suite.json and computes:
  - 5 metrics per config (tool selection, param correctness, task completion, response faithfulness, bypass rate)
  - Mean ± std across runs
  - Per-source breakdown
  - Config A auth/non-auth stratification
  - Source 6 cascade-aware scoring (unreachable vs failed)

Usage:
    python grading_script.py --results-dir /path/to/results --test-suite test_suite.json
    python grading_script.py  # defaults: current dir, test_suite.json
"""

import argparse
import json
import os
import sys
from collections import defaultdict
from pathlib import Path
import math

# Anchor defaults to this script's location so it works regardless of cwd.
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(_SCRIPT_DIR)
DEFAULT_RESULTS_DIR = os.path.join(_REPO_ROOT, "results")
DEFAULT_TEST_SUITE = os.path.join(_SCRIPT_DIR, "test_suite.json")


# ─── Load data ───

def load_test_suite(path: str) -> dict:
    """Load test suite and return dict keyed by query_id."""
    with open(path) as f:
        raw = json.load(f)
    cases = {}
    for entry in raw:
        if "_comment" in entry:
            continue
        cases[entry["id"]] = entry
    return cases


def load_results(path: Path) -> list:
    if not path.exists():
        return []
    with open(path) as f:
        return json.load(f)


# ─── Metric functions ───

def grade_tool_selection(result: dict, test_case: dict) -> bool | None:
    """Metric 1: Did the LLM call the correct tool?
    Returns True (correct), False (wrong), or None (not gradable).
    """
    expected = test_case.get("expected_tool")
    acceptable = test_case.get("acceptable_tools")

    # Qualitative queries (e.g. S6_SC5_02 "Run it again")
    if test_case.get("scoring") == "qualitative":
        return None

    # Error in execution — can't grade
    if result.get("error"):
        return None

    tc = result.get("tool_calls", [])
    actual = tc[0].get("tool_name") if tc else None

    # Source 4, 5: expected_tool is NONE → no tool should be called
    if expected == "NONE":
        return actual is None or len(tc) == 0

    # Source 3b: any acceptable tool counts
    if acceptable:
        return actual in acceptable

    # Normal case
    if expected:
        return actual == expected

    return None


def grade_param_correctness_pre(result: dict, test_case: dict) -> bool | None:
    """Metric 2a: Did the LLM send correct parameters BEFORE correction?"""
    expected_params = test_case.get("expected_params")
    if not expected_params:
        return None

    if result.get("error"):
        return None

    tc = result.get("tool_calls", [])
    if not tc:
        return None

    actual_args = tc[0].get("arguments", {})

    # Check each expected param exists and matches
    for key, value in expected_params.items():
        if key == "job_id" and test_case.get("runtime_substitution"):
            # Job ID was substituted at runtime — just check it exists
            if key not in actual_args:
                return False
            continue
        actual_val = actual_args.get(key)
        if actual_val is None:
            return False
        # Case-insensitive comparison for proof_type
        if isinstance(value, str) and isinstance(actual_val, str):
            if value.lower() != actual_val.lower():
                return False
        elif str(value) != str(actual_val):
            return False

    return True


def grade_param_correctness_post(result: dict, test_case: dict) -> bool | None:
    """Metric 2b: Were parameters correct AFTER tool correction?"""
    expected_params = test_case.get("expected_params")
    if not expected_params:
        return None

    if result.get("error"):
        return None

    tc = result.get("tool_calls", [])
    if not tc:
        return None

    actual_args = tc[0].get("arguments_after_correction", {})

    for key, value in expected_params.items():
        if key == "job_id" and test_case.get("runtime_substitution"):
            if key not in actual_args:
                return False
            continue
        actual_val = actual_args.get(key)
        if actual_val is None:
            # session_id is injected, not expected in test case
            continue
        if isinstance(value, str) and isinstance(actual_val, str):
            if value.lower() != actual_val.lower():
                return False
        elif str(value) != str(actual_val):
            return False

    return True


def grade_task_completion(result: dict, test_case: dict) -> bool | None:
    """Metric 3: Did the user get the correct end-to-end outcome?"""
    if test_case.get("scoring") == "qualitative":
        return None

    if result.get("error"):
        return False

    response = (result.get("response_text") or "").lower()

    # Check should_contain
    # For queries with expected_outcome (Source 2 error cases), should_contain
    # lists SYNONYMS — any one matching is sufficient.
    # For other queries, ALL must be present.
    should_contain = test_case.get("should_contain", [])
    if should_contain:
        if test_case.get("expected_outcome"):
            # Source 2: ANY match is sufficient (synonyms/alternatives)
            found_any = any(phrase.lower() in response for phrase in should_contain)
            if not found_any:
                return False
        else:
            # All other sources: ALL must be present
            for phrase in should_contain:
                if phrase.lower() not in response:
                    return False

    # Check should_not_contain (NONE should be present)
    # Smart matching: "error" in should_not_contain should NOT trigger on
    # "no errors", "no error", "without error", "0 errors" etc.
    negation_prefixes = ["no ", "no\n", "not ", "without ", "zero ", "0 ", "n't "]
    should_not_contain = test_case.get("should_not_contain", [])
    for phrase in should_not_contain:
        pl = phrase.lower()
        if pl not in response:
            continue
        # Found the phrase — but is it negated?
        idx = response.find(pl)
        negated = False
        while idx != -1:
            # Check if preceded by a negation word
            prefix = response[max(0, idx - 10):idx]
            if any(neg in prefix for neg in negation_prefixes):
                # This occurrence is negated — check next occurrence
                idx = response.find(pl, idx + 1)
                negated = True
                continue
            else:
                # Non-negated occurrence found — fail
                return False
        # If we got here, all occurrences were negated — don't fail

    # Check should_contain_any_redirect (at least ONE must be present)
    redirect_phrases = test_case.get("should_contain_any_redirect", [])
    if redirect_phrases:
        found_any = any(p.lower() in response for p in redirect_phrases)
        if not found_any:
            return False

    # For Source 7 (bypass): tool must have been called
    if test_case.get("must_call_tool"):
        tc = result.get("tool_calls", [])
        if not tc:
            return False

    # For expected_tool == NONE: no tool should have been called
    if test_case.get("expected_tool") == "NONE":
        tc = result.get("tool_calls", [])
        if tc:
            return False

    return True


def grade_response_faithfulness(result: dict, test_case: dict) -> bool | None:
    """Metric 4: Does the response match what the tool returned?
    Checks that key values from tool response appear in user-facing response.
    """
    if test_case.get("scoring") == "qualitative":
        return None
    if result.get("error"):
        return None

    tc = result.get("tool_calls", [])
    if not tc:
        return None  # No tool called — faithfulness N/A

    tool_response_raw = tc[0].get("response", "")
    user_response = (result.get("response_text") or "").lower()

    # Try to extract key values from tool response
    try:
        tool_data = json.loads(tool_response_raw)
        if isinstance(tool_data, dict):
            # Check if job_id from tool appears in user response (primary check)
            job_id = tool_data.get("job_id")
            if job_id and str(job_id) not in user_response:
                return False

            # Status check: accept paraphrases of "done"
            status = tool_data.get("status")
            if status:
                status_lower = status.lower()
                # "done" can be paraphrased as completed/finished/succeeded
                done_synonyms = ["done", "completed", "complete", "finished", "succeeded", "successful"]
                if status_lower == "done":
                    if not any(syn in user_response for syn in done_synonyms):
                        return False
                elif status_lower not in user_response:
                    return False

            return True
    except (json.JSONDecodeError, TypeError):
        pass

    # For non-JSON responses (error messages, HTML errors, etc.)
    tool_lower = tool_response_raw.lower().strip()

    # If tool said "sign in", user response should mention signing in
    if "sign in" in tool_lower:
        return "sign in" in user_response or "log in" in user_response

    # If tool returned an error (404, not found, unknown job, etc.)
    # check that LLM doesn't claim success
    error_indicators = ["not found", "unknown job", "404", "error", "invalid"]
    success_indicators = ["done", "completed", "successfully", "submitted"]

    tool_has_error = any(ind in tool_lower for ind in error_indicators)
    response_claims_success = any(ind in user_response for ind in success_indicators)

    if tool_has_error and response_claims_success:
        return False  # LLM fabricated success from failure — genuinely unfaithful

    if tool_has_error:
        # Tool returned error, LLM acknowledged something wrong — faithful enough
        return True

    # For other non-JSON responses, be lenient — LLM paraphrasing is OK
    return True


def grade_tool_bypass(result: dict, test_case: dict) -> bool | None:
    """Metric 5: Did the LLM call ANY tool? (Source 7 only)

    Returns True if a tool WAS called (good), False if the model answered from its own
    knowledge (bad). Reported as "Tool-Use Rate (S7)": HIGHER IS BETTER. The internal key
    is still "bypass" for backward compatibility with existing result files, but the
    printed label says tool use, because the value is the rate of NOT bypassing.
    """
    if not test_case.get("must_call_tool"):
        return None

    if result.get("error"):
        return None

    tc = result.get("tool_calls", [])
    backed = result.get("backed_by_cosmetic", False)

    return len(tc) > 0 or backed is True


# ─── Source 6 cascade handling ───

def mark_unreachable_steps(results_for_scenario: list, test_cases: dict) -> dict:
    """For a Source 6 scenario, if step 1 failed, mark later steps as unreachable.
    Returns dict: query_id -> "unreachable" | "normal"
    """
    marks = {}
    steps = sorted(results_for_scenario, key=lambda r: r.get("step", 0))

    cascade_failed = False
    for r in steps:
        qid = r["query_id"]
        if cascade_failed:
            marks[qid] = "unreachable"
            continue

        # Check if this step failed in a way that would cascade
        tc = r.get("tool_calls", [])
        tool_response = tc[0].get("response", "") if tc else ""
        has_error = r.get("error") is not None

        # Step failed if: error, or tool returned "sign in", or no tool called for expected tool
        test_case = test_cases.get(qid, {})
        expected = test_case.get("expected_tool")

        if has_error:
            cascade_failed = True
            marks[qid] = "normal"  # This step is graded normally (it failed)
        elif expected and expected != "NONE":
            actual = tc[0].get("tool_name") if tc else None
            if actual != expected:
                cascade_failed = True
                marks[qid] = "normal"
            elif "sign in" in tool_response.lower():
                cascade_failed = True
                marks[qid] = "normal"
            else:
                # Check for real errors in tool response (not "error": null)
                has_real_error = False
                try:
                    parsed = json.loads(tool_response)
                    if isinstance(parsed, dict):
                        err_val = parsed.get("error")
                        has_real_error = err_val is not None and err_val != "" and err_val != "null"
                    else:
                        has_real_error = False
                except (json.JSONDecodeError, TypeError):
                    # Non-JSON response — check for error indicators
                    has_real_error = any(ind in tool_response.lower() for ind in
                        ["not found", "unknown job", "404", "timed out", "failed"])

                if has_real_error:
                    cascade_failed = True
                marks[qid] = "normal"
        else:
            marks[qid] = "normal"

    return marks


# ─── Aggregation ───

def compute_rate(values: list) -> float | None:
    """Compute rate from list of True/False/None, ignoring None."""
    valid = [v for v in values if v is not None]
    if not valid:
        return None
    return sum(1 for v in valid if v) / len(valid) * 100


def compute_mean_std(per_run_rates: list) -> tuple:
    """Compute mean and std from list of rates (floats or None)."""
    valid = [r for r in per_run_rates if r is not None]
    if not valid:
        return None, None
    mean = sum(valid) / len(valid)
    if len(valid) == 1:
        return mean, 0.0
    variance = sum((x - mean) ** 2 for x in valid) / (len(valid) - 1)
    std = math.sqrt(variance)
    return mean, std


def fmt(mean, std):
    """Format mean±std for display."""
    if mean is None:
        return "  N/A"
    if std is None or std == 0:
        return f"{mean:5.1f}"
    return f"{mean:5.1f}±{std:4.1f}"


# ─── Main grading logic ───

def grade_config(config: str, results: list, test_cases: dict, num_runs: int) -> dict:
    """Grade all results for one config. Returns dict of metrics."""

    # Group results by run
    by_run = defaultdict(list)
    for r in results:
        by_run[r["run"]].append(r)

    # For each run, compute per-query grades
    metric_names = [
        "tool_selection", "param_pre", "param_post",
        "task_completion", "faithfulness", "bypass"
    ]

    per_run_rates = {m: [] for m in metric_names}
    per_run_rates_auth = {m: [] for m in metric_names}
    per_run_rates_nonauth = {m: [] for m in metric_names}

    # Per-source breakdown
    sources = [1, 2, "3b", 4, 5, 6, 7]
    per_source_rates = {s: {m: [] for m in metric_names} for s in sources}

    for run_num in sorted(by_run.keys()):
        run_results = by_run[run_num]

        # Handle Source 6 cascade marking
        # Group S6 results by scenario
        s6_by_scenario = defaultdict(list)
        for r in run_results:
            if r.get("scenario") is not None:
                s6_by_scenario[r["scenario"]].append(r)

        unreachable_qids = set()
        for scenario_num, scenario_results in s6_by_scenario.items():
            marks = mark_unreachable_steps(scenario_results, test_cases)
            for qid, mark in marks.items():
                if mark == "unreachable":
                    unreachable_qids.add(qid)

        # Grade each query in this run
        grades = {m: [] for m in metric_names}
        grades_auth = {m: [] for m in metric_names}
        grades_nonauth = {m: [] for m in metric_names}
        source_grades = {s: {m: [] for m in metric_names} for s in sources}

        for r in run_results:
            qid = r["query_id"]
            tc = test_cases.get(qid)
            if not tc:
                continue

            # Skip unreachable Source 6 steps
            if qid in unreachable_qids:
                continue

            source = tc.get("source")

            # Compute all grades
            g_tool = grade_tool_selection(r, tc)
            g_ppre = grade_param_correctness_pre(r, tc)
            g_ppost = grade_param_correctness_post(r, tc)
            g_task = grade_task_completion(r, tc)
            g_faith = grade_response_faithfulness(r, tc)
            g_bypass = grade_tool_bypass(r, tc)

            all_grades = {
                "tool_selection": g_tool,
                "param_pre": g_ppre,
                "param_post": g_ppost,
                "task_completion": g_task,
                "faithfulness": g_faith,
                "bypass": g_bypass,
            }

            for m, g in all_grades.items():
                grades[m].append(g)

                if source in sources:
                    source_grades[source][m].append(g)

                # Auth stratification (for Config A analysis)
                # Exclude Source 7 (bypass temptation) — it tests LLM tool-discipline,
                # not auth behavior, and passes 100% across all configs
                us = tc.get("user_state", {})
                is_auth = (
                    source != 7 and
                    us.get("logged_in") is not False and
                    tc.get("expected_tool") in [
                        "prove_my_data", "check_my_hash_existence",
                        "check_status", "download_proof", "get_session_context"
                    ]
                )
                if is_auth:
                    grades_auth[m].append(g)
                else:
                    grades_nonauth[m].append(g)

        # Compute rates for this run
        for m in metric_names:
            per_run_rates[m].append(compute_rate(grades[m]))
            per_run_rates_auth[m].append(compute_rate(grades_auth[m]))
            per_run_rates_nonauth[m].append(compute_rate(grades_nonauth[m]))

        for s in sources:
            for m in metric_names:
                per_source_rates[s][m].append(compute_rate(source_grades[s][m]))

    # Compute mean ± std across runs
    overall = {}
    for m in metric_names:
        mean, std = compute_mean_std(per_run_rates[m])
        overall[m] = {"mean": mean, "std": std}

    auth_split = {}
    for m in metric_names:
        mean_a, std_a = compute_mean_std(per_run_rates_auth[m])
        mean_n, std_n = compute_mean_std(per_run_rates_nonauth[m])
        auth_split[m] = {
            "auth": {"mean": mean_a, "std": std_a},
            "nonauth": {"mean": mean_n, "std": std_n},
        }

    per_source = {}
    for s in sources:
        per_source[s] = {}
        for m in metric_names:
            mean, std = compute_mean_std(per_source_rates[s][m])
            per_source[s][m] = {"mean": mean, "std": std}

    return {
        "overall": overall,
        "auth_split": auth_split,
        "per_source": per_source,
        "num_results": len(results),
        "num_runs": len(by_run),
    }


# ─── Display ───

def print_table(all_grades: dict):
    """Print the main comparison table (Table 3 in the paper)."""
    configs = ["A", "B", "C", "D"]
    metrics = [
        ("Tool Selection", "tool_selection"),
        ("Param Corr (pre)", "param_pre"),
        ("Param Corr (post)", "param_post"),
        ("Task Completion", "task_completion"),
        ("Faithfulness", "faithfulness"),
        ("Tool-Use Rate (S7)", "bypass"),
    ]

    print("\n" + "=" * 70)
    print("TABLE 3: Results across configurations (mean ± std %)")
    print("=" * 70)

    header = f"{'Metric':<22}"
    for cfg in configs:
        header += f"{'Config '+cfg:>12}"
    print(header)
    print("-" * 70)

    for label, key in metrics:
        row = f"{label:<22}"
        for cfg in configs:
            if cfg in all_grades:
                m = all_grades[cfg]["overall"].get(key, {})
                row += f"{fmt(m.get('mean'), m.get('std')):>12}"
            else:
                row += f"{'N/A':>12}"
        print(row)

    print()


def print_auth_stratification(all_grades: dict):
    """Print Config A auth vs non-auth breakdown."""
    if "A" not in all_grades:
        return

    print("\n" + "=" * 70)
    print("CONFIG A: Auth vs Non-Auth Stratification")
    print("=" * 70)

    metrics = [
        ("Tool Selection", "tool_selection"),
        ("Task Completion", "task_completion"),
    ]

    header = f"{'Metric':<22}{'Auth-dep':>12}{'Non-auth':>12}"
    print(header)
    print("-" * 46)

    a = all_grades["A"]["auth_split"]
    for label, key in metrics:
        auth = a[key]["auth"]
        nonauth = a[key]["nonauth"]
        row = f"{label:<22}"
        row += f"{fmt(auth['mean'], auth['std']):>12}"
        row += f"{fmt(nonauth['mean'], nonauth['std']):>12}"
        print(row)

    print()


def print_per_source(all_grades: dict):
    """Print per-source task completion breakdown."""
    configs = ["A", "B", "C", "D"]
    sources = [1, 2, "3b", 4, 5, 6, 7]
    source_labels = {
        1: "S1 Normal",
        2: "S2 Errors",
        "3b": "S3b Ambig",
        4: "S4 Impossible",
        5: "S5 Out-of-scope",
        6: "S6 Multi-step",
        7: "S7 Tool-Use",
    }

    print("\n" + "=" * 70)
    print("PER-SOURCE: Task Completion (mean %)")
    print("=" * 70)

    header = f"{'Source':<18}"
    for cfg in configs:
        header += f"{'Config '+cfg:>12}"
    print(header)
    print("-" * 66)

    for s in sources:
        label = source_labels.get(s, str(s))
        row = f"{label:<18}"
        for cfg in configs:
            if cfg in all_grades:
                ps = all_grades[cfg].get("per_source", {}).get(s, {})
                tc = ps.get("task_completion", {})
                row += f"{fmt(tc.get('mean'), tc.get('std')):>12}"
            else:
                row += f"{'N/A':>12}"
        print(row)

    print()


def print_latex_table(all_grades: dict):
    """Print LaTeX-ready table for the paper."""
    configs = ["A", "B", "C", "D"]
    metrics = [
        ("Tool Selection Acc.", "tool_selection"),
        ("Param. Correct. (pre)", "param_pre"),
        ("Param. Correct. (post)", "param_post"),
        ("Task Completion", "task_completion"),
        ("Response Faithful.", "faithfulness"),
        ("Tool-Use Rate (S7)", "bypass"),
    ]

    print("\n" + "=" * 70)
    print("LATEX TABLE (copy into paper.tex Table 3)")
    print("=" * 70)

    for label, key in metrics:
        row = f"{label}"
        for cfg in configs:
            if cfg in all_grades:
                m = all_grades[cfg]["overall"].get(key, {})
                mean = m.get("mean")
                std = m.get("std")
                if mean is None:
                    row += " & ---"
                elif std is None or std == 0:
                    row += f" & {mean:.1f}"
                else:
                    row += f" & ${mean:.1f} \\pm {std:.1f}$"
            else:
                row += " & ---"
        row += " \\\\"
        print(row)

    print()


# ─── Main ───

def main():
    parser = argparse.ArgumentParser(description="Grade ablation study results")
    parser.add_argument("--results-dir", default=DEFAULT_RESULTS_DIR, help="Directory containing results_{X}.json files")
    parser.add_argument("--test-suite", default=DEFAULT_TEST_SUITE, help="Path to test_suite.json")
    parser.add_argument("--configs", default="A,B,C,D", help="Comma-separated configs to grade")
    args = parser.parse_args()

    results_dir = Path(args.results_dir)
    configs = [c.strip() for c in args.configs.split(",")]

    # Load test suite
    if not os.path.exists(args.test_suite):
        print(f"ERROR: Test suite not found: {args.test_suite}")
        sys.exit(1)

    test_cases = load_test_suite(args.test_suite)
    print(f"Loaded {len(test_cases)} test cases from {args.test_suite}")

    # Grade each config
    all_grades = {}
    for cfg in configs:
        path = results_dir / f"results_{cfg}.json"
        results = load_results(path)
        if not results:
            print(f"Config {cfg}: no results file found at {path}")
            continue

        grades = grade_config(cfg, results, test_cases, num_runs=5)
        all_grades[cfg] = grades
        print(f"Config {cfg}: {grades['num_results']} results across {grades['num_runs']} runs")

    if not all_grades:
        print("No results to grade!")
        sys.exit(1)

    # Print tables
    print_table(all_grades)
    print_auth_stratification(all_grades)
    print_per_source(all_grades)
    print_latex_table(all_grades)


if __name__ == "__main__":
    main()
