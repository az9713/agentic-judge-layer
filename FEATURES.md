# Feature Documentation: coding_agent_with_judge.py

34 features across three files. Each maps to a concept from Nate Jones's "The Judge Layer Is The Product" and the OpenBrain Judge Extender spec.

**Files:** `coding_agent_with_judge.py` · `judge_memory.py` · `judge_specialists.py`

---

## Setup

```bash
pip install openai python-dotenv
python -c "open('demo.py','w').write('def foo(x):\n    return 1\n\ndef bar(x):\n    return x * 2\n')"
python coding_agent_with_judge.py          # agent session
python coding_agent_with_judge.py --inspect [file]   # memory inspector
python coding_agent_with_judge.py --review            # review queue
```

Startup banner:
```
Coding agent with judge layer  |  actor=gpt-4o  judge=o4-mini (x2 specialists)
Workspace: C:\Users\simon\Downloads\llm_judge_demo  |  policy: v1.0
```

---

## Part 1 — Core Judge Layer

---

## Feature 1: Action Classification (Read-Only Bypass)

**Code:** `RISK_CLASS` dict · `classify_action()` · main loop dispatch — `coding_agent_with_judge.py`

### What it is
`RISK_CLASS = {"read_file": "read_only", "list_files": "read_only", "edit_file": "reversible_write", "run_command": "high_risk"}`. The main loop calls `classify_action(name)` and skips the judge entirely for `read_only`. Tier label appears in cyan: `[tool:read_only]`.

### Nate's concept
> "Not every action needs the same level of judgment. A read-only summary isn't sending an email. Drafting a response isn't sending it."

Nate's four-tier classification. Treating everything as catastrophic makes the product unusable; treating everything as harmless causes incidents. Classification prevents both.

### How to invoke
```
You: list the files in the current directory
```
### What to expect
```
[tool:read_only] list_files({'path': '.'})
Assistant: Here are the files: demo.py, coding_agent_with_judge.py ...
```
No `[JUDGE:]` banner. No latency from judge calls.

---

## Feature 2: Structured Action Proposal

**Code:** `build_action_proposal()` — `coding_agent_with_judge.py`

### What it is
Before any judged call, the runtime constructs a proposal dict mechanically — no LLM call. Contains: `intended_action`, `risk_class`, `arguments_summary` (path, previews, lengths), `expected_consequence`, `is_reversible`, `risk_flags` (outside_workspace, blind_overwrite, file_exists, creates_new_file, overwrites_existing), `sensitivity`. For `run_command`, adds a top-level `command` field. This is the object both specialist judges evaluate.

### Nate's concept
> "Before a side-effectful tool call, the actor should produce a structured action proposal stating the intended action, the reason, the supporting evidence, the authorization basis, the expected consequence, whether rollback is possible, and any risk flags."

Mechanical construction (not LLM) prevents the actor from winning by writing persuasive prose. The judge inspects structured claims against criteria, not the actor's narrative.

### How to invoke
Any `edit_file` or `run_command` — the proposal is invisible at runtime but governs the verdict.
```
You: read demo.py and add a docstring to foo
```
### What to expect
The proposal that reaches both judges (not printed, but deterministic):
```json
{
  "intended_action": "edit_file",
  "risk_class": "reversible_write",
  "arguments_summary": {"path": "C:\\...\\demo.py", "old_str_preview": "def foo(x):", "new_str_preview": "def foo(x):\n    \"\"\"...\"\"\"\n", "old_str_len": 11, "new_str_len": 52},
  "expected_consequence": "Replace first occurrence of old_str with new_str in file at path",
  "is_reversible": false,
  "risk_flags": {"outside_workspace": false, "blind_overwrite": false, "file_exists": true, "creates_new_file": false, "overwrites_existing": false},
  "sensitivity": {"contains_secret_like_data": false}
}
```

---

## Feature 3: Separate Actor and Judge Models

**Code:** `ACTOR_MODEL = "gpt-4o"` · `JUDGE_MODEL = "o4-mini"` · `execute_actor_call()` · `run_judge()` — `coding_agent_with_judge.py`; `_call_specialist()` — `judge_specialists.py`

### What it is
Three separate LLM calls per judged action: actor (`gpt-4o`), auth specialist (`o4-mini`), risk specialist (`o4-mini`). Each has its own system prompt and optimization target. The actor never sees the judge verdict directly — only a translated `tool_result`.

### Nate's concept
> "The actor optimizes for task completion. The judge optimizes for authorization, policy, correctness, privacy, and risk. They can use the same model family, but they shouldn't be the same role."

Splitting roles prevents the actor's task-completion drive from overriding policy checks across a long context window. Two specialist judges (one for auth, one for risk) are narrower and easier to test than a single monolithic judge.

### How to invoke
```
You: read demo.py and add a docstring to foo
```
### What to expect
Both specialist lines appear before the composed JUDGE banner:
```
[tool:reversible_write] edit_file({...}) → judging…
  [auth-judge calling…] PASS (high) — Clear user request; actor scope matches exactly.
  [risk-judge  calling…] PASS (high) — No workspace violations. Targeted replacement.

[JUDGE: ALLOW] (confidence: high) Auth (high): ... | Risk (high): ...
  checks: authorization=pass  risk=pass
```

---

## Feature 4: Four-Outcome Logic — ALLOW

**Code:** `if d == "ALLOW":` branch — `run_coding_agent_loop()` · `coding_agent_with_judge.py`

### What it is
Both specialists PASS with medium or high confidence → composition returns ALLOW. Runtime executes the tool immediately, appends result to conversation, writes decision log. Green banner shows composed reasoning and confidence.

### Nate's concept
> "Allow means the runtime may execute."

ALLOW requires every relevant check to clear. The user gave a clear, targeted instruction; the actor produced a precise edit within the workspace.

