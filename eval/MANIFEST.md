# Test suite manifest

Two JSON files define every query the paper evaluates. Neither has been modified since the
runs; the counts below are computed from the files.

## eval/test_suite.json — main ablation suite

The file holds 100 entries: 80 query objects and 20 `_comment` entries that document the
groups. Of the 80 query objects, 75 were executed and 5 were not.

| Source (paper Table II) | Suite label | Mode | Executed | Purpose |
|---|---|---|---|---|
| 1. Normal operations | `1` | isolated | 23 | tool selection and parameters |
| 2. Graceful failures | `2` | isolated | 10 | error handling |
| 3. Context-free ambiguity | `3b` | isolated | 5 | ambiguity handling |
| 4. Impossible requests | `4` | isolated | 5 | capability suppression |
| 5. Out-of-scope and redirect | `5` | isolated | 5 | CoSMeTIC awareness |
| 6. Multi-step workflows | `6` | sequential, 5 scenarios (4+3+3+3+4 steps) | 17 | Redis features B4 to B6 |
| 7. Tool-bypass temptation | `7` | isolated | 10 | tool-use discipline |
| **Executed total** | | | **75** | |
| Context-dependent ambiguity | `3a` | sequential, no scenario | **0** | authored, never executed |

**The five unexecuted entries** are `S3a_01` to `S3a_05` ("Run it again", "Do the same thing",
"Same as last time", "Again please", "One more time"). They carry `mode: sequential` but no
`scenario` field, so the runner's scenario grouping never schedules them; they appear in no
results file. They are kept in the file exactly as authored. One context-dependent query does
run: `S6_SC5_02` ("Run it again") is step 2 of Scenario 5 and is scored qualitatively.

The paper calls suite label `3b` "Source 3" for readability.

## eval/test_suite_verify.json — verification extension

7 executed queries: `S1_24`, `S1_25` (isolated, `verify_proof`), `S2_11` (isolated, error
path) and Scenario 6 (`S6_SC6_01` to `S6_SC6_04`: prove, status, download, verify), which
submits its own job and therefore does not depend on the reference-job fixture.

## Fixture dependence

Isolated queries that need a completed job receive one injected into Redis by the runner
before each sweep (the "reference job"). See `results/RESULTS_MANIFEST.csv` for which sweeps
had a reference job that reached `done` and which did not.

## Grading

- `grading_script.py` is the original grader, unchanged in its scoring logic since the runs.
  The only post-run edit is the printed label of the Source 7 metric, from "Tool Bypass Rate"
  to "Tool-Use Rate", because the value it prints is the rate of tool use (higher is better).
- `grading_faithfulness_v2.py` is the corrected faithfulness grader described in the paper.
- `test_grading_faithfulness_v2.py` holds the four synthetic positive controls and the
  hand-label agreement check (150 of 151).
