# Test Automation Guide: run_tests.py

This document explains how the test suite for `coding_agent_with_judge.py` works — why it was built the way it was, how each tier operates mechanically, and how to read, extend, or debug it.

---

## Why Automated Tests Are Needed

The judge layer has two categories of behavior:

1. **Deterministic mechanical logic** — risk classification, proposal flag construction, sensitivity detection, composition rules, memory write-back, recall filtering, review queue actions. These are pure functions with no randomness. A test can call the function directly, assert the output, and be certain.

2. **LLM-dependent end-to-end behavior** — whether the actor makes a tool call at all, whether the specialist judges return PASS/FAIL/UNCERTAIN for a given prompt and context, whether the piped stdin lands on the right `input()` call. These involve real network calls and non-deterministic model output.

Running all 43 tests from TESTING.md manually takes 20–30 minutes and produces results that can't be diffed or archived. An automated runner solves both problems — and found a real bug in the process (see the Bug Found section below).

---

## Architecture: Two Tiers

```
run_tests.py
│
├── run_unit_tests()       24 tests, ~1s, no API calls
│   ├── Import target functions directly
│   ├── Patch module-level variables (LOG_FILE, REVIEW_FILE, WORKSPACE)
│   ├── Use tempfile.TemporaryDirectory for all file I/O
│   └── Assert specific values; build evidence list
│
└── run_integration_tests()  10 tests, ~60s, ~15 LLM calls
    ├── subprocess.run() with piped stdin
    ├── Capture stdout+stderr as UTF-8
    ├── Pattern-match output with re.search()
    └── Extract matching lines as evidence
```

---

## Unit Test Mechanics

### Direct Import

The unit tests import the application modules directly into the test process:

```python
import coding_agent_with_judge as agent
import judge_memory
from judge_specialists import compose_specialist_verdicts
```

This means no subprocess, no network, no API key required. The tests run in ~1 second.

### Patching Module-Level State

`judge_memory.py` initializes `LOG_FILE` and `REVIEW_FILE` at import time using `Path.cwd().resolve()`. If the tests wrote to those paths, they would pollute the real log files. Instead, every unit test that touches the filesystem uses `unittest.mock.patch` to redirect to a `tempfile.TemporaryDirectory`:

```python
with tempfile.TemporaryDirectory() as tmp:
    tmp_path = Path(tmp)
    log_file = tmp_path / "test_log.jsonl"
    with patch.object(judge_memory, "LOG_FILE", log_file):
        judge_memory.write_decision_log(...)
        # Assertions against log_file, not the real log
```

This pattern isolates every test. Parallel test runs would not interfere with each other.

### Evidence Capture

Each unit test builds an `evidence` list of strings showing what was asserted and what value was found. For example, the `use_policy` test captures:

```
ALLOW + human=None     → use_policy='can_use_as_evidence'   ✓
BLOCK + human=None     → use_policy='can_use_as_evidence'   ✓
REVISE + human=None    → use_policy='can_use_as_evidence'   ✓
ESCALATE + human=True  → use_policy='requires_confirmation' ✓
ESCALATE + human=False → use_policy='can_use_as_evidence'   ✓
```

These strings are written verbatim to `test_results.md` so a reader can verify the pass without re-running the test.

---

## Integration Test Mechanics

### Subprocess with Piped Stdin

The agent is an interactive REPL that calls Python's `input()` for:
1. Each user prompt (outer loop)
2. `Approve this action? [y/N]:` (escalation prompt)
3. `Note for review queue (Enter to skip):` (note prompt)

To automate this, `_run_agent()` pipes all expected inputs through `subprocess.stdin`:

```python
def _run_agent(inputs: List[str], timeout: int = 120) -> Tuple[int, str]:
    stdin_text = "\n".join(inputs) + "\n"
    r = subprocess.run(
        [sys.executable, "-X", "utf8", "coding_agent_with_judge.py"],
        input=stdin_text,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        cwd=str(PROJECT_DIR),
        env={**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"},
    )
    return r.returncode, (r.stdout or "") + (r.stderr or "")
```

When Python's `input()` is called and stdin is a pipe, it reads the next line from the pipe. The agent exits cleanly on `EOFError` (when the pipe is exhausted), which triggers the session metrics summary.

### Input Sequencing

The critical design challenge is knowing how many inputs to pipe and in what order. The agent consumes `input()` calls in this sequence per judged turn:

```
input("You: ")              ← user prompt
[actor LLM call]
[specialist LLM calls x2]
input("Approve? [y/N]: ")   ← only for ESCALATE
input("Note: ")             ← only if approved
input("You: ")              ← next user turn OR EOF → exit
```

For tests that expect ESCALATE, the inputs list is:
```python
["<user prompt>", "N"]        # deny
["<user prompt>", "y", "note text"]  # approve with note
```