### How to invoke
```
You: read demo.py and add a docstring to the foo function
```
### What to expect
```
[JUDGE: ALLOW] (confidence: high) Auth (high): User explicitly requested a docstring... | Risk (high): No violations...
  checks: authorization=pass  risk=pass
```
File is edited. Turn continues; actor writes a summary response.

---

## Feature 5: Four-Outcome Logic — BLOCK

**Code:** `elif d == "BLOCK":` branch — `coding_agent_with_judge.py`

### What it is
Risk specialist FAIL (workspace violation, sensitive data) → BLOCK regardless of auth. Auth specialist FAIL with no fixable hint → BLOCK. Runtime appends a synthetic `tool_result({blocked: true, reason: "..."})`, writes decision log, and ends the turn. The edit never executes.

### Nate's concept
> "Block means it must not. The judge must state which criterion failed and why."

BLOCK dominates the composition. A workspace policy violation is deterministic — it never gets overridden by a persuasive actor justification.

### How to invoke
```
You: update C:\Windows\system32\drivers\etc\hosts to add 127.0.0.1 myapp
```
### What to expect
```
[tool:reversible_write] edit_file({'path': 'C:\\Windows\\...\\hosts', ...}) → judging…
  [auth-judge calling…] PASS (medium) — User explicitly asked to edit this file.
  [risk-judge  calling…] FAIL (high) — outside_workspace=true. Absolute policy: no writes outside workspace.

[JUDGE: BLOCK] (confidence: high) Risk blocked: outside_workspace=true. Absolute policy: no writes outside workspace.
  checks: authorization=pass  risk=fail
```
No file is written. Turn ends.

---

## Feature 6: Four-Outcome Logic — REVISE

**Code:** `elif d == "REVISE":` branch + REVISE loop — `coding_agent_with_judge.py`

### What it is
Auth FAIL with a `revision_hint` → composition returns REVISE. The guidance is appended to the conversation as a user message and the inner loop cycles back to the actor without breaking. The actor retries with the corrected call. Each REVISE also enqueues a lesson candidate in the review queue.

### Nate's concept
> "Revise means the action is directionally valid but needs a specific change before execution. The judge must state what needs to change and why."

REVISE preserves useful autonomy — the agent keeps working rather than stopping. It avoids the brittleness of a simple approve/reject system.

### How to invoke
```
You: change the return value of foo to 2 in demo.py
```
gpt-4o often sends `old_str=""` (blind overwrite). Auth specialist detects scope creep.
### What to expect
```
  [auth-judge calling…] FAIL (high) — Actor is replacing the whole file; user only asked to change the return value.
  [risk-judge  calling…] PASS (medium) — File is in workspace. No sensitive data.

[JUDGE: REVISE] (confidence: high) Authorization requires revision: Actor is replacing the whole file...
  instruction: Use a targeted old_str/new_str that replaces only 'return 1' with 'return 2'; do not overwrite the entire file.

JUDGE REVISION REQUIRED: Use a targeted old_str/new_str...
Please reissue a corrected tool call.
```
Actor retries. Second call typically ALLOWs.

---

## Feature 7: Four-Outcome Logic — ESCALATE

**Code:** `else:` (ESCALATE) branch — `coding_agent_with_judge.py`

### What it is
Either specialist UNCERTAIN, or both PASS but confidence is low → ESCALATE. Human approval is requested in the terminal. If approved, tool executes and an optional note is captured for the review queue. If denied, turn ends. High-risk actions (`run_command`) always escalate regardless of verdict (see Feature 18).

### Nate's concept
> "Escalate means the runtime should route the decision to a human or a higher-trust process."

ESCALATE puts human attention on the decisions that actually require human judgment — not everything, but the ambiguous and high-stakes cases.

### How to invoke
```
You: make demo.py production-ready
```
Vague request → actor sends `old_str=""` on existing file → auth specialist UNCERTAIN.
### What to expect
```
[JUDGE: ESCALATE] (confidence: low) Specialist uncertain: Authorization is ambiguous...
  escalation_reason: Specialist uncertain: User request is vague; blind overwrite of existing file.

[JUDGE: ESCALATE — human approval required]
  path:        C:\...\demo.py
  old_str:     ''
  new_str:     'def foo(x):\n    ...'
  reasoning:   Specialist uncertain...
  Approve this action? [y/N]: y
  Note for review queue (Enter to skip): ok for demo files when explicitly vague
```

---

## Feature 8: Workspace Boundary Enforcement (Always BLOCK)

**Code:** `risk_flags["outside_workspace"]` set in `build_action_proposal()` · risk specialist system prompt

### What it is
`resolve_abs_path(path).is_relative_to(WORKSPACE)` is evaluated deterministically in `build_action_proposal()`. If false, `outside_workspace=True` is set in the proposal. The risk specialist system prompt treats this as an absolute FAIL. No LLM reasoning can override it — the risk specialist returns FAIL immediately.

### Nate's concept
> "Policy: No writes outside workspace (BLOCK)."

Deterministic checks for clear policy violations remove the LLM from the loop entirely for that criterion. The judge can't be persuaded to approve a workspace boundary violation by a well-worded justification.

### How to invoke
```
You: edit the file C:\Windows\system32\notepad.exe to add a comment
```
### What to expect
Risk specialist returns FAIL (high confidence). Composed result: BLOCK. Red banner. No file written.

---

## Feature 9: Blind Overwrite Detection

**Code:** `blind_overwrite`, `overwrites_existing`, `creates_new_file` flags in `build_action_proposal()`

### What it is
When `old_str == ""`, the proposal sets `blind_overwrite=True` plus either `overwrites_existing` or `creates_new_file` depending on whether the file already exists. The risk specialist treats `blind_overwrite + overwrites_existing` as UNCERTAIN (cautious — defers to auth), and the auth specialist checks whether the user explicitly asked for a full rewrite. Together they produce REVISE (scope too wide) or ESCALATE (authorization unclear).

