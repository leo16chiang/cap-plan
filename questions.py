"""Review questionnaire. Edit SECTIONS to change the form; rendering, parsing and
validation are driven entirely from this structure."""

from cycles import CURRENT

QUARTERS = [q["label"] for q in CURRENT["quarters"]]

SECTIONS = [
    {
        "title": "1. Workload outlook",
        "intro": "Tell us how you expect your mainframe CPU consumption to change over the next four quarters.",
        "questions": [
            {
                "id": "growth",
                "type": "quarters",
                "label": "Projected change in average monthly CPU (MSU) versus your current baseline",
                "help": "Percent change per quarter relative to the baseline shown above. Use negative numbers for reductions, 0 for no change.",
                "required": True,
                "min": -100,
                "max": 500,
            },
            {
                "id": "driver",
                "type": "select",
                "label": "What is the primary driver of that change?",
                "required": True,
                "options": [
                    "Business / transaction volume growth",
                    "New application or functionality",
                    "Workload migrating off the mainframe",
                    "Decommissioning",
                    "Performance tuning / optimization",
                    "No major change expected",
                ],
            },
            {
                "id": "confidence",
                "type": "radio",
                "label": "How confident are you in these projections?",
                "required": True,
                "options": [
                    "High: based on committed, funded plans",
                    "Medium: likely, but not yet scheduled or funded",
                    "Low: rough estimate",
                ],
            },
        ],
    },
    {
        "title": "2. Planned changes (next 12 months)",
        "questions": [
            {
                "id": "changes",
                "type": "checkboxes",
                "label": "Which of these are planned?",
                "help": "Select all that apply.",
                "required": True,
                "exclusive": "None planned",
                "options": [
                    "Onboarding a new application or workload",
                    "Major release or feature launch",
                    "Moving workload off the mainframe",
                    "Decommissioning an application or component",
                    "Software upgrades (Db2, CICS, IMS, z/OS, compilers)",
                    "Batch schedule or job-stream changes",
                    "Shifting work to zIIP-eligible processing (Java, DRDA)",
                    "Code or SQL tuning initiatives",
                    "None planned",
                ],
            },
            {
                "id": "biggest_change",
                "type": "textarea",
                "label": "Describe the change with the largest expected CPU impact",
                "help": "What is changing, roughly how big, and which LPARs/regions or job streams are affected.",
                "required": True,
                "placeholder": "e.g. Onboarding RTP volumes from the new partner bank in Feb 2027; expect ~15% more CICS transactions.",
            },
            {
                "id": "biggest_change_month",
                "type": "month",
                "label": "Target month for that change",
            },
        ],
    },
    {
        "title": "3. Peaks and batch",
        "questions": [
            {
                "id": "peaks",
                "type": "textarea",
                "label": "Known peak periods or one-time events",
                "help": "Year-end close, open enrollment, campaigns, regulatory deadlines, conversions, DR tests…",
                "rows": 3,
            },
            {
                "id": "batch_window",
                "type": "radio",
                "label": "Will your batch window requirements change?",
                "required": True,
                "options": [
                    "No change",
                    "Need a longer window",
                    "Need a shorter window / earlier completion",
                    "Unsure",
                ],
            },
            {
                "id": "batch_detail",
                "type": "textarea",
                "label": "Batch details (if changing)",
                "rows": 3,
            },
        ],
    },
    {
        "title": "4. Look back",
        "questions": [
            {
                "id": "variance_explanation",
                "type": "textarea",
                "callout": "lookback",
                "label": "What drove the difference between last cycle's projection and actual usage?",
                "rows": 3,
            },
        ],
    },
    {
        "title": "5. Follow-up",
        "questions": [
            {
                "id": "extra_contacts",
                "type": "text",
                "label": "Anyone else we should include in follow-up?",
                "placeholder": "Names or email addresses",
            },
            {
                "id": "meeting",
                "type": "radio",
                "label": "Would you like a review meeting with Capacity Planning?",
                "required": True,
                "options": ["Yes, set up a 30-minute review", "No, this submission is enough"],
            },
            {
                "id": "comments",
                "type": "textarea",
                "label": "Anything else we should know?",
                "rows": 3,
            },
        ],
    },
]

