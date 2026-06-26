# notice_data.py
# Deterministic reference data for Tax Notice Shield.
# This is the "source of truth" for facts (deadlines, section meanings).
# The LLM (Groq) is only used to phrase the explanation in plain English and
# to generate the draft letter — it never invents deadlines or legal facts.
# Verify these against incometax.gov.in before relying on them in production;
# dates/timelines can change with each Finance Act.

NOTICE_TYPES = {
    "143(1)": {
        "label": "Intimation after processing",
        "plain": "The tax department processed your return and is telling you the result — it could mean you owe more tax, you'll get a refund, or everything matched.",
        "deadline_days": 30,
        "deadline_note": "Usually 30 days from the date on the intimation. The exact date is printed on the notice itself — always check that first.",
        "is_worrying": False,
        "action_steps": [
            "Open the PDF of the intimation (downloaded from the e-filing portal, not just the SMS/email).",
            "Find the 'Income/Tax computed under section 143(1)' table — this shows what the department calculated vs what you filed.",
            "Compare every row against your own return, Form 16, and AIS.",
            "For each row that differs, go to Pending Actions → e-Proceedings on the portal and choose Agree or Disagree.",
            "If you agree, pay any demand shown within the deadline. If you disagree, attach your supporting document as proof when you select Disagree.",
        ],
    },
    "139(9)": {
        "label": "Defective return notice",
        "plain": "The department says your return has a mistake or missing piece — like the wrong ITR form, a missing schedule, or numbers that don't add up internally.",
        "deadline_days": 15,
        "deadline_note": "Typically 15 days from the date of the notice. If you miss this, your original return can be treated as if you never filed it.",
        "is_worrying": True,
        "action_steps": [
            "Read the defect description on the notice carefully — it names the exact problem.",
            "Log in to the e-filing portal → Pending Actions → e-Proceedings → find this notice.",
            "Fix the specific defect in your return (correct ITR form, missing schedule, etc.).",
            "Upload the corrected return as a fresh file before the deadline.",
            "Keep the acknowledgment number — this is your proof of timely correction.",
        ],
    },
    "143(2)": {
        "label": "Scrutiny notice",
        "plain": "Your return has been picked for a detailed check. This does not mean you've done anything wrong — it can also happen through random selection. The department wants you to prove your numbers with documents.",
        "deadline_days": None,
        "deadline_note": "The deadline is written on the notice itself and varies by case. Scrutiny notices must be issued within a fixed window after you filed, but your reply deadline is separate — check the notice.",
        "is_worrying": True,
        "action_steps": [
            "Do not panic — scrutiny selection can be random, not just suspicion-based.",
            "Note exactly which assessment year and which claims are being questioned.",
            "Gather every document that supports those specific claims (bank statements, rent receipts, sale deeds, investment proofs).",
            "Submit your written explanation plus documents through e-Proceedings before the deadline.",
            "If the amount involved is large or the issue is complex, get a Chartered Accountant to review your reply before submitting — this one is worth professional eyes.",
        ],
    },
    "148": {
        "label": "Reassessment notice (income may have escaped)",
        "plain": "The department believes some income from a past year was never taxed properly, and wants to reopen that year's assessment.",
        "deadline_days": None,
        "deadline_note": "Reply deadline is on the notice. Note: the department can reopen returns going back 3 years normally, or up to 10 years if the unreported amount is large.",
        "is_worrying": True,
        "action_steps": [
            "Read the 'reasons for reopening' the department must provide — you're entitled to see why.",
            "File the return for that year if asked, or respond with your explanation if you believe no income was missed.",
            "Gather full documentation for that specific year, not just the current one.",
            "Strongly consider a Chartered Accountant for this one — reassessment has real financial stakes and procedural steps that are easy to get wrong.",
            "Respond before the deadline; silence is treated as non-compliance, not agreement.",
        ],
    },
    "156": {
        "label": "Demand notice",
        "plain": "An assessment, reassessment, or correction has concluded, and you now owe a specific amount.",
        "deadline_days": 30,
        "deadline_note": "Usually 30 days from the date of the notice to pay, unless it says otherwise.",
        "is_worrying": True,
        "action_steps": [
            "Check the amount and the assessment year stated on the notice.",
            "If you agree, pay through the e-filing portal before the deadline to avoid further interest.",
            "If you disagree, you can file a rectification request or appeal — but you must act before the deadline either way.",
            "Keep the payment receipt or your rectification acknowledgment safely.",
        ],
    },
}