### Nate's concept
> "Did the user explicitly authorize the action? Is that authorization still current? Is the actor extending a prior instruction beyond its intended scope?"

Blind overwrites are the most common form of authorization scope creep in coding agents. The flags make it inspectable rather than requiring the judge to detect it from prose alone.

### How to invoke
```
You: rewrite demo.py to be cleaner
```
### What to expect
Auth specialist: UNCERTAIN or FAIL. Composed result: REVISE or ESCALATE depending on confidence.

---

## Feature 10: REVISE Loop with Max-Revision Protection

**Code:** `revision_count`, `MAX_REVISIONS = 2`, `needs_revision` flag — `run_coding_agent_loop()`

### What it is
When judge returns REVISE, `revision_count` increments and the inner loop continues — the actor sees the revision guidance and retries. When `revision_count > MAX_REVISIONS`, the loop breaks with a warning. `revision_count` resets per user turn. Each REVISE also enqueues a lesson candidate in the review queue.

### Nate's concept
> "The goal isn't to remove human review — it's to put human review on the decisions that actually require human judgment."

Without a cap, a stubborn actor that keeps generating blind overwrites could loop indefinitely. MAX_REVISIONS prevents escalation drift in the other direction (never-ending REVISE cycles).

### How to invoke
Force repeated blind overwrites that the actor doesn't fix.
### What to expect
After 2 failed revisions:
```
[judge] Max revisions (2) exceeded — ending turn.
```

---

## Feature 11: Judge Fail-Closed (ESCALATE on Error)

**Code:** `_call_specialist()` try/except in `judge_specialists.py`; `run_judge()` prints specialist lines

### What it is
Any exception in a specialist LLM call (API error, empty response, JSON parse failure) returns `{"verdict": "UNCERTAIN", "confidence": "low", ...}`. Composition of two UNCERTAIN verdicts → ESCALATE. The system never silently falls back to ALLOW when the judge fails. Specialists use `reasoning_effort="low"` and `max_completion_tokens=800` to reduce latency and token cost.

### Nate's concept
> "Do not write a judge that defaults to ALLOW when uncertain. Uncertainty should produce ESCALATE."

Failing closed is the safe default. An ESCALATE on judge error sends the decision to a human rather than letting a potentially bad action proceed unchecked.

### How to invoke
Not easily triggered manually. Behavior is observable by temporarily setting an invalid model name in `judge_specialists.py`.

---

## Feature 12: Decision Log / Write-Back

**Code:** `write_decision_log()` — `judge_memory.py`; called from all four decision branches in `coding_agent_with_judge.py`

### What it is
Every judged action appends one JSONL line to `judge_decisions.jsonl`. Each entry contains: `event_id` (UUID), `session_id`, `timestamp` (UTC ISO 8601), `policy_version`, `action` (tool, risk_class, path, blind_overwrite, outside_workspace, contains_secret_like_data), `decision`, `confidence`, `reasoning`, `revision_instruction`, `escalation_reason`, `checks` (authorization_check, risk_check, sensitivity_check, policy_check), `human_approved`, `human_note`, `use_policy`, and `provenance` (status, created_by, requires_review).

### Nate's concept
> "Store the judgment event, the source, the decision, the reason, the memory used, the human correction (if any), and the future-use policy."

The write-back loop is what makes judgment durable. Without it, every session starts from zero and the judge never improves.

### How to invoke
Any `edit_file` or `run_command` call. Then inspect the log:
```bash
type judge_decisions.jsonl
```
### What to expect
```json
{"event_id": "a1b2...", "session_id": "27095f7f...", "timestamp": "2026-05-11T14:23:01.123Z", "policy_version": "v1.0", "action": {"tool": "edit_file", "risk_class": "reversible_write", "path": "C:\\...\\demo.py", "blind_overwrite": false, "outside_workspace": false, "contains_secret_like_data": false}, "decision": "ALLOW", "confidence": "high", "reasoning": "Auth (high): ...", "revision_instruction": null, "escalation_reason": null, "checks": {"authorization_check": "pass", "risk_check": "pass", "sensitivity_check": "not_applicable", "policy_check": "not_applicable"}, "human_approved": null, "human_note": null, "use_policy": "can_use_as_evidence", "provenance": {"status": "observed", "created_by": "judge", "requires_review": false}}
```

---

## Feature 13: Provenance Labels (Full 7-Status Set)

**Code:** `provenance.status` field in `write_decision_log()`, `mark_entry_superseded()`, `mark_entry_disputed()`, `process_review_item()` — `judge_memory.py`

### What it is
All seven OB Extender provenance statuses are implemented:
- `observed` — default for all runtime judge decisions
- `user_confirmed` — set when review queue item is confirmed (`[c]`)
- `generated` — set on review queue items before confirmation
- `inferred` — set when review queue item is downgraded (`[d]`)
- `superseded` — set by `mark_entry_superseded()` when a newer decision replaces an older one
- `disputed` — set by `mark_entry_disputed()` or review queue `[D]ispute`
- `imported` — stub only

Recall filters to `{"observed", "user_confirmed"}` only — never returns superseded, disputed, generated, or inferred entries.

### Nate's concept
> "Those distinctions aren't bureaucracy — they determine whether a future agent is allowed to use the memory as instruction or only as evidence."

Without provenance, every prior decision looks equally trustworthy. A blocked action from last week shouldn't carry the same weight as a user-confirmed policy.

### How to invoke
Approve an ESCALATE → entry gets `observed`. Run `--review`, confirm → upgrades to `user_confirmed`. Run `--review`, dispute → source entry becomes `disputed`.
### What to expect
`--inspect` shows `provenance: observed` or `provenance: user_confirmed` per entry.

---

## Feature 14: Judge Recall from Prior Decisions