ALL_QUESTIONS = [q for s in SECTIONS for q in s["questions"]]
MAX_TEXT = 4000
OTHER = "Other"
CHOICE_TYPES = ("select", "radio", "checkboxes")

# Every choice question gets an "Other" option with its own text box, unless it sets "other": False.
for _q in ALL_QUESTIONS:
    if _q["type"] in CHOICE_TYPES and _q.get("other", True) and OTHER not in _q["options"]:
        _q["options"] = _q["options"] + [OTHER]
        _q["has_other"] = True


def other_key(qid):
    return f"{qid}__other"


def _other_selected(q, value):
    return q.get("has_other") and (OTHER in value if isinstance(value, list) else value == OTHER)


def parse_answers(form):
    """Pull answers out of a POSTed form, keeping only known questions and options."""
    answers = {}
    for q in ALL_QUESTIONS:
        qid, qtype = q["id"], q["type"]
        if qtype == "quarters":
            answers[qid] = [form.get(f"{qid}__{i}", "").strip()[:12] for i in range(len(QUARTERS))]
        elif qtype == "checkboxes":
            answers[qid] = [v for v in form.getlist(qid) if v in q["options"]]
        elif qtype in ("select", "radio"):
            value = form.get(qid, "")
            answers[qid] = value if value in q["options"] else ""
        else:
            answers[qid] = form.get(qid, "").strip()[:MAX_TEXT]
        if q.get("has_other"):
            text = form.get(other_key(qid), "").strip()[:MAX_TEXT]
            answers[other_key(qid)] = text if _other_selected(q, answers[qid]) else ""
    return answers


def display_value(q, answers, quarter_labels):
    """Human-readable answer text ('' when unanswered)."""
    value = answers.get(q["id"])
    if q["type"] == "quarters":
        parts = [f"{label}: {raw}%" for label, raw in zip(quarter_labels, value or []) if raw not in ("", None)]
        return ", ".join(parts)
    if q.get("has_other") and value:
        other = answers.get(other_key(q["id"])) or ""
        spell = lambda v: f"{OTHER}: {other}" if v == OTHER and other else v  # noqa: E731
        value = [spell(v) for v in value] if isinstance(value, list) else spell(value)
    if isinstance(value, list):
        return "; ".join(value)
    return value or ""


def display_answers(answers, quarter_labels):
    """[(question label, answer text)] in form order, plus any keys from older questionnaires."""
    rows = [(q["label"], display_value(q, answers, quarter_labels)) for q in ALL_QUESTIONS]
    known = {q["id"] for q in ALL_QUESTIONS} | {other_key(q["id"]) for q in ALL_QUESTIONS}
    rows += [(key, str(value)) for key, value in answers.items() if key not in known]
    return rows


def diff_answers(old, new, quarter_labels):
    """[(question label, old text, new text)] for answers that changed between two versions."""
    changes = []
    for q in ALL_QUESTIONS:
        before = display_value(q, old, quarter_labels)
        after = display_value(q, new, quarter_labels)
        if before != after:
            changes.append((q["label"], before, after))
    return changes


def validate_complete(answers):
    """Drafts can be incomplete; submitting requires every required answer to be valid."""
    errors = {}
    for q in ALL_QUESTIONS:
        qid, value = q["id"], answers.get(q["id"])
        if q["type"] == "quarters":
            bad = []
            for label, raw in zip(QUARTERS, value or []):
                if raw == "":
                    if q.get("required"):
                        bad.append(f"{label} is missing")
                    continue
                try:
                    num = float(raw)
                except ValueError:
                    bad.append(f"{label} is not a number")
                    continue
                if not q["min"] <= num <= q["max"]:
                    bad.append(f"{label} must be between {q['min']}% and {q['max']}%")
            if bad:
                errors[qid] = "; ".join(bad) + "."
        elif q.get("required") and not value:
            errors[qid] = "Required to submit."
        elif q["type"] == "checkboxes" and q.get("exclusive") in (value or []) and len(value) > 1:
            errors[qid] = f"'{q['exclusive']}' can't be combined with other choices."
        elif _other_selected(q, value or "") and not answers.get(other_key(qid)):
            errors[qid] = "You picked 'Other'. Please describe it in the box."
    return errors