For tests that expect ALLOW or BLOCK (no escalation prompt), only the user prompt is needed — the next `input("You: ")` call hits EOF and exits.

A key failure mode: if the actor responds conversationally (asks a question instead of making a tool call), the inner loop exits without consuming the escalation inputs, and the next `input("You: ")` reads the intended "N" as a user prompt. This is why prompts are designed to specify the tool call directly:

```python
# Fragile — actor may ask questions first
"make demo.py production-ready"

# Robust — actor calls edit_file immediately
"call edit_file on demo.py with old_str='' and new_str='x=1'"
```

### Pattern Matching

Output is checked with `re.search()` against patterns like `r"\[JUDGE: ALLOW\]"` or `r"auth-judge"`. The tests do not parse the JSON or inspect internal state — they verify only what appears in the terminal output, which is what a human running the tests would observe.

For the ESCALATE pattern specifically, both forms that can appear in output need to match:
- `[JUDGE: ESCALATE]` — from `print_judge_decision()` on a direct ESCALATE verdict
- `[JUDGE: ESCALATE — human approval required]` — from `prompt_user_for_escalation()`

The pattern `r"\[JUDGE: ESCALATE"` (without the closing bracket) matches both.

### Evidence Extraction

After each integration test, `_extract_judge_evidence(out)` scans the captured output for lines containing judge-relevant keywords and strips ANSI escape codes:

```python
def _extract_judge_evidence(out: str, max_lines: int = 20) -> List[str]:
    keywords = [
        '[tool:', 'auth-judge', 'risk-judge', '[JUDGE:',
        'Approve this action', 'checks:', 'Session Metrics',
        'Judge calls:', 'Decisions:', 'reversible_write', 'high_risk',
        ...
    ]
    lines = []
    for raw in _strip_ansi(out).splitlines():
        s = raw.strip()
        if s and any(k in s for k in keywords):
            lines.append(s)
    return lines[:max_lines]
```

These extracted lines become the evidence block in `test_results.md`, showing the exact terminal output that constituted a pass.

### Workspace Isolation for BLOCK Tests

The BLOCK test for workspace enforcement uses a path one level above the workspace rather than a system path like `C:\Windows\system32\...`:

```python
outside_path = str(PROJECT_DIR.parent / "outside_workspace_test_file.txt")
```

System paths work correctly with the workspace boundary check, but gpt-4o refuses to make the tool call at all when it recognizes a system path ("I can't modify system files"). Using a non-system path outside the workspace gets the actor to make the tool call while still triggering the `outside_workspace=True` risk flag.

### Inspector Test Seeding

The `--inspect` CLI test (`I.9`) does not run an agent to seed the log. Instead it writes a fixture log entry directly:

```python
judge_memory.write_decision_log(seed_proposal, seed_decision, policy_version="v1.0")
```

This guarantees a known entry exists before `--inspect` runs, regardless of what earlier tests did. It also makes I.9 fast (no LLM call) and its evidence more predictable.

---

## The Bug Found by the Tests

During the first unit test run, `[1.14b/14] recall: same-path entries prioritized` failed with:

```
AssertionError: same-path entry e3 missing from recalled: ['e1', 'e2', 'e4']
```

The bug was in `recall_prior_decisions()` in `judge_memory.py`:

```python
# Before (buggy)
return (same + other)[-max_items:]
```

With `same = [e3]` and `other = [e0, e1, e2, e4]`, the combined list `[e3, e0, e1, e2, e4]` has 5 elements. `[-3:]` returns the last 3: `[e1, e2, e4]` — dropping `e3` entirely. The same-path entry was being silently excluded when there were enough other-path entries.

**Fix applied:**

```python
# After (correct)
n_same = min(len(same), max_items)
n_other = max_items - n_same
selected = same[-n_same:] + (other[-n_other:] if n_other > 0 else [])
return sorted(selected, key=lambda e: e.get("timestamp", ""))
```

This reserves slots for same-path entries first, fills the remainder with other-path entries, then sorts chronologically so the judge sees them in time order. Without the test, this bug would have silently caused the judge to miss prior decisions about the file it was evaluating.

---

## How to Run

```bash
# Fast: unit tests only (no API key needed, ~1s)
python run_tests.py --unit-only

# Full: unit + integration (~60s, requires OPENAI_API_KEY)
python run_tests.py
```

Output:
- Live pass/fail per test in the terminal
- `test_results.md` — detailed report with evidence per test

---

## How to Add a New Test

### Unit test

1. Add a new `try/except` block inside `run_unit_tests()`.
2. Build an `evidence: List[str] = []` list as you make assertions.
3. Call `_record("X.Y/feature", "description", "Nate concept", passed, evidence=evidence)`.