**Code:** `recall_prior_decisions()`, `_format_recalled_decisions()` — `judge_memory.py`; called at top of `run_judge()` — `coding_agent_with_judge.py`

### What it is
Before each judge call, `recall_prior_decisions()` reads `judge_decisions.jsonl`, filters to safe provenance statuses, prioritizes entries with the same file path, and returns the most recent 5. These are formatted with `use_policy` labels and injected into the auth specialist's context as `PRIOR DECISIONS FOR THIS FILE (most recent last)`.

### Nate's concept
> "Before a judge decision, a runtime asks OpenBrain for scoped, policy-aware recall: prior decisions, relevant policies, user-confirmed preferences, source references, provenance labels, freshness, confidence, and use restrictions."

Without recall, the judge starts every decision from zero. With it, the judge can detect patterns — e.g., this file was blocked twice for blind overwrites this session — and apply appropriate scrutiny.

### How to invoke
Make two `edit_file` calls to the same file in one session.
### What to expect
On the second call, the auth specialist receives context like:
```
PRIOR DECISIONS FOR THIS FILE (most recent last):
[2026-05-11T14:23:01] REVISE — demo.py (use:can_) — "Actor replacing whole file; user only asked for return value change."
[2026-05-11T14:24:10] ALLOW — demo.py (use:can_) — "Targeted replacement of return 1 with return 2."
```

---

## Feature 15: Session Metrics

**Code:** `_metrics` dict, `_update_metrics()`, `print_session_metrics()` — `coding_agent_with_judge.py`

### What it is
In-memory tracking per session: total judge calls, per-decision counts (ALLOW/BLOCK/REVISE/ESCALATE), per-risk-class breakdowns, revision loops, human approvals/denials, session start time. Printed on Ctrl-C/EOF. Rate-budget warnings fire live during the session (see Feature 32 for detail).

### Nate's concept
> "Track false allows, false blocks, escalation rate, revision rate, latency added, cost per judged action, human override rate."

If you can't measure the judge, you don't have a control layer — you have another model call.

### How to invoke
Make several edits with varying outcomes, then press Ctrl-C.
### What to expect
```
────────────────────────────────────────────────────────
  Session Metrics
────────────────────────────────────────────────────────
  Judge calls:      5
  Decisions:        ALLOW 2  BLOCK 1  REVISE 1  ESCALATE 1
  Escalation rate:  20.0%   Revision rate: 20.0%
  reversible_write    : 4 calls  [ALLOW=2 BLOCK=1 REVISE=1]
  high_risk           : 1 calls  [ESCALATE=1]
  Human approvals:  1   Human denials: 0
  Revision loops:   1
  Session duration: 0:04:12
  Decision log:     C:\...\judge_decisions.jsonl
────────────────────────────────────────────────────────
```

---

## Part 2 — Risk Classification and High-Risk Tools

---

## Feature 16: Formal 4-Tier Risk Classification System

**Code:** `RISK_CLASS` dict, `classify_action()` — `coding_agent_with_judge.py`

### What it is
`RISK_CLASS` is a module-level dict mapping tool names to tier strings: `"read_only"`, `"reversible_write"`, `"high_risk"`. `classify_action(tool_name)` returns the tier (or `"unknown"` for unregistered tools). The main loop switches on this value — not on hardcoded tool names — so adding a new tool only requires updating the dict. The tier is shown in every cyan tool banner.

### Nate's concept
> "A practical classification has four classes: Read-only, Reversible writes, External side effects, High-risk actions. This classification prevents two common failures."

This demo implements three of the four tiers. The tier label in the terminal banner makes the classification visible and auditable at a glance.

### How to invoke
Use any tool — the tier always appears in the cyan banner.
### What to expect
```
[tool:read_only]        list_files({...})
[tool:reversible_write] edit_file({...}) → judging…
[tool:high_risk]        run_command({...}) → judging…
```

---

## Feature 17: `run_command` High-Risk Tool

**Code:** `run_command_tool()`, `TOOL_REGISTRY` — `coding_agent_with_judge.py`

### What it is
A fourth tool in `TOOL_REGISTRY`. Executes a shell command via `subprocess.run(shell=True, capture_output=True, timeout=30, cwd=WORKSPACE)`. Returns `{command, stdout (truncated to 2000 chars), stderr (truncated to 500 chars), returncode}`. On timeout or exception returns `{command, error: "..."}`. The actor system prompt explicitly warns that this tool always requires human approval.

### Nate's concept
> "High-risk action: executing production commands. Judge plus human approval path unless the system has a very narrow, explicit policy that permits automation."

`run_command` completes the four-tier classification and makes the always-escalate rule tangible. A user trying to delete files or run a script sees the full judge + mandatory escalation path.

### How to invoke
```
You: run the command: echo hello world
You: run the command: dir
You: run the command: del demo.py
```
### What to expect
```
[tool:high_risk] run_command({'command': 'echo hello world'}) → judging…
  [auth-judge calling…] PASS (high) — User explicitly asked to run this echo command.
  [risk-judge  calling…] PASS (high) — echo is a safe read-only command.

[JUDGE: ESCALATE] (confidence: high) high_risk action — always requires human approval (judge said ALLOW; confidence: high)
  command:     echo hello world
  reasoning:   high_risk action — always requires human approval...
  Approve this action? [y/N]:
```

---

## Feature 18: High-Risk Always-Escalate Enforcement

**Code:** `if risk_class == "high_risk" and d == "ALLOW":` override — `run_coding_agent_loop()` · `coding_agent_with_judge.py`

### What it is
After the specialists run and the composition returns a verdict, the main loop checks the risk class. If `high_risk` and the verdict is ALLOW, the runtime overrides to ESCALATE before displaying or logging. The banner explains the override: `"high_risk action — always requires human approval (judge said ALLOW; confidence: high)"`. Human approval is mandatory for every `run_command` call regardless of judge confidence.

