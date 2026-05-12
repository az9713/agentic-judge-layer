"""
coding_agent_with_judge.py — Coding agent with full judge layer.

Architecture:
  Actor (gpt-4o)       — completes coding tasks using tools
  Judge (o4-mini x2)   — authorization specialist + risk specialist, composed verdict
  judge_memory.py      — decision log, recall, provenance, use policies, review queue, inspector
  judge_specialists.py — specialist LLM judges and composition logic

Run modes:
  python coding_agent_with_judge.py              — interactive agent session
  python coding_agent_with_judge.py --inspect    — memory inspector
  python coding_agent_with_judge.py --inspect <file>  — filter by filename
  python coding_agent_with_judge.py --review     — interactive review queue
"""

import inspect
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from openai import OpenAI
from dotenv import load_dotenv

# ── Memory subsystem (defines WORKSPACE, SESSION_ID, LOG_FILE, REVIEW_FILE) ───
from judge_memory import (
    WORKSPACE, SESSION_ID, LOG_FILE,
    write_decision_log, recall_prior_decisions, _format_recalled_decisions,
    enqueue_for_review, run_inspector, run_review_cli,
)

# ── Specialist judges ──────────────────────────────────────────────────────────
from judge_specialists import (
    run_authorization_judge, run_risk_judge, compose_specialist_verdicts,
)

load_dotenv()
openai_client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])

# ── Models ─────────────────────────────────────────────────────────────────────
ACTOR_MODEL = "gpt-4o"
JUDGE_MODEL  = "o4-mini"   # used by specialists in judge_specialists.py

# ── Policy ─────────────────────────────────────────────────────────────────────
JUDGE_POLICY_VERSION = "v1.0"
MAX_REVISIONS = 2

# ── Risk classification ────────────────────────────────────────────────────────
RISK_CLASS: Dict[str, str] = {
    "read_file":    "read_only",
    "list_files":   "read_only",
    "edit_file":    "reversible_write",
    "run_command":  "high_risk",
}

def classify_action(tool_name: str) -> str:
    return RISK_CLASS.get(tool_name, "unknown")

# ── Sensitivity detection ──────────────────────────────────────────────────────
_SECRET_PATTERNS = [
    re.compile(r'(?i)(api[_\-]?key|secret|password|passwd|token|private[_\-]?key)\s*[:=]\s*\S+'),
    re.compile(r'(?i)(sk-|pk-|ghp_|eyJ)[A-Za-z0-9_\-]{10,}'),
    re.compile(r'\b[0-9]{16,}\b'),   # card-number-length numeric strings
]

def _detect_sensitivity(text: str) -> Dict[str, bool]:
    contains_secret = any(p.search(text) for p in _SECRET_PATTERNS)
    return {"contains_secret_like_data": contains_secret}

# ── Metrics thresholds ─────────────────────────────────────────────────────────
ESCALATION_RATE_WARN = 0.30
REVISION_RATE_WARN   = 0.25

_metrics: Dict[str, Any] = {
    "total": 0,
    "decisions": {"ALLOW": 0, "BLOCK": 0, "REVISE": 0, "ESCALATE": 0},
    "by_risk_class": {
        "reversible_write": {"total": 0, "ALLOW": 0, "BLOCK": 0, "REVISE": 0, "ESCALATE": 0},
        "high_risk":        {"total": 0, "ALLOW": 0, "BLOCK": 0, "REVISE": 0, "ESCALATE": 0},
    },
    "revision_loops": 0,
    "human_approvals": 0,
    "human_denials": 0,
    "session_start": datetime.now(timezone.utc),
}

# ── Colors ─────────────────────────────────────────────────────────────────────
YOU_COLOR      = "\033[94m"
ASST_COLOR     = "\033[93m"
TOOL_COLOR     = "\033[96m"
ALLOW_COLOR    = "\033[92m"
BLOCK_COLOR    = "\033[91m"
REVISE_COLOR   = "\033[95m"
ESCALATE_COLOR = "\033[33m"
DIM_COLOR      = "\033[2m"
RESET          = "\033[0m"

# ── System prompts ─────────────────────────────────────────────────────────────

