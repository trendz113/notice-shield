"""
Notice Shield backend.
Routes:
  GET  /health
  POST /api/extract        - upload Form16 + AIS PDFs, returns extracted fields w/ confidence
  POST /api/analyze        - takes confirmed fields, returns deterministic risk analysis
  POST /api/create-order   - creates a Razorpay order server-side (never exposes key secret)
  POST /api/verify-payment - verifies Razorpay signature server-side before unlocking anything
  POST /api/report         - PAID. Takes confirmed data + verified payment ref, returns
                              Claude-written plain-English report + letter
  GET  /api/download-excel - PAID. Returns the Excel workbook for a completed analysis

Security note: report generation and Excel download both require a verified
payment_id that has been checked server-side via /api/verify-payment. The
frontend can be fully open-source and inspected; it cannot unlock anything
on its own.
"""
import os
import json
import hmac
import hashlib
import base64
import urllib.request
import urllib.error
from flask import Flask, request, jsonify, send_file
from flask_cors import CORS
import io

from extraction import decrypt_ais_pdf, extract_form16, extract_ais
from analysis import analyze
from excel_report import build_workbook
from notice_data import get_notice_types_list, get_notice_detail, RISK_BUCKETS

app = Flask(__name__)
CORS(app, resources={r"/api/*": {"origins": "*"}}, supports_credentials=False)

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
RAZORPAY_KEY_ID = os.environ.get("RAZORPAY_KEY_ID")
RAZORPAY_KEY_SECRET = os.environ.get("RAZORPAY_KEY_SECRET")


