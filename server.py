# server.py
# Tax Notice Shield backend — standalone Flask service for Railway.
# Deliberately a SEPARATE service from your main salarybit server_railway.py,
# so a bug here can never crash Tax AI / Insurance Mitra / other live tools.
#
# Two main flows:
#   POST /api/risk-check   -> Entry A: preventive checker, guided answers in
#   POST /api/notice-help  -> Entry B: user already has a notice
#
# Uses GROQ_API_KEY (same provider as your other tools), not Anthropic.

import os
import json
import traceback
from flask import Flask, request, jsonify
from flask_cors import CORS
from groq import Groq

from notice_data import NOTICE_TYPES, RISK_BUCKETS

app = Flask(__name__)
CORS(app)

GROQ_API_KEY = os.environ.get("GROQ_API_KEY")
GROQ_MODEL = "openai/gpt-oss-120b"  # current Groq model as of June 2026;
# llama-3.3-70b-versatile and llama-3.1-8b-instant were deprecated June 17 2026 —
# check https://console.groq.com/docs/models if this stops working later.

groq_client = Groq(api_key=GROQ_API_KEY) if GROQ_API_KEY else None


# ---------- Deterministic scoring (no AI here — this must be reliable) ----------

def score_risk_answers(answers):
    """answers: { bucket_id: 'yes_ok' | 'flagged' | 'not_applicable' }"""
    bucket_by_id = {b["id"]: b for b in RISK_BUCKETS}
    flagged, clear, skipped = [], [], []

    for bucket in RISK_BUCKETS:
        answer = answers.get(bucket["id"])
        if answer == "flagged":
            flagged.append(bucket)
        elif answer == "not_applicable":
            skipped.append(bucket)
        else:
            clear.append(bucket)

    if len(flagged) == 0:
        verdict = "green"
    elif len(flagged) <= 2:
        verdict = "amber"
    else:
        verdict = "red"

    return flagged, clear, skipped, verdict


# ---------- Prompt builders ----------

def build_risk_report_prompt(flagged, verdict):
    if flagged:
        flagged_text = "\n".join(
            f"- {b['id']}: {b['plain_risk']} Fix: {b['fix_if_flagged']}" for b in flagged
        )
    else:
        flagged_text = "None — no issues flagged."

    return f"""You are writing a plain-English tax report for an ordinary Indian salaried taxpayer who is NOT a tax expert and may be anxious about getting an income tax notice. Use simple words, short sentences, no legal jargon unless you explain it immediately in brackets. Do not invent any numbers, deadlines, or section references beyond what is given below — only use the facts provided.

Overall risk level: {verdict} (green = low risk, amber = some issues to fix, red = high risk of notice)

Flagged issues with their facts:
{flagged_text}

Write a report with these parts, in this order:
1. One-line verdict in plain English (max 20 words).
2. For each flagged issue: a short heading, then 2-3 sentences explaining what's wrong using only the facts given, in everyday language a non-expert would understand.
3. A short numbered action list (max 6 steps) of what to do this week, ordered by urgency.
4. One closing sentence of reassurance that is honest (do not promise no notice will come; do not minimize real issues either).

Do not use the words "discrepancy," "non-compliance," or other bureaucratic terms — say what actually happened instead (e.g. "your bank reported interest you didn't include" not "income discrepancy detected"). Output only the report text, no preamble."""


def build_notice_letter_prompt(notice_section, user_facts):
    notice_info = NOTICE_TYPES.get(notice_section)
    if not notice_info:
        raise ValueError(f"Unknown notice section: {notice_section}")

    return f"""You are drafting a formal but plain-language response letter for an Indian taxpayer to submit through the Income Tax e-Proceedings portal, replying to a notice under Section {notice_section} ({notice_info['label']}).

What this notice means: {notice_info['plain']}

Facts the taxpayer has given about their specific situation:
{json.dumps(user_facts, indent=2)}

Write a formal response letter that:
- Addresses the Assessing Officer respectfully and references the notice section and the taxpayer's PAN/assessment year as placeholders the user must fill in (write them as [PAN], [Assessment Year], [Notice Date], [DIN/Notice Number] — do not invent these).
- States clearly what the taxpayer is responding to.
- Explains their position using ONLY the facts given above — do not invent any figures, dates, or documents not mentioned.
- Lists the supporting documents they are attaching, based on what they told you.
- Closes formally.
- Uses correct but plain language — this should be readable by the taxpayer too, not just official jargon.

Output only the letter text, ready to copy into a PDF, no preamble or explanation."""


# ---------- Groq call ----------

def call_groq(prompt):
    if groq_client is None:
        raise RuntimeError("GROQ_API_KEY is not set on this service.")
    completion = groq_client.chat.completions.create(
        model=GROQ_MODEL,
        max_tokens=1500,
        messages=[{"role": "user", "content": prompt}],
    )
    return completion.choices[0].message.content or ""


# ---------- Routes ----------

@app.route("/api/risk-check", methods=["POST"])
def risk_check():
    try:
        body = request.get_json(silent=True) or {}
        answers = body.get("answers")
        if not isinstance(answers, dict):
            return jsonify({"error": "Missing answers object."}), 400

        flagged, clear, skipped, verdict = score_risk_answers(answers)
        prompt = build_risk_report_prompt(flagged, verdict)
        report_text = call_groq(prompt)

        return jsonify({
            "verdict": verdict,
            "flaggedCount": len(flagged),
            "flaggedIds": [b["id"] for b in flagged],
            "report": report_text,
        })
    except Exception as e:
        print("risk-check error:", e)
        traceback.print_exc()
        return jsonify({"error": "Could not generate risk report. Please try again."}), 500


@app.route("/api/notice-help", methods=["POST"])
def notice_help():
    try:
        body = request.get_json(silent=True) or {}
        notice_section = body.get("noticeSection")
        user_facts = body.get("userFacts")

        notice_info = NOTICE_TYPES.get(notice_section)
        if not notice_info:
            return jsonify({
                "error": f'Unrecognized notice section "{notice_section}". '
                         f'Supported: {", ".join(NOTICE_TYPES.keys())}'
            }), 400

        letter = None
        if user_facts:
            letter_prompt = build_notice_letter_prompt(notice_section, user_facts)
            letter = call_groq(letter_prompt)

        return jsonify({
            "section": notice_section,
            "label": notice_info["label"],
            "plain": notice_info["plain"],
            "deadlineNote": notice_info["deadline_note"],
            "isWorrying": notice_info["is_worrying"],
            "actionSteps": notice_info["action_steps"],
            "letter": letter,
        })
    except Exception as e:
        print("notice-help error:", e)
        traceback.print_exc()
        return jsonify({"error": "Could not process this request. Please try again."}), 500


@app.route("/api/risk-buckets", methods=["GET"])
def risk_buckets():
    return jsonify([{"id": b["id"], "question": b["question"]} for b in RISK_BUCKETS])


@app.route("/api/notice-types", methods=["GET"])
def notice_types():
    return jsonify([
        {"section": section, "label": info["label"]}
        for section, info in NOTICE_TYPES.items()
    ])


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"service": "Notice Shield API", "status": "ok"})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 3000))
    app.run(host="0.0.0.0", port=port)