ACTOR_SYSTEM_PROMPT = """
You are a coding assistant whose goal it is to help us solve coding tasks.
You have access to a series of tools you can execute. Here are the tools:

{tool_list_repr}

CRITICAL RULES — follow these exactly:
1. When the user gives you a task, proceed IMMEDIATELY with tool calls. Do NOT ask
   "shall I proceed?", "would you like me to?", or seek any confirmation first.
   All safety checks are handled automatically by a judge system — your job is to act.
2. To use a tool, reply with EXACTLY ONE LINE in the format:
     tool: TOOL_NAME({{JSON_ARGS}})
   Use compact single-line JSON with double quotes. No prose before or after the line.
3. After receiving a tool_result(...) message, continue the task immediately.
4. run_command executes a shell command. Use it directly when the user asks — the
   judge system handles approval; you do NOT ask permission yourself.
5. If no tool is needed, respond normally.
"""

JUDGE_SYSTEM_PROMPT = f"""
JUDGE POLICY: {JUDGE_POLICY_VERSION} (coding agent workspace policy)

NOTE: This prompt is used by specialist judges (authorization and risk).
The composition layer combines their verdicts into one final decision.

You will receive prior decisions (if any), the recent conversation, and the
proposed action. The authorization specialist evaluates user intent;
the risk specialist evaluates technical risk signals.

Provenance labels for prior decisions injected into context:
  can_use_as_evidence   — use to inform judgment, not as binding instruction
  requires_confirmation — treat as a signal only; do not act on automatically
  can_use_as_instruction — confirmed by human; can guide behavior directly
""".strip()


# ── Tools ──────────────────────────────────────────────────────────────────────

def resolve_abs_path(path_str: str) -> Path:
    path = Path(path_str).expanduser()
    if not path.is_absolute():
        path = (Path.cwd() / path).resolve()
    return path

def read_file_tool(filename: str) -> Dict[str, Any]:
    """
    Gets the full content of a file provided by the user.
    :param filename: The name of the file to read.
    :return: The full content of the file.
    """
    full_path = resolve_abs_path(filename)
    with open(str(full_path), "r") as f:
        content = f.read()
    return {"file_path": str(full_path), "content": content}

def list_files_tool(path: str) -> Dict[str, Any]:
    """
    Lists the files in a directory provided by the user.
    :param path: The path to a directory to list files from.
    :return: A list of files in the directory.
    """
    full_path = resolve_abs_path(path)
    all_files = []
    for item in full_path.iterdir():
        all_files.append({"filename": item.name, "type": "file" if item.is_file() else "dir"})
    return {"path": str(full_path), "files": all_files}

def edit_file_tool(path: str, old_str: str, new_str: str) -> Dict[str, Any]:
    """
    Replaces first occurrence of old_str with new_str in file. If old_str is empty,
    create/overwrite the file with new_str.
    :param path: The path to the file to edit.
    :param old_str: The string to replace (empty = create/overwrite).
    :param new_str: The replacement string.
    :return: A dictionary with the path and the action taken.
    """
    full_path = resolve_abs_path(path)
    if old_str == "":
        full_path.write_text(new_str, encoding="utf-8")
        return {"path": str(full_path), "action": "created_file"}
    original = full_path.read_text(encoding="utf-8")
    if original.find(old_str) == -1:
        return {"path": str(full_path), "action": "old_str not found"}
    edited = original.replace(old_str, new_str, 1)
    full_path.write_text(edited, encoding="utf-8")
    return {"path": str(full_path), "action": "edited"}

def run_command_tool(command: str) -> Dict[str, Any]:
    """
    Executes a shell command in the current workspace. HIGH-RISK: always requires
    human approval. Commands can modify or delete files and affect system state.
    :param command: The shell command to execute.
    :return: stdout, stderr, and return code.
    """
    try:
        result = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            text=True,
            timeout=30,
            cwd=str(WORKSPACE),
        )
        return {
            "command": command,
            "stdout": result.stdout[:2000],
            "stderr": result.stderr[:500],
            "returncode": result.returncode,
        }
    except subprocess.TimeoutExpired:
        return {"command": command, "error": "command timed out after 30s"}
    except Exception as exc:
        return {"command": command, "error": str(exc)}

TOOL_REGISTRY = {
    "read_file":    read_file_tool,
    "list_files":   list_files_tool,
    "edit_file":    edit_file_tool,
    "run_command":  run_command_tool,
}