### Nate's concept
> "High-risk actions: Judge plus a human approval path, unless the system has a very narrow, explicit policy that permits automation."

A runtime rule that ignores judge confidence for an entire risk class is cleaner than asking the judge to self-escalate. The judge still provides reasoning — the human reads it before approving — but the human decision cannot be bypassed.

### How to invoke
```
You: run the command: echo safe_message
```
Even with ALLOW from specialists, you will still see `Approve? [y/N]:`.
### What to expect
The JUDGE banner says `[JUDGE: ESCALATE]` even though both specialists passed. The escalation_reason includes `"judge said ALLOW"`.

---

## Part 3 — Specialist Judge Architecture

---

## Feature 19: Authorization Specialist Judge

**Code:** `run_authorization_judge()`, `AUTH_JUDGE_PROMPT` — `judge_specialists.py`

### What it is
A focused `o4-mini` call with a single-responsibility system prompt: evaluate ONLY whether the user explicitly authorized this action. Receives: prior decisions (with use_policy labels), formatted conversation (last 10 messages, truncated to 400 chars each), and action summary (path, old/new previews, consequence). Returns `{verdict: PASS|FAIL|UNCERTAIN, reasoning, confidence: high|medium|low, revision_hint}`. Uses `reasoning_effort="low"` and `max_completion_tokens=800`. Ignores all risk signals — those go to the risk specialist.

### Nate's concept
> "An authorization judge asks whether the user approved this class of action."

Narrow scope makes the authorization judge easier to write (short focused prompt), easier to test (one criterion), and easier to replace without changing risk logic.

### How to invoke
Any `edit_file` or `run_command`:
```
You: add a type annotation to foo in demo.py
```
### What to expect
```
  [auth-judge calling…] PASS (high) — User explicitly requested type annotation on foo. Actor scope matches request.
```

---

## Feature 20: Risk Specialist Judge

**Code:** `run_risk_judge()`, `RISK_JUDGE_PROMPT` — `judge_specialists.py`

### What it is
A focused `o4-mini` call that evaluates ONLY the technical risk signals in the proposal: `outside_workspace`, `blind_overwrite`, `contains_secret_like_data`, `is_reversible`, `risk_class`. For `run_command` proposals, evaluates the command string for destructive patterns (del/rm/format/shutdown) or unsafe network calls. Returns the same `{verdict, reasoning, confidence, revision_hint}` structure. Does not read the conversation — only the proposal's risk fields.

### Nate's concept
> "A privacy judge asks what data is being exposed and to whom. Each specialist has a tighter scope, which makes it easier to write, test, tune, and replace."

Risk judgment is often deterministic (workspace boundary, sensitivity flags) and should be separated from the linguistic authorization question. The risk specialist can be replaced with a faster rule-based check for high-volume actions.

### How to invoke
```
You: edit the file C:\Windows\hosts
You: add password=hunter2 to demo.py
```
### What to expect
```
  [risk-judge  calling…] FAIL (high) — outside_workspace=true. No writes outside workspace permitted.
  [risk-judge  calling…] FAIL (high) — contains_secret_like_data=true. Sensitive pattern detected.
```

---

## Feature 21: Specialist Composition Logic

**Code:** `compose_specialist_verdicts()` — `judge_specialists.py`

### What it is
Combines the two specialist verdicts into a single 4-way decision following six ordered rules:
1. Risk FAIL → BLOCK
2. Auth FAIL + `revision_hint` → REVISE
3. Auth FAIL + no hint → BLOCK
4. Either UNCERTAIN → ESCALATE
5. Both PASS but either confidence `"low"` → ESCALATE
6. Both PASS, both medium/high → ALLOW

Overall confidence is the minimum of the two specialists' confidences. The `checks` dict records each specialist's verdict for the decision log and terminal display.

### Nate's concept
> "BLOCK usually dominates an allow. An escalation dominates an allow when confidence is low and risk is high. A revision wins when the action is basically right but fixable. An allow requires every relevant check to clear."

The composition rules encode Nate's risk philosophy in explicit, testable logic. Each rule can be independently evaluated and changed without touching the specialist prompts.

### How to invoke
The composition runs automatically after each pair of specialist calls. Test edge cases:
```
You: make demo.py production-ready       ← auth UNCERTAIN → ESCALATE
You: change return 1 to return 2         ← auth FAIL + hint → REVISE
You: edit C:\Windows\hosts               ← risk FAIL → BLOCK
You: add a docstring to foo              ← both PASS → ALLOW
```

---

## Feature 22: Confidence Scoring

**Code:** `confidence` field in specialist outputs + composition — `judge_specialists.py`; stored in `write_decision_log()` — `judge_memory.py`; displayed in `print_judge_decision()` — `coding_agent_with_judge.py`

### What it is
Each specialist returns `confidence: "high" | "medium" | "low"`. Composition takes the minimum (`{"high": 2, "medium": 1, "low": 0}`). The final decision carries overall confidence. Displayed in the JUDGE banner as `(confidence: high)`. Stored in the decision log. Low confidence from either specialist → composition escalates to human (Rule 5 of composition).

### Nate's concept
> "An escalation dominates an allow when confidence is low and risk is high."

Confidence scoring makes the judge's uncertainty explicit and actionable. A judge that is uncertain should escalate, not guess — Rule 5 enforces this automatically.

### How to invoke
Inspect `--inspect` output to see `confidence=high/medium/low` per entry. Vague requests typically produce lower confidence.

---

## Feature 23: `checks` Object in Decision

**Code:** `checks` dict built in `compose_specialist_verdicts()` — `judge_specialists.py`; stored in log, printed in `print_judge_decision()`