# Risk buckets used for the preventive checker (Entry A)
RISK_BUCKETS = [
    {
        "id": "salary_mismatch",
        "question": "Does the salary in your Form 16 match what your employer reported, with no bonus or correction left out?",
        "plain_risk": "If your employer reported a different salary figure to the tax department than what's on your Form 16, the system flags the gap automatically.",
        "fix_if_flagged": "Ask your employer (HR/payroll) for a corrected Form 16, or report the higher figure yourself in your return with an explanatory note.",
    },
    {
        "id": "interest_income",
        "question": "Did you include ALL bank interest in your return — savings account, FDs, RDs, and any tax refund interest?",
        "plain_risk": "Banks report every rupee of interest they pay you, even small amounts. If your return shows less than what the bank reported, it's an automatic flag.",
        "fix_if_flagged": "Add the missing interest income to your return. If you haven't filed yet, just include it. If you've already filed, you may need to file a revised return.",
    },
    {
        "id": "tds_credit",
        "question": "Does the TDS amount you've claimed match exactly what shows in your Form 26AS?",
        "plain_risk": "Claiming more TDS credit than what's recorded against your PAN in Form 26AS will not be allowed and can delay or block your refund.",
        "fix_if_flagged": "Use the figure from Form 26AS, not your own estimate. If your 26AS looks wrong, that's usually a reporting delay by the deductor — wait a few days and recheck before filing.",
    },
    {
        "id": "capital_gains",
        "question": "If you sold shares, mutual funds, or property this year, have you reported every sale and calculated the gain correctly?",
        "plain_risk": "Brokers, registrars, and mutual fund houses report your sale transactions directly. A missed sale or a wrong gain calculation is one of the most common notice triggers.",
        "fix_if_flagged": "Cross-check every transaction in your AIS against your own records. Recompute the gain carefully, especially if it involves an older purchase date.",
    },
    {
        "id": "high_value_spend",
        "question": "Do your big-ticket expenses this year (large cash deposits, credit card bills, property purchase, big investments, foreign remittance) roughly match the income you're declaring?",
        "plain_risk": "If your declared income looks too small to explain your spending or investments, the department may ask you to justify the source of funds — even if everything is perfectly legal.",
        "fix_if_flagged": "Keep a simple note ready explaining where the money came from for each large transaction — savings, a loan, a gift, sale of an asset — with proof if possible.",
    },
    {
        "id": "gst_turnover",
        "question": "(Skip if not applicable) If you run a business or are a professional with GST registration, does your ITR turnover roughly match your GST returns?",
        "plain_risk": "A large gap between GST turnover (GSTR-1/3B) and the turnover shown in your income tax return is checked automatically for business owners and professionals.",
        "fix_if_flagged": "Reconcile both figures before filing. If there's a genuine reason for the gap (like exempt supplies), keep that explanation documented.",
    },
    {
        "id": "exemption_swap",
        "question": "If you filed a revised or updated return, did you change which exemption or allowance you're claiming (for example, switching from HRA to a different allowance)?",
        "plain_risk": "Switching exemptions between your original and revised return, without genuinely qualifying for the new one, is a pattern the department is now specifically watching for.",
        "fix_if_flagged": "Only claim an exemption you actually qualify for under its own conditions. Keep the proof for whichever one you claim — rent receipts for HRA, or employer confirmation for other allowances.",
    },
    {
        "id": "stale_ais",
        "question": "Did you download your AIS recently (after May 31 of the filing year), or are you using an older copy?",
        "plain_risk": "AIS data keeps updating as banks and employers file late corrections. An old AIS may be missing information that's already visible to the department in the current version.",
        "fix_if_flagged": "Download a fresh AIS just before filing, not weeks in advance, and recheck it against your return one more time.",
    },
]