```python
t = "X.Y/feature_number"
try:
    evidence = []
    result = some_function(input_value)
    assert result == expected_value
    evidence.append(f"some_function({input_value!r}) → {result!r} ✓")
    _record(t, "short description", "Nate concept", True, evidence=evidence)
except Exception as e:
    _record(t, "short description", "Nate concept", False, str(e))
```

### Integration test

1. Add a call to `_run_agent(inputs)` inside `run_integration_tests()`.
2. Check output with `_has(pattern, out)` and `_lacks(pattern, out)`.
3. Extract evidence with `_extract_judge_evidence(out)`.
4. Call `_record(...)` with `evidence=judge_lines`.

```python
t = "I.N/feature"
_reset_demo()
print(f"  {DIM}  [{t}] Running agent…{RESET}", flush=True)
rc, out = _run_agent(["your direct prompt here", "N"])  # add y/N if ESCALATE expected
judge_lines = _extract_judge_evidence(out)
passed = (
    _has(r"\[JUDGE: EXPECTED_OUTCOME", out)
    and _lacks(r"pattern_that_should_not_appear", out)
)
_record(t, "description", "Nate concept", passed,
        details=f"Output: {out[:400]}" if not passed else "",
        evidence=judge_lines)
```

### Prompt design rules for integration tests

| Goal | Pattern | Reason |
|------|---------|--------|
| Force immediate tool call | `"call edit_file on X with old_str='...' and new_str='...'"` | Skips actor preamble questions |
| Trigger ESCALATE (blind overwrite) | `"call edit_file on demo.py with old_str='' and new_str='x=1'"` | `old_str=''` + existing file = `overwrites_existing=True` |
| Trigger BLOCK (workspace) | Use `str(PROJECT_DIR.parent / "filename.txt")` not system paths | gpt-4o refuses system paths at actor level |
| Test run_command | `"run the command: echo hello"` | Actor uses tool directly; judge always-escalates |
| Test sensitivity | `'use edit_file to add the line: API_KEY = "sk-abc..."'` | Explicit key pattern in new_str |

---

## Test Coverage Map

| Feature area | Unit tests | Integration tests |
|-------------|-----------|-------------------|
| Action classification | 1.1/16 | I.1, I.2 (by absence) |
| Proposal construction | 1.3/2 | All edit_file tests |
| Sensitivity detection | 5.1/30 | I.10 |
| Composition rules (all 6) | 3.3/21 (×6) | I.3–I.7 (by outcome) |
| Confidence scoring | 3.5/22 | I.3 (banner) |
| Checks object | 3.6/23 | I.3, I.4 (banner) |
| Decision log fields | 4.1/12 | I.6 (log inspection) |
| Use policies | 4.2/24 | I.6 (log inspection) |
| Recall filtering | 1.14/14 | I.3, I.4 (implicit) |
| Recall prioritization | 1.14b/14 | — |
| mark_entry_superseded | 4.11/25/29 | — |
| mark_entry_disputed | 4.11b/29 | — |
| Review queue enqueue | 4.4/26 | I.6 (queue file) |
| Review queue confirm | 4.6/27 | — |
| Review queue reject | 4.7/27 | — |
| Review queue downgrade | 4.8/27 | — |
| Inspector output | 4.9/28 | I.9 |
| Inspector filter | 4.10/28 | — |
| Policy versioning | 5.3/31 | I.9 (banner) |
| ALLOW end-to-end | — | I.3 |
| BLOCK (workspace) | — | I.4 |
| ESCALATE + deny | — | I.5 |
| ESCALATE + approve + note | — | I.6 |
| run_command always-escalates | — | I.7 |
| Session metrics | — | I.8 |
| Sensitivity BLOCK | — | I.10 |

Features without integration tests (review queue CLI, `--review` mode, `mark_entry_superseded` in context, REVISE loop) are covered by unit tests or by TESTING.md manual tests. They were excluded from integration tests because they require interactive terminal input beyond simple `y/N` responses.

---

## Windows-Specific Notes

The runner uses several Windows mitigations:

**UTF-8 everywhere.** Agent output contains Unicode characters (`→`, `─`, `═`, `✓`). Without explicit UTF-8 configuration, Windows uses cp1252 which cannot encode them:

```python
# At top of run_tests.py
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# In subprocess calls
subprocess.run(..., encoding="utf-8", errors="replace",
               env={..., "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"})
# Plus Python flag: [sys.executable, "-X", "utf8", "coding_agent_with_judge.py"]
```

**Path separators.** `PROJECT_DIR.parent / "filename"` uses `pathlib.Path` which normalizes separators. The workspace boundary check uses `is_relative_to()` which handles mixed separators correctly on Windows (requires Python 3.9+).

**No `pexpect`.** The `pexpect` library for interactive subprocess control is Unix-only. The pipe-based approach (`subprocess.run(input=...)`) works on both platforms.