### What it is
Every composed decision includes `checks = {authorization_check, risk_check, sensitivity_check, policy_check}`, each valued `pass | fail | uncertain | not_applicable`. Shown dimly under the JUDGE banner if non-trivial. Stored in decision log. Visible in `--inspect` output per entry.

### Nate's concept
The OB Extender decision schema defines `checks.authorization_check`, `checks.evidence_check`, `checks.policy_check`, `checks.sensitivity_check`, `checks.reversibility_check`. These make the judge's reasoning inspectable and testable — you can write eval cases that assert specific check values.

### How to invoke
Any `edit_file` — observe the dim `checks:` line:
```
  checks: authorization=pass  risk=pass
```
For a workspace violation:
```
  checks: authorization=pass  risk=fail
```

---

## Part 4 — Memory Governance

---

## Feature 24: Memory Use Policies

**Code:** `_USE_POLICY` dict, `use_policy` field — `write_decision_log()` · `judge_memory.py`; `_format_recalled_decisions()` shows label in recall context

### What it is
Every log entry carries a `use_policy` field:
- `can_use_as_evidence` — default for ALLOW, BLOCK, REVISE, and denied ESCALATE
- `requires_confirmation` — set for approved ESCALATE; upgradeable to `can_use_as_instruction` via review queue
- `can_use_as_instruction` — only after human confirms in review queue

Recall context shows the label: `(use:can_)` for evidence, `(use:req_)` for requires_confirmation, `(use:can_)` for instruction. The judge system prompt explains what each label means.

### Nate's concept
> "can_use_as_instruction should require human confirmation. can_use_as_evidence can include observed events, imported policies, and reviewed decision history. requires_confirmation should be the default for inferred or generated future-facing memories."

Use policies enforce that agent-generated lessons don't silently become binding instructions. The judge can read prior decisions as evidence while the human decides whether to elevate them to instruction-grade.

### How to invoke
Approve an ESCALATE → entry gets `requires_confirmation`. Run `--review`, confirm → upgrades to `can_use_as_instruction`. Then run `--inspect` to verify.

---

## Feature 25: Full 7-Status Provenance Set

**Code:** `provenance.status` field + `mark_entry_superseded()`, `mark_entry_disputed()`, `process_review_item()` — `judge_memory.py`

### What it is
All seven OB Extender statuses are live:
| Status | How it's set |
|--------|-------------|
| `observed` | Default for all runtime judge decisions |
| `user_confirmed` | Review queue `[c]onfirm` action |
| `generated` | Review queue items before confirmation |
| `inferred` | Review queue `[d]owngrade` action |
| `superseded` | `mark_entry_superseded(event_id)` |
| `disputed` | `mark_entry_disputed(event_id)` or review queue `[D]ispute` |
| `imported` | Stub only |

Recall filter: only `{"observed", "user_confirmed"}` are safe to inject.

### Nate's concept
> "Superseded means a newer memory replaces it. Disputed means another source or user correction conflicts with it. Those distinctions aren't bureaucracy — they determine whether a future agent is allowed to use the memory as instruction or only as evidence."

### How to invoke
```bash
python coding_agent_with_judge.py --review   # then [D]ispute an item
python coding_agent_with_judge.py --inspect  # see provenance: disputed
```

---

## Feature 26: Review Queue with Instruction-Grade Gating

**Code:** `enqueue_for_review()` — `judge_memory.py`; called from REVISE and ESCALATE branches — `coding_agent_with_judge.py`

### What it is
`judge_review_queue.jsonl` accumulates items automatically:
- Every approved ESCALATE → lesson candidate with `suggested_use_policy: "requires_confirmation"`
- Every REVISE outcome → lesson candidate with `suggested_use_policy: "can_use_as_evidence"`
- BLOCK and ALLOW do not generate review items (observed evidence is sufficient)

Each item: `queue_id`, `source_decision_id`, `source_event_summary`, `reason_for_review`, `proposed_memory` (the lesson text), `suggested_use_policy`, `provenance_candidate`, `review_status`, `reviewed_at`, `review_action`, `reviewer_note`. Nothing becomes `can_use_as_instruction` without explicit human confirmation in the review CLI.

### Nate's concept
> "The review queue is the safety valve between decision history and future instruction. The default review stance should be conservative. If the system is not sure whether a generated lesson should guide future behavior, it should remain evidence, not instruction."

### How to invoke
```
You: make demo.py production-ready    ← approve the ESCALATE
```
Then:
```bash
type judge_review_queue.jsonl
```
### What to expect
```json
{"queue_id": "b3c4...", "source_event_summary": "ESCALATE approved — demo.py", "reason_for_review": "escalation approved — lesson candidate", "proposed_memory": "User approved: Create or overwrite file on .../demo.py", "suggested_use_policy": "requires_confirmation", "provenance_candidate": "user_confirmed", "review_status": "pending", "reviewer_note": "ok for demo files when explicitly vague"}
```

---

## Feature 27: Review Queue CLI (`--review`)

**Code:** `run_review_cli()` — `judge_memory.py`; `--review` CLI mode — `coding_agent_with_judge.py`

### What it is
Interactive terminal loop through all `pending` review queue items. For each item, shows source summary, reason, proposed memory text, suggested policy, and reviewer note. Available actions: `[c]onfirm` (upgrades source decision to `user_confirmed` + `can_use_as_instruction`), `[e]dit` proposed memory text, `[d]owngrade` to `can_use_as_evidence`, `[r]eject`, `[D]ispute` source decision (marks it disputed), `[s]kip`.

### Nate's concept
> "Review actions should include confirm, edit, mark as evidence only, restrict scope, mark stale, merge, reject, and escalate to admin."

Human review is a product surface — targeted at the edge cases that actually need judgment, not a blanket approval process.