def get_tool_str_representation(tool_name: str) -> str:
    tool = TOOL_REGISTRY[tool_name]
    return f"""
    Name: {tool_name}
    Description: {tool.__doc__}
    Signature: {inspect.signature(tool)}
    """

def get_full_system_prompt() -> str:
    tool_str_repr = ""
    for tool_name in TOOL_REGISTRY:
        tool_str_repr += "TOOL\n===" + get_tool_str_representation(tool_name)
        tool_str_repr += f"\n{'='*15}\n"
    return ACTOR_SYSTEM_PROMPT.format(tool_list_repr=tool_str_repr)

def extract_tool_invocations(text: str) -> List[Tuple[str, Dict[str, Any]]]:
    invocations = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line.startswith("tool:"):
            continue
        try:
            after = line[len("tool:"):].strip()
            name, rest = after.split("(", 1)
            name = name.strip()
            if not rest.endswith(")"):
                continue
            json_str = rest[:-1].strip()
            args = json.loads(json_str)
            invocations.append((name, args))
        except Exception:
            continue
    return invocations


# ── Actor LLM call ─────────────────────────────────────────────────────────────

def execute_actor_call(conversation: List[Dict[str, str]]) -> str:
    response = openai_client.chat.completions.create(
        model=ACTOR_MODEL,
        messages=conversation,
        max_tokens=2000,
    )
    return response.choices[0].message.content


# ── Action proposal ────────────────────────────────────────────────────────────

def build_action_proposal(
    tool_name: str,
    args: Dict[str, Any],
    conversation: List[Dict[str, str]],
) -> Dict[str, Any]:
    """Build a structured action proposal from runtime data (no LLM call)."""
    risk_class = classify_action(tool_name)

    if tool_name == "run_command":
        command = args.get("command", "")
        sensitivity = _detect_sensitivity(command)
        return {
            "intended_action": "run_command",
            "risk_class": risk_class,
            "command": command,
            "arguments_summary": {"command": command},
            "expected_consequence": f"Execute shell command: {command[:100]}",
            "is_reversible": False,
            "risk_flags": {
                "outside_workspace": False,
                "file_exists": False,
                "blind_overwrite": False,
                "creates_new_file": False,
                "overwrites_existing": False,
            },
            "sensitivity": sensitivity,
        }

    # edit_file
    resolved_path = resolve_abs_path(args.get("path", ""))
    old_str = args.get("old_str") or ""
    new_str = args.get("new_str") or ""
    file_exists = resolved_path.exists()
    sensitivity = _detect_sensitivity(old_str + new_str)

    return {
        "intended_action": "edit_file",
        "risk_class": risk_class,
        "arguments_summary": {
            "path": str(resolved_path),
            "path_raw": args.get("path", ""),
            "old_str_preview": old_str[:200],
            "new_str_preview": new_str[:200],
            "old_str_len": len(old_str),
            "new_str_len": len(new_str),
        },
        "expected_consequence": (
            "Create or overwrite file at path with new_str"
            if old_str == ""
            else "Replace first occurrence of old_str with new_str in file at path"
        ),
        "is_reversible": False,
        "risk_flags": {
            "outside_workspace": not resolved_path.is_relative_to(WORKSPACE),
            "file_exists": file_exists,
            "blind_overwrite": old_str == "",
            "creates_new_file": old_str == "" and not file_exists,
            "overwrites_existing": old_str == "" and file_exists,
        },
        "sensitivity": sensitivity,
    }


# ── Judge dispatch (delegates to specialists) ──────────────────────────────────

