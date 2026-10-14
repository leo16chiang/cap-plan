"""Review workflow: which status each action is allowed from, and who may take it.

    draft ──submit──▶ submitted ──approve──▶ approved ──signoff──▶ signed_off
      ▲                   │                     │                      │
      └── changes_requested ◀──request_changes──┘                      │
      └──────────────────────────────── reopen ◀───────────────────────┘

Teams (signers) save, submit and sign off. Capacity Planning (admins) approve,
request changes and reopen. The server checks every action against this table.
"""

STATUS_LABELS = {
    "not_started": "Not started",
    "draft": "Draft",
    "submitted": "Awaiting review",
    "changes_requested": "Changes requested",
    "approved": "Ready to sign off",
    "signed_off": "Signed off",
}

EDITABLE = ("not_started", "draft", "changes_requested")

# actor: who may take it. from: statuses it's allowed from. to: resulting status (None = unchanged).
# answers: whether the event carries form answers. note: whether a note is required.
EVENTS = {
    "save": {"actor": "signer", "from": EDITABLE, "to": None, "answers": True, "label": "Saved draft"},
    "submit": {"actor": "signer", "from": EDITABLE, "to": "submitted", "answers": True, "label": "Submitted for review"},
    "request_changes": {"actor": "admin", "from": ("submitted", "approved"), "to": "changes_requested",
                        "note": True, "label": "Changes requested"},
    "approve": {"actor": "admin", "from": ("submitted",), "to": "approved", "label": "Approved for sign-off"},
    "signoff": {"actor": "signer", "from": ("approved",), "to": "signed_off", "label": "Signed off"},
    "reopen": {"actor": "admin", "from": ("signed_off",), "to": "draft", "note": True, "label": "Reopened"},
}

TEAM_EVENTS = [e for e, spec in EVENTS.items() if spec["actor"] == "signer"]
ADMIN_EVENTS = [e for e, spec in EVENTS.items() if spec["actor"] == "admin"]


def next_status(event, current):
    to = EVENTS[event]["to"]
    if to:
        return to
    return "draft" if current == "not_started" else current


def blocked_message(event, status):
    """Explains why an action isn't allowed right now."""
    if status == "signed_off":
        return "This review is already signed off. Ask Capacity Planning to reopen it."
    if status == "submitted" and EVENTS[event]["actor"] == "signer":
        return "This review is waiting for Capacity Planning. You can't change it until they respond."
    if status == "approved" and event in ("save", "submit"):
        return "This review has been approved, so the answers are locked. Sign off, or ask Capacity Planning to send it back."
    if event == "signoff":
        return "Capacity Planning has to approve this review before it can be signed off."
    return f"'{EVENTS[event]['label']}' isn't possible while the review is '{STATUS_LABELS[status]}'."