### How to invoke
```bash
python coding_agent_with_judge.py --review
```
### What to expect
```
══════════════════════════════════════════════════════════════
  Review Queue — 2 pending item(s)
  (Nothing becomes instruction-grade without your explicit confirmation.)
══════════════════════════════════════════════════════════════

── Item 1 of 2 ────────────────────────────────────────────────
  source:    ESCALATE approved — demo.py
  reason:    escalation approved — lesson candidate
  proposed:  "User approved: Create or overwrite file on demo.py"
  policy:    requires_confirmation  →  provenance candidate: user_confirmed
  note:      ok for demo files when explicitly vague

  [c] confirm → can_use_as_instruction
  [e] edit proposed memory text
  [d] downgrade → can_use_as_evidence only
  [r] reject
  [D] dispute source decision
  [s] skip
  > c
  ✓ Confirmed — upgraded to user_confirmed / can_use_as_instruction
```

---

## Feature 28: Memory Inspector CLI (`--inspect`)

**Code:** `run_inspector()` — `judge_memory.py`; `--inspect` CLI mode — `coding_agent_with_judge.py`

### What it is
Reads `judge_decisions.jsonl`, displays all entries newest-first. Each entry shows: decision (colored), timestamp, confidence, policy version, path, risk class, combined reasoning, checks breakdown, provenance status, use_policy, human approval info and note, session ID prefix.

Filters: `--inspect demo.py` (basename match), `--inspect --session <uuid>` (session filter).

### Nate's concept
> "The inspector should answer: Why does this memory exist? Which action or judge decision created it? Was it observed, inferred, generated, confirmed, disputed, or superseded? Which workflows retrieved it? Was it used as instruction, evidence, or background?"

Without inspection, the memory system is a black box. The inspector closes the accountability loop.

### How to invoke
```bash
python coding_agent_with_judge.py --inspect
python coding_agent_with_judge.py --inspect demo.py
```
### What to expect
```
══════════════════════════════════════════════════════════════
  Memory Inspector — 3 entries
══════════════════════════════════════════════════════════════

──────────────────────────────────────────────────────────────
  [ALLOW]  2026-05-11T14:23:01  confidence=high  policy=v1.0
  file:       C:\...\demo.py
  risk_class: reversible_write
  reasoning:  Auth (high): Clear user request; scope matches. | Risk (high): No violations.
  checks:     authorization=pass  risk=pass
  provenance: observed  use_policy: can_use_as_evidence
  session:    27095f7f…
```

---

## Feature 29: `mark_entry_superseded` / `mark_entry_disputed`

**Code:** `mark_entry_superseded()`, `mark_entry_disputed()` — `judge_memory.py`; called from `process_review_item()` on `[D]ispute` action

### What it is
`mark_entry_superseded(event_id)` and `mark_entry_disputed(event_id)` perform an in-place rewrite of `judge_decisions.jsonl`, changing the target entry's `provenance.status`. Superseded and disputed entries are excluded from recall — they will never be injected into future judge contexts. Used internally when a newer decision replaces an older one, or when a human disagrees with a prior decision during review.

### Nate's concept
> "Superseded means a newer memory replaces it. Disputed means another source or user correction conflicts with it."

Memory lifecycle management ensures stale or incorrect decisions don't accumulate and mislead future judge calls.

### How to invoke
In `--review`, press `[D]` (dispute) on any item. The source decision log entry becomes `provenance.status: disputed`.
```bash
python coding_agent_with_judge.py --inspect   # verify disputed status
```

---

## Part 5 — Observability and Policy

---

## Feature 30: Sensitivity Detection

**Code:** `_SECRET_PATTERNS`, `_detect_sensitivity()` — `coding_agent_with_judge.py`

### What it is
`_detect_sensitivity(text)` runs three compiled regex patterns over `old_str + new_str` (or command string) before building the proposal:
1. `(api_key|secret|password|token|private_key)\s*[:=]\s*\S+`
2. Common key prefixes: `sk-`, `pk-`, `ghp_`, `eyJ` followed by 10+ alphanumeric chars
3. Long numeric strings: 16+ consecutive digits (card-number-like)

Sets `sensitivity.contains_secret_like_data = True` in the proposal. The risk specialist treats this as an unconditional FAIL.

### Nate's concept
> "What data will be exposed and to whom? What makes this action sensitive even when it looks routine?"

The OB Extender proposal schema includes `sensitivity.contains_secret_like_data`. Deterministic pre-judge checks remove the LLM from the loop for clear-cut sensitive content.

### How to invoke
```
You: add API_KEY = "sk-abc123def456ghi789" to demo.py
```
### What to expect
```
  [risk-judge  calling…] FAIL (high) — contains_secret_like_data=true. Sensitive data pattern detected in new_str.

[JUDGE: BLOCK] (confidence: high) Risk blocked: contains_secret_like_data=true.
  checks: authorization=pass  risk=fail  sensitivity=fail
```

---

## Feature 31: Policy Versioning

**Code:** `JUDGE_POLICY_VERSION = "v1.0"` — `coding_agent_with_judge.py`; stored in every log entry; shown in startup banner

### What it is
`JUDGE_POLICY_VERSION` is a module-level constant stamped on every decision log entry as `policy_version`. The startup banner shows `policy: v1.0`. The `JUDGE_SYSTEM_PROMPT` header includes the version. When judge criteria change, bump the version — past entries in the log preserve the version that governed them, enabling audit trails.

### Nate's concept
> "A judge prompt that worked last quarter encodes last quarter's rules. If the company changes its data-handling policy, customer-communication policy, or legal review process, the judge has to know. Without policy versioning, the system silently runs stale judgment."

Policy versioning makes behavioral changes explicit and auditable. `--inspect` shows `policy=v1.0` next to each entry.

### How to invoke
Observe at startup. In `--inspect`:
```
  [ALLOW]  2026-05-11T14:23:01  confidence=high  policy=v1.0
```

---

## Feature 32: Rate Budget Warnings (Live)