def run_judge(
    proposal: Dict[str, Any],
    conversation: List[Dict[str, str]],
) -> Dict[str, Any]:
    """Run both specialist judges, print their verdicts, return composed decision."""
    prior = recall_prior_decisions(proposal)

    print(f"  {DIM_COLOR}[auth-judge calling…]{RESET}", end=" ", flush=True)
    auth_v = run_authorization_judge(proposal, conversation, prior)
    auth_color = ALLOW_COLOR if auth_v["verdict"] == "PASS" else (
                 BLOCK_COLOR if auth_v["verdict"] == "FAIL" else ESCALATE_COLOR)
    print(f"{auth_color}{auth_v['verdict']}{RESET} ({auth_v['confidence']}) — {auth_v['reasoning']}")

    print(f"  {DIM_COLOR}[risk-judge  calling…]{RESET}", end=" ", flush=True)
    risk_v = run_risk_judge(proposal, prior)
    risk_color = ALLOW_COLOR if risk_v["verdict"] == "PASS" else (
                 BLOCK_COLOR if risk_v["verdict"] == "FAIL" else ESCALATE_COLOR)
    print(f"{risk_color}{risk_v['verdict']}{RESET} ({risk_v['confidence']}) — {risk_v['reasoning']}")

    return compose_specialist_verdicts(auth_v, risk_v, proposal)


# ── Display helpers ────────────────────────────────────────────────────────────

def print_judge_decision(decision: Dict[str, Any]) -> None:
    d = decision.get("decision", "?")
    conf = decision.get("confidence", "")
    color = {"ALLOW": ALLOW_COLOR, "BLOCK": BLOCK_COLOR,
             "REVISE": REVISE_COLOR, "ESCALATE": ESCALATE_COLOR}.get(d, RESET)
    conf_str = f" (confidence: {conf})" if conf else ""
    print(f"\n{color}[JUDGE: {d}]{RESET}{conf_str} {decision.get('reasoning', '')}")
    if d == "REVISE" and decision.get("revision_instruction"):
        print(f"  instruction: {decision['revision_instruction']}")
    if d == "ESCALATE" and decision.get("escalation_reason"):
        print(f"  escalation_reason: {decision['escalation_reason']}")
    checks = decision.get("checks", {})
    if checks:
        chk = "  ".join(f"{k.replace('_check','')}={v}" for k, v in checks.items()
                        if v not in ("not_applicable", None))
        if chk:
            print(f"  {DIM_COLOR}checks: {chk}{RESET}")


def prompt_user_for_escalation(
    proposal: Dict[str, Any],
    decision: Dict[str, Any],
) -> Tuple[bool, Optional[str]]:
    """Return (approved, optional_note)."""
    args = proposal.get("arguments_summary", {})
    print(f"\n{ESCALATE_COLOR}[JUDGE: ESCALATE — human approval required]{RESET}")
    if proposal.get("command"):
        print(f"  command:     {proposal['command']}")
    else:
        print(f"  path:        {args.get('path')}")
        print(f"  old_str:     {args.get('old_str_preview')!r}")
        print(f"  new_str:     {args.get('new_str_preview')!r}")
    print(f"  reasoning:   {decision.get('reasoning')}")
    print(f"  reason:      {decision.get('escalation_reason')}")
    ans = input("  Approve this action? [y/N]: ").strip().lower()
    approved = ans in ("y", "yes")
    note: Optional[str] = None
    if approved:
        try:
            raw_note = input("  Note for review queue (Enter to skip): ").strip()
            if raw_note:
                note = raw_note
        except (KeyboardInterrupt, EOFError):
            pass
    return approved, note


# ── Metrics ────────────────────────────────────────────────────────────────────

def _update_metrics(decision_str: str, risk_class: str) -> None:
    _metrics["total"] += 1
    _metrics["decisions"][decision_str] += 1
    rc = _metrics["by_risk_class"].get(risk_class)
    if rc is not None:
        rc["total"] += 1
        if decision_str in rc:
            rc[decision_str] += 1
    # Rate-budget warnings (only after ≥ 3 judge calls to avoid noise)
    total = _metrics["total"]
    if total >= 3:
        esc_rate = _metrics["decisions"]["ESCALATE"] / total
        rev_rate = _metrics["decisions"]["REVISE"] / total
        if esc_rate > ESCALATION_RATE_WARN:
            print(f"{ESCALATE_COLOR}[metrics] Escalation rate {esc_rate:.0%} — "
                  f"above {ESCALATION_RATE_WARN:.0%} threshold. "
                  f"Review actor prompt or judge criteria.{RESET}")
        if rev_rate > REVISION_RATE_WARN:
            print(f"{REVISE_COLOR}[metrics] Revision rate {rev_rate:.0%} — "
                  f"above {REVISION_RATE_WARN:.0%} threshold. "
                  f"Review actor prompt specificity.{RESET}")


