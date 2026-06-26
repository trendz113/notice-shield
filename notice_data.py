"""
Static reference data for the free "I got a notice" flow. No AI involved —
this is fixed, fact-checked content per notice section, served as-is.
Field names match exactly what notice-shield.html's JS expects:
  section, label, deadlineNote, isWorrying, plain, actionSteps
"""

NOTICE_TYPES = [
    {
        "section": "143(1)",
        "label": "Intimation under Section 143(1)",
        "deadlineNote": "Respond within 30 days of the intimation date",
        "isWorrying": False,
        "plain": (
            "This is an automated check the department runs on every return — most people "
            "get one. It compares what you filed against your Form16/AIS and may show a "
            "small adjustment or refund. It does not mean you've done anything wrong."
        ),
        "actionSteps": [
            "Log in to the e-filing portal at incometax.gov.in",
            "Go to Pending Actions → e-Proceedings, and open this intimation",
            "Check the proposed adjustment line by line against your own records",
            "Select 'Agree' if it's correct, or 'Disagree' with a brief reason and supporting document if not",
            "Submit before the deadline shown on the notice itself",
        ],
    },
    {
        "section": "139(9)",
        "label": "Defective Return Notice — Section 139(9)",
        "deadlineNote": "Usually 15 days from the date on the notice",
        "isWorrying": True,
        "plain": (
            "The department found something incomplete or inconsistent in your return — "
            "a missing schedule, mismatched figures, or a required detail left blank. "
            "If you don't fix and resubmit in time, your original return can be treated as invalid."
        ),
        "actionSteps": [
            "Open the notice and read the specific defect described — it will name the exact issue",
            "Log in to the e-filing portal and go to 'e-File' → 'Rectification' or the defective-return response option shown for this notice",
            "Correct the specific defect in a revised return",
            "Submit the corrected return before the deadline stated on the notice",
            "Keep the acknowledgment number safe as proof of timely correction",
        ],
    },
    {
        "section": "143(2)",
        "label": "Scrutiny Notice — Section 143(2)",
        "deadlineNote": "Reply by the date shown in the e-Proceedings tab",
        "isWorrying": True,
        "plain": (
            "Your return has been picked for a closer review, either through risk parameters "
            "or random sampling. Being selected for scrutiny does not by itself mean you did "
            "anything wrong — it means the department wants more detail and documentation."
        ),
        "actionSteps": [
            "Log in to the e-filing portal and check the e-Proceedings tab for what's being asked",
            "Gather the documents the notice specifically requests — usually income proof, deduction proof, or bank statements",
            "Respond through e-Proceedings with your explanation and the supporting documents attached",
            "If the issue is complex or the amounts are large, this is the one notice type where getting a Chartered Accountant involved is genuinely worth it",
            "Track the deadline closely — scrutiny replies are usually not casually extended",
        ],
    },
    {
        "section": "148",
        "label": "Reassessment Notice — Section 148",
        "deadlineNote": "Per the date specified in the notice itself",
        "isWorrying": True,
        "plain": (
            "The department believes some income from an earlier year may not have been "
            "assessed properly. This can look back up to 3 years normally, or up to 10 years "
            "if there's evidence of income exceeding ₹50 lakh having escaped assessment."
        ),
        "actionSteps": [
            "Read the notice carefully for which assessment year it relates to and what income is in question",
            "File the return for that specific year if you haven't already, or respond explaining why the income was already accounted for",
            "Gather supporting documents for that year — bank statements, prior ITR, Form16/AIS for that period",
            "Given the stakes involved with reassessment, strongly consider having a Chartered Accountant review this with you before responding",
        ],
    },
    {
        "section": "156",
        "label": "Demand Notice — Section 156",
        "deadlineNote": "Usually 30 days from the date of the notice",
        "isWorrying": True,
        "plain": (
            "The department has determined you owe additional tax, interest, or penalty, "
            "and is asking you to pay it. This often follows another notice (like 143(1) "
            "or 143(2)) that already explained why."
        ),
        "actionSteps": [
            "Check what assessment or notice this demand is linked to, so you understand why it's being raised",
            "If you agree with the amount, pay it through the e-filing portal before the deadline to avoid further interest",
            "If you disagree, file a rectification request or response disputing the specific amount, with your reasoning",
            "Don't ignore this even if you plan to dispute it — file your disagreement formally rather than just not paying",
        ],
    },
]


def get_notice_types_list():
    """Returns the list shape /api/notice-types serves."""
    return [{"section": n["section"], "label": n["label"]} for n in NOTICE_TYPES]


def get_notice_detail(section: str):
    """Returns the full playbook for a given section, or None if not found."""
    for n in NOTICE_TYPES:
        if n["section"] == section:
            return n
    return None


RISK_BUCKETS = [
    {"id": "salary_mismatch", "question": "Does your Form16 salary figure match what's shown in your AIS?"},
    {"id": "interest_income", "question": "Have you included all bank interest (savings/FD/RD) shown in your AIS?"},
    {"id": "tds_mismatch", "question": "Does your TDS claim match what's reflected in Form 26AS / AIS?"},
    {"id": "capital_gains", "question": "If you sold any shares, mutual funds, or property, have you reported all of it correctly?"},
    {"id": "high_value_txn", "question": "Do any large transactions in your AIS (deposits, big purchases, investments) look out of line with your declared income?"},
    {"id": "gst_itr_gap", "question": "If you run a business, does your GST turnover roughly match what you declared in your ITR?"},
    {"id": "exemption_swap", "question": "If you filed a revised return, did you keep the same exemption claims as your original return?"},
    {"id": "stale_ais", "question": "Did you download your AIS fresh, close to when you filed (not weeks/months earlier)?"},
]