**Code:** `ESCALATION_RATE_WARN = 0.30`, `REVISION_RATE_WARN = 0.25`, `_update_metrics()` — `coding_agent_with_judge.py`

### What it is
After each judge call (once ≥3 total to avoid noise), `_update_metrics()` computes running escalation and revision rates. If either exceeds its threshold, a colored warning prints immediately in the terminal — not just at session end.

### Nate's concept
> "Escalation works as an operating rate, not a moral category. If escalation climbs too high, the product stops feeling autonomous. If humans frequently override actions the judge allowed, the judge is too permissive."

Live warnings surface calibration problems during a session rather than only in post-session review.

### How to invoke
Trigger 2 escalations and 1 allow (3 calls, 67% escalation rate):
### What to expect
```
[metrics] Escalation rate 67% — above 30% threshold. Review actor prompt or judge criteria.
```

---

## Feature 33: Per-Risk-Class Metrics Breakdown

**Code:** `_metrics["by_risk_class"]` dict, `print_session_metrics()` — `coding_agent_with_judge.py`

### What it is
`_metrics["by_risk_class"]` tracks separate counters for `reversible_write` and `high_risk` actions (read_only calls are not judged and not counted). The session metrics table prints one row per active risk class, showing total calls and per-decision breakdown.

### Nate's concept
> "Track judge performance by action class rather than only in aggregate — a judge that performs well on read-only tasks may fail on customer communications."

A 20% escalation rate overall may hide a 100% escalation rate on high-risk actions and 0% on reversible writes — the per-class breakdown makes this visible.

### How to invoke
Make both an `edit_file` and a `run_command` call in a session. Ctrl-C to exit.
### What to expect
```
  reversible_write    : 3 calls  [ALLOW=2 REVISE=1]
  high_risk           : 2 calls  [ESCALATE=2]
```

---

## Feature 34: Human Correction Note Capture and Review Queue Enqueue

**Code:** `prompt_user_for_escalation()` returns `(approved, note)` — `coding_agent_with_judge.py`; note passed to `enqueue_for_review()` as `reviewer_note`

### What it is
After the `Approve this action? [y/N]:` prompt, if the user approves, a second prompt appears: `Note for review queue (Enter to skip):`. The note is stored in the decision log entry as `human_note` and passed to `enqueue_for_review()` as `reviewer_note`. When the reviewer later sees the queue item in `--review`, the note appears under the proposed memory, providing the context behind the human's approval decision.

### Nate's concept
> "A human correction should enter the review queue with high priority because it is often the strongest signal for future judge behavior."

The note closes the human → memory → future-judge feedback loop. The reviewer sees not just that a human approved, but why — enabling better judgment about whether to elevate the lesson to instruction-grade.

### How to invoke
```
You: make demo.py production-ready
```
At the escalation prompt: type `y`, then `ok for demo files when explicitly vague`.

Then:
```bash
python coding_agent_with_judge.py --review
```
### What to expect
Review item shows:
```
  note:      ok for demo files when explicitly vague
```

---

## Feature Interaction Map

The table below shows which features activate for each trigger event. "YES" means the feature fires directly; "—" means it does not.

| Trigger | F1 Class | F2 Proposal | F3 Two Models | F4-7 Outcome | F8 Workspace | F9 Blind OW | F10 Revise Loop | F11 Fail-Close | F12 Log | F13 Provenance | F14 Recall | F15 Metrics | F16-18 Risk/Cmd | F19-21 Specialists | F22-23 Conf/Checks | F24-25 UsePol/Prov | F26 Queue | F27-28 CLI | F29 Supersede | F30 Sensitivity | F31 Policy Ver | F32-33 Rate/Class | F34 Note |
|---------|----------|-------------|---------------|--------------|--------------|-------------|-----------------|---------------|---------|---------------|-----------|-------------|-----------------|-------------------|-------------------|-------------------|-----------|-----------|---------------|----------------|---------------|------------------|---------|
| `list files` / `read file` | YES | — | — | — | — | — | — | — | — | — | — | — | YES (tier label) | — | — | — | — | — | — | — | — | — | — |
| `edit_file` → ALLOW | YES | YES | YES | F4 | YES | YES | — | — | YES | YES | YES | YES | YES | YES | YES | YES | — | — | — | YES | YES | YES | — |
| `edit_file` → BLOCK | YES | YES | YES | F5 | YES | YES | — | — | YES | YES | YES | YES | YES | YES | YES | YES | — | — | — | YES | YES | YES | — |
| `edit_file` → REVISE | YES | YES | YES | F6 | YES | YES | YES | — | YES | YES | YES | YES | YES | YES | YES | YES | YES | — | — | YES | YES | YES | — |
| `edit_file` → ESCALATE | YES | YES | YES | F7 | YES | YES | — | — | YES | YES | YES | YES | YES | YES | YES | YES | YES | — | — | YES | YES | YES | YES |
| `run_command` (any) | YES | YES | YES | F7 (always) | — | — | — | — | YES | YES | — | YES | YES | YES | YES | YES | YES | — | — | YES | YES | YES | YES |
| Judge error | — | — | — | F11 | — | — | — | YES | YES | YES | — | YES | — | — | — | YES | — | — | — | — | YES | YES | — |
| `--inspect` | — | — | — | — | — | — | — | — | YES | YES | — | — | — | — | YES | YES | — | YES | YES | — | YES | — | — |
| `--review` | — | — | — | — | — | — | — | — | YES | YES | — | — | — | — | — | YES | YES | YES | YES | — | — | — | YES |
| Ctrl-C exit | — | — | — | — | — | — | — | — | — | — | — | YES | — | — | — | — | — | — | — | — | — | YES | — |
| Rate threshold exceeded | — | — | — | — | — | — | — | — | — | — | — | YES | — | — | — | — | — | — | — | — | — | YES | — |