def print_session_metrics() -> None:
    m = _metrics
    total = m["total"]
    if total == 0:
        return
    elapsed = datetime.now(timezone.utc) - m["session_start"]
    elapsed_str = str(elapsed).split(".")[0]
    d = m["decisions"]
    esc_rate = 100 * d["ESCALATE"] / total
    rev_rate = 100 * d["REVISE"] / total
    print(f"\n{'─'*54}")
    print(f"  Session Metrics")
    print(f"{'─'*54}")
    print(f"  Judge calls:      {total}")
    print(f"  Decisions:        ALLOW {d['ALLOW']}  BLOCK {d['BLOCK']}  "
          f"REVISE {d['REVISE']}  ESCALATE {d['ESCALATE']}")
    print(f"  Escalation rate:  {esc_rate:.1f}%   Revision rate: {rev_rate:.1f}%")
    for rc, counts in m["by_risk_class"].items():
        if counts["total"] > 0:
            sub = "  ".join(f"{k}={v}" for k, v in counts.items() if k != "total" and v > 0)
            print(f"  {rc:20s}: {counts['total']} calls  [{sub}]")
    print(f"  Human approvals:  {m['human_approvals']}   Human denials: {m['human_denials']}")
    print(f"  Revision loops:   {m['revision_loops']}")
    print(f"  Session duration: {elapsed_str}")
    if LOG_FILE.exists():
        print(f"  Decision log:     {LOG_FILE}")
    print(f"{'─'*54}\n")


# ── Main agent loop ────────────────────────────────────────────────────────────

def _execute_tool(name: str, args: Dict[str, Any]) -> Any:
    """Execute a tool call and return the result dict."""
    if name == "read_file":
        return read_file_tool(args.get("filename", "."))
    elif name == "list_files":
        return list_files_tool(args.get("path", "."))
    elif name == "edit_file":
        return edit_file_tool(
            args.get("path", "."),
            args.get("old_str", ""),
            args.get("new_str", ""),
        )
    elif name == "run_command":
        return run_command_tool(args.get("command", ""))
    return {"error": f"unknown tool: {name}"}


