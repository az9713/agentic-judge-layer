"""
judge_memory.py — Memory subsystem for the judge layer.

Handles: decision log write-back, recall, provenance labels, use policies,
review queue gating, and memory inspector / review CLI.
"""

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

WORKSPACE: Path = Path.cwd().resolve()
SESSION_ID: str = str(uuid.uuid4())
LOG_FILE: Path = WORKSPACE / "judge_decisions.jsonl"
REVIEW_FILE: Path = WORKSPACE / "judge_review_queue.jsonl"

# Provenance statuses safe to inject into judge context
SAFE_RECALL_STATUSES = {"observed", "user_confirmed"}

# Use-policy defaults by decision outcome
_USE_POLICY = {
    "ALLOW":  "can_use_as_evidence",
    "BLOCK":  "can_use_as_evidence",
    "REVISE": "can_use_as_evidence",
}

# ── Decision Log ───────────────────────────────────────────────────────────────

def write_decision_log(
    proposal: Dict[str, Any],
    decision: Dict[str, Any],
    human_approved: Optional[bool] = None,
    human_note: Optional[str] = None,
    policy_version: str = "v1.0",
) -> str:
    """Append a judgment event to the decision log. Returns event_id."""
    args = proposal.get("arguments_summary", {})
    d = decision.get("decision", "?")

    if d == "ESCALATE":
        use_policy = "requires_confirmation" if human_approved else "can_use_as_evidence"
    else:
        use_policy = _USE_POLICY.get(d, "can_use_as_evidence")

    # Instruction-grade memories need human review before they can guide future behavior.
    requires_review = (d == "ESCALATE" and human_approved is True)

    event_id = str(uuid.uuid4())
    entry = {
        "event_id": event_id,
        "session_id": SESSION_ID,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "policy_version": policy_version,
        "action": {
            "tool": proposal.get("intended_action", "edit_file"),
            "risk_class": proposal.get("risk_class", "reversible_write"),
            "path": args.get("path"),
            "blind_overwrite": proposal.get("risk_flags", {}).get("blind_overwrite"),
            "outside_workspace": proposal.get("risk_flags", {}).get("outside_workspace"),
            "contains_secret_like_data": proposal.get("sensitivity", {}).get("contains_secret_like_data", False),
        },
        "decision": d,
        "confidence": decision.get("confidence", "medium"),
        "reasoning": decision.get("reasoning"),
        "revision_instruction": decision.get("revision_instruction"),
        "escalation_reason": decision.get("escalation_reason"),
        "checks": decision.get("checks", {}),
        "human_approved": human_approved,
        "human_note": human_note,
        "use_policy": use_policy,
        "provenance": {
            "status": "observed",
            "created_by": "judge",
            "requires_review": requires_review,
        },
    }
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
    except OSError:
        pass
    return event_id