def razorpay_create_order(amount: int, currency: str = "INR") -> dict:
    """
    Calls Razorpay's REST API directly via urllib instead of the razorpay
    SDK, so there's no extra package for Railway's build to fail to install
    (the same failure mode that hit the 'requests' library earlier).
    Auth is HTTP Basic with key_id:key_secret — identical to what the SDK
    does internally, just without the dependency.
    """
    auth = base64.b64encode(f"{RAZORPAY_KEY_ID}:{RAZORPAY_KEY_SECRET}".encode()).decode()
    body = json.dumps({
        "amount": amount,
        "currency": currency,
        "payment_capture": 1,
    }).encode()
    req = urllib.request.Request(
        "https://api.razorpay.com/v1/orders",
        data=body,
        headers={"Content-Type": "application/json", "Authorization": f"Basic {auth}"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"Razorpay API error: {e.read().decode()}")


razorpay_configured = bool(RAZORPAY_KEY_ID and RAZORPAY_KEY_SECRET)

# In-memory store of verified payments for this process.
# NOTE: this resets on every deploy/restart. For production durability
# beyond a single process lifetime, swap this for a small persistent
# store (e.g. a Railway-attached Postgres/SQLite) before relying on it
# for real revenue — flagging this honestly rather than pretending an
# in-memory dict is durable storage.
VERIFIED_PAYMENTS = set()


@app.route("/health")
def health():
    return jsonify({"service": "Notice Shield API", "status": "ok"})


# ---------------------------------------------------------------------------
# Free tier: guided notice playbook + quick risk check.
# These power the live notice-shield.html page (no payment, no PDF upload —
# the fast, free entry point for someone who just got a notice or is
# generally worried). Kept on the same Flask app as the paid upload-based
# tool below; they don't share any state or routes with each other.
# ---------------------------------------------------------------------------

@app.route("/api/notice-types", methods=["GET"])
def api_notice_types():
    return jsonify(get_notice_types_list())


def build_letter_from_facts_prompt(notice_entry: dict, user_facts: str) -> str:
    return f"""Draft a short, plain-language response letter for the Income Tax
e-Proceedings portal, addressing a {notice_entry['section']} notice
({notice_entry['label']}).

The person describes their situation as follows — use ONLY these facts,
never invent dates, amounts, or documents not mentioned here:
"{user_facts}"

Format as a brief formal letter: salutation, 2-3 short paragraphs explaining
their position factually based only on what they described, and a closing
line offering to submit supporting documents if required."""


@app.route("/api/notice-help", methods=["POST"])
def api_notice_help():
    data = request.get_json(force=True) or {}
    section = data.get("noticeSection")
    user_facts = data.get("userFacts")

    if not section:
        return jsonify({"error": "Please select a notice section."}), 400

    entry = get_notice_detail(section)
    if not entry:
        return jsonify({"error": f"'{section}' isn't a section we recognize. Double-check the number on your notice."}), 404

    response = {
        "label": entry["label"],
        "deadlineNote": entry["deadlineNote"],
        "isWorrying": entry["isWorrying"],
        "plain": entry["plain"],
        "actionSteps": entry["actionSteps"],
    }

    # Letter drafting is a separate, optional second call from the same
    # endpoint (the frontend re-calls this route with userFacts filled in).
    if user_facts and user_facts.get("description"):
        try:
            letter = call_claude(build_letter_from_facts_prompt(entry, user_facts["description"]))
            response["letter"] = letter
        except Exception as e:
            return jsonify({"error": f"Could not draft the letter: {str(e)}"}), 502

    return jsonify(response)


@app.route("/api/risk-buckets", methods=["GET"])
def api_risk_buckets():
    return jsonify(RISK_BUCKETS)


def build_quick_risk_prompt(flagged_questions: list[str]) -> str:
    flags_text = "\n".join(f"- {q}" for q in flagged_questions) or "Nothing was flagged."
    return f"""You are writing a short, plain-English tax notice risk summary for an
Indian salaried taxpayer, based only on a quick self-assessment quiz (not
actual document data). Use only the facts given below — never invent
specific numbers, since none were provided.

The person flagged these areas as "no" or "not sure":
{flags_text}

Write a short summary (under 200 words):
1. One-sentence honest verdict in plain English
2. For each flagged area, one or two plain sentences on why it matters and
   what document or fix to look into
3. If nothing was flagged, reassure them briefly and clearly
4. End with one line suggesting that for a precise, document-based check
   (comparing their actual Form16 and AIS line by line), the full Notice
   Shield report tool can do that — without being pushy about it

Do not use the word "taxpayer" — say "you". No legal jargon."""


@app.route("/api/risk-check", methods=["POST"])
def api_risk_check():
    data = request.get_json(force=True) or {}
    answers = data.get("answers", {})

    bucket_lookup = {b["id"]: b["question"] for b in RISK_BUCKETS}
    flagged = [bucket_lookup[k] for k, v in answers.items() if v == "flagged" and k in bucket_lookup]

    if not flagged:
        verdict = "green"
    elif len(flagged) <= 2:
        verdict = "amber"
    else:
        verdict = "red"

    try:
        report_text = call_claude(build_quick_risk_prompt(flagged), max_tokens=500)
    except Exception as e:
        return jsonify({"error": f"Could not generate report: {str(e)}"}), 502

    return jsonify({"verdict": verdict, "report": report_text})


@app.route("/api/extract", methods=["POST"])
def api_extract():
    """
    Expects multipart/form-data with:
      form16: PDF file
      ais: PDF file
      pan: string
      dob_ddmmyyyy: string (for AIS password)
    """
    form16_file = request.files.get("form16")
    ais_file = request.files.get("ais")
    pan = request.form.get("pan", "")
    dob = request.form.get("dob_ddmmyyyy", "")

    if not form16_file or not ais_file:
        return jsonify({"error": "Both Form16 and AIS PDFs are required."}), 400
    if not pan or not dob:
        return jsonify({"error": "PAN and date of birth are required to unlock the AIS PDF."}), 400

    result = {"form16": {}, "ais": {}, "errors": []}

    try:
        result["form16"] = extract_form16(io.BytesIO(form16_file.read()))
    except Exception as e:
        result["errors"].append(f"Form16 extraction issue: {str(e)}")

    try:
        ais_bytes = io.BytesIO(ais_file.read())
        reader = decrypt_ais_pdf(ais_bytes, pan=pan, dob_ddmmyyyy=dob)
        from pypdf import PdfWriter
        writer = PdfWriter()
        for page in reader.pages:
            writer.add_page(page)
        buf = io.BytesIO()
        writer.write(buf)
        buf.seek(0)
        result["ais"] = extract_ais(buf)
    except ValueError as e:
        result["errors"].append(str(e))
    except Exception as e:
        result["errors"].append(f"AIS extraction issue: {str(e)}")

    return jsonify(result)


@app.route("/api/analyze", methods=["POST"])
def api_analyze():
    """
    Expects JSON: { form16: {...}, ais: {...}, extra: {...} }
    All values should already be user-confirmed at this point.
    Returns the deterministic analysis only — no AI call, no payment needed,
    since this is what populates the locked preview (verdict + flag count
    shown blurred, without the actual fix text, which stays behind payment).
    """
    data = request.get_json(force=True) or {}
    form16 = data.get("form16", {})
    ais = data.get("ais", {})
    extra = data.get("extra", {})

    result = analyze(form16, ais, extra)
    return jsonify(result)


@app.route("/api/create-order", methods=["POST"])
def api_create_order():
    if not razorpay_configured:
        return jsonify({"error": "Payment is not configured on this server."}), 500

    data = request.get_json(force=True) or {}
    amount = data.get("amount", 14900)  # paise; 14900 = Rs.149
    currency = data.get("currency", "INR")

    try:
        order = razorpay_create_order(amount, currency)
    except Exception as e:
        return jsonify({"error": f"Could not create order: {str(e)}"}), 500

    return jsonify({
        "order_id": order["id"],
        "amount": order["amount"],
        "currency": order["currency"],
        "key_id": RAZORPAY_KEY_ID,
    })


@app.route("/api/verify-payment", methods=["POST"])
def api_verify_payment():
    if not RAZORPAY_KEY_SECRET:
        return jsonify({"error": "Payment is not configured on this server."}), 500

    data = request.get_json(force=True) or {}
    order_id = data.get("razorpay_order_id")
    payment_id = data.get("razorpay_payment_id")
    signature = data.get("razorpay_signature")

    if not all([order_id, payment_id, signature]):
        return jsonify({"error": "Missing payment verification fields."}), 400

    # Verify signature ourselves (HMAC SHA256 of "order_id|payment_id" using
    # the key secret) rather than only trusting the SDK helper, so the logic
    # is plain and auditable.
    body = f"{order_id}|{payment_id}"
    expected_signature = hmac.new(
        RAZORPAY_KEY_SECRET.encode(), body.encode(), hashlib.sha256
    ).hexdigest()

    if not hmac.compare_digest(expected_signature, signature):
        return jsonify({"error": "Payment signature did not match."}), 400

    VERIFIED_PAYMENTS.add(payment_id)
    return jsonify({"status": "verified", "payment_id": payment_id})


def call_claude(prompt: str, max_tokens: int = 1500) -> str:
    if not ANTHROPIC_API_KEY:
        raise RuntimeError("ANTHROPIC_API_KEY not configured on this server.")

    body = json.dumps({
        "model": "claude-sonnet-4-6",
        "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": prompt}],
    }).encode()

    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=body,
        headers={
            "Content-Type": "application/json",
            "x-api-key": ANTHROPIC_API_KEY,
            "anthropic-version": "2023-06-01",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        result = json.loads(resp.read())
    return "".join(block.get("text", "") for block in result.get("content", []))


def build_report_prompt(analysis_result: dict, taxpayer_name: str) -> str:
    flags_text = "\n".join(
        f"- [{f['severity'].upper()}] {f['bucket']}: {f['detail']}"
        for f in analysis_result.get("flags", [])
    ) or "No issues were found."

    return f"""You are writing a plain-English tax notice risk report for an Indian
salaried taxpayer named {taxpayer_name}. Use only the facts given below —
never invent numbers, dates, or sections not present here.

Verdict: {analysis_result['verdict']}
Flagged issues:
{flags_text}

Write a short report with:
1. One-sentence verdict in plain English (no jargon)
2. For each flagged issue: what it means in one or two plain sentences, and
   exactly what to do about it (concrete next action)
3. If no issues, reassure them clearly and briefly

Keep total length under 350 words. No legal jargon. Write for someone who
has never dealt with the income tax department before. Do not use the word
"taxpayer" — say "you"."""


def build_letter_prompt(analysis_result: dict, taxpayer_name: str, pan: str) -> str:
    flags_text = "\n".join(
        f"- {f['detail']}" for f in analysis_result.get("flags", [])
    ) or "No discrepancies were found."

    return f"""Draft a formal but plain-language response letter for the Income Tax
e-Proceedings portal, from {taxpayer_name} (PAN: {pan}), addressing these
specific points only — do not invent any facts beyond what's given:

{flags_text}

Format as a short formal letter: salutation, 2-3 short paragraphs explaining
the position on each point factually, and a closing line offering to submit
supporting documents if required. Do not fabricate document references,
dates, or section numbers not implied by the facts above."""


@app.route("/api/report", methods=["POST"])
def api_report():
    data = request.get_json(force=True) or {}
    payment_id = data.get("payment_id")

    if payment_id not in VERIFIED_PAYMENTS:
        return jsonify({"error": "Payment not verified. Please complete payment first."}), 402

    form16 = data.get("form16", {})
    ais = data.get("ais", {})
    extra = data.get("extra", {})
    taxpayer_name = data.get("taxpayer_name", "")
    pan = data.get("pan", "")

    analysis_result = analyze(form16, ais, extra)

    try:
        report_text = call_claude(build_report_prompt(analysis_result, taxpayer_name))
    except Exception as e:
        return jsonify({"error": f"Could not generate report: {str(e)}"}), 502

    letter_text = ""
    if analysis_result["flags"]:
        try:
            letter_text = call_claude(build_letter_prompt(analysis_result, taxpayer_name, pan))
        except Exception as e:
            letter_text = f"(Letter generation failed: {str(e)}. Your risk report above is still valid.)"

    return jsonify({
        "verdict": analysis_result["verdict"],
        "flags": analysis_result["flags"],
        "report": report_text,
        "letter": letter_text,
    })


@app.route("/api/download-excel", methods=["POST"])
def api_download_excel():
    data = request.get_json(force=True) or {}
    payment_id = data.get("payment_id")

    if payment_id not in VERIFIED_PAYMENTS:
        return jsonify({"error": "Payment not verified."}), 402

    form16 = data.get("form16", {})
    ais = data.get("ais", {})
    extra = data.get("extra", {})
    taxpayer_name = data.get("taxpayer_name", "")
    pan = data.get("pan", "")

    analysis_result = analyze(form16, ais, extra)
    wb = build_workbook(analysis_result, taxpayer_name=taxpayer_name, pan=pan)

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return send_file(
        buf,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        as_attachment=True,
        download_name="notice-shield-report.xlsx",
    )


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