def run_coding_agent_loop() -> None:
    conversation: List[Dict[str, str]] = [
        {"role": "system", "content": get_full_system_prompt()}
    ]
    print(f"Coding agent with judge layer  |  actor={ACTOR_MODEL}  judge={JUDGE_MODEL} (x2 specialists)")
    print(f"Workspace: {WORKSPACE}  |  policy: {JUDGE_POLICY_VERSION}\n")

    while True:
        try:
            user_input = input(f"{YOU_COLOR}You:{RESET} ").strip()
        except (KeyboardInterrupt, EOFError):
            print_session_metrics()
            break
        if not user_input:
            continue

        conversation.append({"role": "user", "content": user_input})
        revision_count = 0

        while True:
            assistant_response = execute_actor_call(conversation)
            conversation.append({"role": "assistant", "content": assistant_response})

            tool_invocations = extract_tool_invocations(assistant_response)

            if not tool_invocations:
                print(f"\n{ASST_COLOR}Assistant:{RESET} {assistant_response}\n")
                break

            halt_turn = False
            needs_revision = False

            for name, args in tool_invocations:
                if name not in TOOL_REGISTRY:
                    conversation.append({
                        "role": "user",
                        "content": f'tool_result({{"error": "unknown tool: {name}"}})',
                    })
                    continue

                risk_class = classify_action(name)

                # ── Read-only: no judge ────────────────────────────────────
                if risk_class == "read_only":
                    print(f"{TOOL_COLOR}[tool:{risk_class}] {name}({args}){RESET}")
                    resp = _execute_tool(name, args)
                    conversation.append({
                        "role": "user",
                        "content": f"tool_result({json.dumps(resp)})",
                    })
                    continue

                # ── Reversible write or high-risk: judge first ─────────────
                print(f"{TOOL_COLOR}[tool:{risk_class}] {name}({args}) → judging…{RESET}")
                proposal = build_action_proposal(name, args, conversation)
                decision = run_judge(proposal, conversation)
                print_judge_decision(decision)

                d = decision.get("decision", "ESCALATE")
                _update_metrics(d, risk_class)

                # High-risk always escalates regardless of judge verdict
                if risk_class == "high_risk" and d == "ALLOW":
                    decision = {
                        **decision,
                        "decision": "ESCALATE",
                        "escalation_reason": (
                            "high_risk action — always requires human approval "
                            f"(judge said ALLOW; confidence: {decision.get('confidence')})"
                        ),
                    }
                    d = "ESCALATE"
                    _metrics["decisions"]["ALLOW"] -= 1
                    _metrics["decisions"]["ESCALATE"] += 1
                    by_rc = _metrics["by_risk_class"].get(risk_class, {})
                    if "ALLOW" in by_rc:
                        by_rc["ALLOW"] -= 1
                    if "ESCALATE" in by_rc:
                        by_rc["ESCALATE"] += 1

                if d == "ALLOW":
                    resp = _execute_tool(name, args)
                    conversation.append({
                        "role": "user",
                        "content": f"tool_result({json.dumps(resp)})",
                    })
                    write_decision_log(proposal, decision, policy_version=JUDGE_POLICY_VERSION)

                elif d == "BLOCK":
                    conversation.append({
                        "role": "user",
                        "content": (
                            f'tool_result({{"blocked": true, '
                            f'"reason": {json.dumps(decision.get("reasoning", "blocked by judge"))}}})'
                        ),
                    })
                    write_decision_log(proposal, decision, policy_version=JUDGE_POLICY_VERSION)
                    halt_turn = True
                    break

                elif d == "REVISE":
                    instruction = decision.get("revision_instruction") or "Revise your action and try again."
                    conversation.append({
                        "role": "user",
                        "content": (
                            f"JUDGE REVISION REQUIRED: {instruction}\n"
                            "Please reissue a corrected tool call."
                        ),
                    })
                    event_id = write_decision_log(proposal, decision, policy_version=JUDGE_POLICY_VERSION)
                    # Enqueue revision as a lesson candidate
                    path = proposal.get("arguments_summary", {}).get("path") or proposal.get("command", "?")
                    enqueue_for_review(
                        source_event_id=event_id,
                        source_summary=f"REVISE {path}",
                        reason="actor needed revision — lesson candidate",
                        proposed_memory=f"Actor needed revision for {path}: {instruction}",
                        suggested_use_policy="can_use_as_evidence",
                    )
                    needs_revision = True
                    break

                else:  # ESCALATE
                    approved, note = prompt_user_for_escalation(proposal, decision)
                    if approved:
                        resp = _execute_tool(name, args)
                        conversation.append({
                            "role": "user",
                            "content": f"tool_result({json.dumps(resp)}) [user approved escalation]",
                        })
                        _metrics["human_approvals"] += 1
                    else:
                        conversation.append({
                            "role": "user",
                            "content": 'tool_result({"status": "user_declined_action"})',
                        })
                        _metrics["human_denials"] += 1
                        halt_turn = True

                    event_id = write_decision_log(
                        proposal, decision,
                        human_approved=approved,
                        human_note=note,
                        policy_version=JUDGE_POLICY_VERSION,
                    )

                    if approved:
                        # Human approval is a lesson candidate — needs review before
                        # it can become instruction-grade memory
                        path = proposal.get("arguments_summary", {}).get("path") or proposal.get("command", "?")
                        action_desc = proposal.get("expected_consequence", "action")
                        enqueue_for_review(
                            source_event_id=event_id,
                            source_summary=f"ESCALATE approved — {path}",
                            reason="escalation approved — lesson candidate",
                            proposed_memory=f"User approved: {action_desc} on {path}",
                            suggested_use_policy="requires_confirmation",
                            reviewer_note=note,
                        )

                    if halt_turn:
                        break

            if halt_turn:
                break

            if needs_revision:
                revision_count += 1
                _metrics["revision_loops"] += 1
                if revision_count > MAX_REVISIONS:
                    print(f"{REVISE_COLOR}[judge] Max revisions ({MAX_REVISIONS}) exceeded — ending turn.{RESET}\n")
                    break
                continue


# ── Entry point ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    if "--inspect" in sys.argv:
        run_inspector(sys.argv)
    elif "--review" in sys.argv:
        run_review_cli()
    else:
        run_coding_agent_loop()