def _load_log() -> List[Dict]:
    if not LOG_FILE.exists():
        return []
    entries: List[Dict] = []
    try:
        with open(LOG_FILE, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        entries.append(json.loads(line))
                    except json.JSONDecodeError:
                        pass
    except OSError:
        pass
    return entries


def _save_log(entries: List[Dict]) -> None:
    try:
        with open(LOG_FILE, "w", encoding="utf-8") as f:
            for e in entries:
                f.write(json.dumps(e) + "\n")
    except OSError:
        pass


def mark_entry_superseded(event_id: str) -> None:
    entries = _load_log()
    for e in entries:
        if e.get("event_id") == event_id:
            e["provenance"]["status"] = "superseded"
    _save_log(entries)


def mark_entry_disputed(event_id: str) -> None:
    entries = _load_log()
    for e in entries:
        if e.get("event_id") == event_id:
            e["provenance"]["status"] = "disputed"
    _save_log(entries)


# ── Recall ─────────────────────────────────────────────────────────────────────

def recall_prior_decisions(proposal: Dict[str, Any], max_items: int = 5) -> List[Dict]:
    """Return recent relevant prior decisions scoped to the same file, filtered
    to safe provenance statuses only (never inferred, generated, disputed, superseded)."""
    entries = _load_log()
    target_path = proposal.get("arguments_summary", {}).get("path")
    unsafe = {"superseded", "disputed", "generated", "inferred"}
    entries = [e for e in entries
               if e.get("provenance", {}).get("status") in SAFE_RECALL_STATUSES
               and e.get("provenance", {}).get("status") not in unsafe]
    same = [e for e in entries if e.get("action", {}).get("path") == target_path]
    other = [e for e in entries if e.get("action", {}).get("path") != target_path]
    # Prioritize same-path: reserve slots for them first, fill rest with other entries.
    # Then sort chronologically so the judge sees oldest→newest (most recent last).
    n_same = min(len(same), max_items)
    n_other = max_items - n_same
    selected = same[-n_same:] + (other[-n_other:] if n_other > 0 else [])
    return sorted(selected, key=lambda e: e.get("timestamp", ""))


def _format_recalled_decisions(entries: List[Dict]) -> str:
    lines = []
    for e in entries:
        ts = e.get("timestamp", "")[:19]
        dec = e.get("decision", "?")
        path = (e.get("action", {}).get("path") or "").split("\\")[-1].split("/")[-1]
        use_pol = (e.get("use_policy") or "")[:4]
        reason = (e.get("reasoning") or "")[:120]
        lines.append(f"[{ts}] {dec} — {path} (use:{use_pol}) — \"{reason}\"")
    return "\n".join(lines)


# ── Review Queue ────────────────────────────────────────────────────────────────

def enqueue_for_review(
    source_event_id: str,
    source_summary: str,
    reason: str,
    proposed_memory: str,
    suggested_use_policy: str = "requires_confirmation",
    reviewer_note: Optional[str] = None,
) -> None:
    """Append a candidate memory to the review queue.
    Nothing becomes instruction-grade automatically — human review is required."""
    item = {
        "queue_id": str(uuid.uuid4()),
        "session_id": SESSION_ID,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "source_decision_id": source_event_id,
        "source_event_summary": source_summary,
        "reason_for_review": reason,
        "proposed_memory": proposed_memory,
        "suggested_use_policy": suggested_use_policy,
        "provenance_candidate": "user_confirmed" if "approved" in reason else "generated",
        "review_status": "pending",
        "reviewed_at": None,
        "review_action": None,
        "reviewer_note": reviewer_note,
    }
    try:
        with open(REVIEW_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(item) + "\n")
    except OSError:
        pass


def _load_review() -> List[Dict]:
    if not REVIEW_FILE.exists():
        return []
    items: List[Dict] = []
    try:
        with open(REVIEW_FILE, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        items.append(json.loads(line))
                    except json.JSONDecodeError:
                        pass
    except OSError:
        pass
    return items


def _save_review(items: List[Dict]) -> None:
    try:
        with open(REVIEW_FILE, "w", encoding="utf-8") as f:
            for item in items:
                f.write(json.dumps(item) + "\n")
    except OSError:
        pass


def process_review_item(queue_id: str, action: str, edited_content: Optional[str] = None) -> None:
    """Apply a reviewer action to a queue item.
    Actions: confirm | edit | downgrade | reject | dispute | mark_stale
    """
    items = _load_review()
    for item in items:
        if item.get("queue_id") != queue_id:
            continue
        item["reviewed_at"] = datetime.now(timezone.utc).isoformat()

        if action == "confirm":
            item["review_status"] = "confirmed"
            item["review_action"] = "confirm"
            # Upgrade source decision's provenance to user_confirmed
            entries = _load_log()
            for e in entries:
                if e.get("event_id") == item.get("source_decision_id"):
                    e["provenance"]["status"] = "user_confirmed"
                    e["use_policy"] = "can_use_as_instruction"
            _save_log(entries)

        elif action == "edit" and edited_content:
            item["proposed_memory"] = edited_content
            item["review_status"] = "edited_pending"
            item["review_action"] = "edit"

        elif action == "downgrade":
            item["review_status"] = "downgraded"
            item["review_action"] = "downgrade"
            item["suggested_use_policy"] = "can_use_as_evidence"

        elif action == "reject":
            item["review_status"] = "rejected"
            item["review_action"] = "reject"

        elif action == "dispute":
            item["review_status"] = "disputed"
            item["review_action"] = "dispute"
            src_id = item.get("source_decision_id")
            if src_id:
                mark_entry_disputed(src_id)

        elif action == "mark_stale":
            item["review_status"] = "stale"
            item["review_action"] = "mark_stale"

        break
    _save_review(items)


# ── Colors (self-contained so inspector/review work standalone) ────────────────

_C = {
    "ALLOW":    "\033[92m",
    "BLOCK":    "\033[91m",
    "REVISE":   "\033[95m",
    "ESCALATE": "\033[33m",
    "RESET":    "\033[0m",
    "BOLD":     "\033[1m",
    "DIM":      "\033[2m",
}


# ── Memory Inspector CLI ───────────────────────────────────────────────────────

def run_inspector(argv: List[str]) -> None:
    """CLI memory inspector.
    Usage:
      python coding_agent_with_judge.py --inspect
      python coding_agent_with_judge.py --inspect demo.py
      python coding_agent_with_judge.py --inspect --session <id>
    """
    entries = _load_log()
    if not entries:
        print("\nNo decision log entries found.\n")
        return

    # Parse filters from argv
    path_filter: Optional[str] = None
    session_filter: Optional[str] = None
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg == "--inspect":
            if i + 1 < len(argv) and not argv[i + 1].startswith("--"):
                path_filter = argv[i + 1].lower()
                i += 2
                continue
        elif arg == "--session" and i + 1 < len(argv):
            session_filter = argv[i + 1]
            i += 2
            continue
        i += 1

    if path_filter:
        entries = [e for e in entries
                   if path_filter in (e.get("action", {}).get("path") or "").lower()]
    if session_filter:
        entries = [e for e in entries if e.get("session_id") == session_filter]

    entries = list(reversed(entries))

    R = _C["RESET"]
    print(f"\n{'═'*62}")
    print(f"  {_C['BOLD']}Memory Inspector{R} — {len(entries)} entr{'y' if len(entries)==1 else 'ies'}")
    if path_filter:
        print(f"  filter: {path_filter}")
    if session_filter:
        print(f"  session: {session_filter}")
    print(f"{'═'*62}")

    for e in entries:
        dec = e.get("decision", "?")
        color = _C.get(dec, R)
        ts = e.get("timestamp", "")[:19]
        path = e.get("action", {}).get("path") or "?"
        conf = e.get("confidence", "?")
        risk_cls = e.get("action", {}).get("risk_class", "?")
        reasoning = e.get("reasoning") or ""
        checks = e.get("checks", {})
        prov = e.get("provenance", {})
        prov_status = prov.get("status", "?")
        use_pol = e.get("use_policy", "?")
        pol_ver = e.get("policy_version", "?")
        human_approved = e.get("human_approved")
        human_note = e.get("human_note") or ""
        session = (e.get("session_id") or "")[:8]

        print(f"\n{'─'*62}")
        print(f"  {color}[{dec}]{R}  {ts}  confidence={conf}  policy={pol_ver}")
        print(f"  file:       {path}")
        print(f"  risk_class: {risk_cls}")
        print(f"  reasoning:  {reasoning}")
        chk_str = "  ".join(f"{k.replace('_check','')}={v}" for k, v in checks.items())
        if chk_str:
            print(f"  checks:     {chk_str}")
        print(f"  provenance: {prov_status}  use_policy: {use_pol}")
        if human_approved is not None:
            note_str = f"  note: \"{human_note}\"" if human_note else ""
            print(f"  human:      {'approved' if human_approved else 'denied'}{note_str}")
        print(f"  session:    {session}…")

    print(f"\n{'═'*62}\n")


# ── Review Queue CLI ───────────────────────────────────────────────────────────

def run_review_cli() -> None:
    """Interactive review queue CLI.
    Usage: python coding_agent_with_judge.py --review
    """
    all_items = _load_review()
    pending = [i for i in all_items if i.get("review_status") == "pending"]

    if not pending:
        reviewed = len([i for i in all_items if i.get("review_status") != "pending"])
        print(f"\nNo pending review items.{f' ({reviewed} already reviewed)' if reviewed else ''}\n")
        return

    R = _C["RESET"]
    print(f"\n{'═'*62}")
    print(f"  {_C['BOLD']}Review Queue{R} — {len(pending)} pending item(s)")
    print(f"  (Nothing becomes instruction-grade without your explicit confirmation.)")
    print(f"{'═'*62}")

    for idx, item in enumerate(pending, 1):
        queue_id = item.get("queue_id", "?")
        summary = item.get("source_event_summary", "?")
        reason = item.get("reason_for_review", "?")
        proposed = item.get("proposed_memory", "?")
        policy = item.get("suggested_use_policy", "?")
        prov_cand = item.get("provenance_candidate", "?")
        note = item.get("reviewer_note") or ""

        print(f"\n── Item {idx} of {len(pending)} {'─'*42}")
        print(f"  source:    {summary}")
        print(f"  reason:    {reason}")
        print(f"  proposed:  \"{proposed}\"")
        print(f"  policy:    {policy}  →  provenance candidate: {prov_cand}")
        if note:
            print(f"  note:      {note}")
        print()
        print("  [c] confirm → can_use_as_instruction")
        print("  [e] edit proposed memory text")
        print("  [d] downgrade → can_use_as_evidence only")
        print("  [r] reject")
        print("  [D] dispute source decision")
        print("  [s] skip")
        try:
            ans = input("  > ").strip()
        except (KeyboardInterrupt, EOFError):
            print("\nReview interrupted.")
            return

        if ans == "c":
            process_review_item(queue_id, "confirm")
            print(f"  ✓ Confirmed — upgraded to user_confirmed / can_use_as_instruction")
        elif ans == "e":
            try:
                edited = input("  New memory text: ").strip()
            except (KeyboardInterrupt, EOFError):
                edited = ""
            if edited:
                process_review_item(queue_id, "edit", edited)
                print(f"  ✓ Edited — re-confirm on next review pass")
        elif ans == "d":
            process_review_item(queue_id, "downgrade")
            print(f"  ✓ Downgraded to can_use_as_evidence")
        elif ans == "r":
            process_review_item(queue_id, "reject")
            print(f"  ✗ Rejected")
        elif ans == "D":
            process_review_item(queue_id, "dispute")
            print(f"  ⚠ Source decision marked disputed")
        else:
            print(f"  Skipped")

    print(f"\n{'═'*62}\n")
