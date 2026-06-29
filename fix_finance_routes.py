"""
fix_finance_routes.py

"Fix Your Finance Now" backend routes — built to sit on the same Flask
app as Notice Shield, in the same repo, reusing the exact same Razorpay
and Claude-calling functions already defined in server.py.

This file does NOT define its own Flask app, its own Razorpay client, or
its own Claude client. It expects to be wired into the existing server.py
by importing this module's route-registration function and calling it
once, passing in the already-existing app, razorpay_create_order,
RAZORPAY_KEY_ID, RAZORPAY_KEY_SECRET, and call_claude — see the bottom of
this file for the exact wiring snippet to add to server.py.

Routes added:
  POST /api/fix-finance/analyze         - free tier, pure calculation
  POST /api/fix-finance/create-order    - Razorpay order (reuses existing fn)
  POST /api/fix-finance/verify-payment  - Razorpay signature verification
  POST /api/fix-finance/report          - PAID. Claude-written report (JSON)
  POST /api/fix-finance/download-pdf    - PAID. Returns the PDF file

Security note, same as Notice Shield: report generation and PDF download
both require a verified payment_id that has been checked server-side via
/api/fix-finance/verify-payment. The frontend cannot unlock anything on
its own.

NOTE on storage: this reuses the same in-memory VERIFIED_PAYMENTS set
pattern as Notice Shield. As with that module, this resets on every
deploy/restart. Fine for now, same honest caveat applies — swap for a
small persistent store before this matters for real revenue continuity.
"""

import json
import hmac
import hashlib
import io
from flask import request, jsonify, send_file

from fix_finance_calc import build_calculated_data
from fix_finance_pdf import build_fix_finance_pdf


# In-memory store of verified payments for THIS product, kept separate
# from Notice Shield's VERIFIED_PAYMENTS set even though both live in the
# same process — avoids any chance of a payment_id collision between
# products being misread as "verified for both".
FIX_FINANCE_VERIFIED_PAYMENTS = set()

# Cache of completed reports, keyed by payment_id, so a repeat PDF
# download or repeat /report call doesn't trigger another Claude API
# call. Same cost-discipline principle as the rest of your products.
FIX_FINANCE_REPORT_CACHE = {}


# ---------------------------------------------------------------------------
# Claude prompt — kept in this file rather than a separate prompts module,
# to match the flat, single-file style of your existing server.py rather
# than introducing new structure you didn't ask for.
# ---------------------------------------------------------------------------

FIX_FINANCE_SYSTEM_PROMPT = """You are writing a financial report for an Indian salaried person who is stressed about money. They have already paid for this report, so it must feel worth it — but it must stay simple.

RULES YOU MUST FOLLOW:
1. Use short sentences. Avoid words like "leverage", "optimize", "portfolio diversification", "liquidity". Write the way you'd explain it to a friend, not a banker.
2. Never invent numbers. Only use the numbers given to you in the data below. If a number isn't given, don't guess it.
3. No legal section numbers (like "Section 13(2)" or "SARFAESI") unless the user's situation is already serious (90+ days overdue or NPA flagged true). For everyone else, just say "the bank can take your house" or "the bank can mark you as a defaulter" in plain words.
4. Every scary fact must be followed by one clear action in the very next sentence. Never leave a scary fact hanging without a fix.
5. Use Rs. for all amounts exactly as given to you — do not reformat or recalculate them. Round to the nearest hundred or thousand if needed — don't write paise-level precision.
6. Keep each section under 120 words.
7. Address the reader as "you". Don't refer to them in third person.
8. Do not add disclaimers like "consult a financial advisor" — the report template already includes one.
9. Output valid JSON matching the schema given. No markdown, no commentary outside the JSON, no markdown code fences.
"""

FIX_FINANCE_USER_PROMPT_TEMPLATE = """
Here is this person's calculated financial data. All numbers below are already computed — just explain them simply.

INCOME & DEBT:
- Monthly take-home income: Rs. {monthly_income}
- Total monthly EMI + minimum due payments: Rs. {total_monthly_debt_payment}
- Debt-to-income ratio: {debt_to_income_pct}%
- Danger zone (above 50%): {is_debt_ratio_dangerous}

EMERGENCY FUND:
- Current savings + liquid assets: Rs. {liquid_savings}
- Monthly essential expenses: Rs. {monthly_essential_expenses}
- Months of safety cover: {emergency_fund_months}
- Months needed to reach a safe 6-month fund: {months_to_build_6mo_fund}

CREDIT CARD (skip this paragraph entirely if outstanding is 0):
- Outstanding balance: Rs. {cc_outstanding}
- Monthly interest rate: {cc_monthly_rate}%
- Years to clear at minimum-due-only pace: {cc_years_to_clear}
- Total interest paid over that time: Rs. {cc_total_interest}
- Never clears at this pace: {cc_never_clears}
- Monthly payment needed to clear in 2 years instead: Rs. {cc_accelerated_payment}

HOME LOAN (skip this paragraph entirely if outstanding is 0):
- Outstanding loan amount: Rs. {home_loan_outstanding}
- Monthly EMI: Rs. {home_loan_emi}
- Already repaid 80%+ of original loan: {repaid_over_80_pct}
- SARFAESI auction risk applies to this loan: {sarfaesi_applies}
- Currently overdue: {is_overdue}
- Days overdue: {days_overdue}
- Current stage: {home_loan_stage}
- Months of EMI their current savings could cover if income stopped: {months_before_default_risk}

CIBIL / CREDIT SCORE RISK:
- Estimated score drop for a minor late payment: {cibil_minor_drop_range}
- Estimated score drop for a major default (90+ days): {cibil_major_drop_range}
- Months for score to start recovering with clean payments: {cibil_recovery_months}
- Months for fuller recovery: {cibil_full_recovery_months}
- Years this stays visible on record: {cibil_record_years}

DEBT PAYOFF ORDER (already calculated, highest interest first):
{debt_payoff_order_list}

90-DAY TARGETS (already calculated, do not recalculate, just narrate clearly):
- Month 1: {month_1_target}
- Month 2: {month_2_target}
- Month 3: {month_3_target}

Now write the report in this exact JSON structure:

{{
  "headline": "One sentence, the single biggest truth about their finances. Direct, not dramatic.",
  "whats_wrong": ["problem 1 in plain words", "problem 2", "problem 3 (max 3, ranked by danger)"],
  "what_happens_if_you_dont_fix_this": {{
    "credit_card": "plain paragraph using the cc_ numbers, OMIT this key entirely if cc_outstanding is 0",
    "home_loan": "plain paragraph using the home_loan_ and cibil_ numbers, OMIT this key entirely if home_loan_outstanding is 0",
    "cibil_general": "plain paragraph on what a damaged score costs them in future, even if no loan is currently at risk"
  }},
  "your_way_out": ["step 1, specific to their numbers", "step 2", "step 3"],
  "ninety_day_plan": {{
    "month_1": "narrate the month_1_target above in plain words",
    "month_2": "narrate the month_2_target above in plain words",
    "month_3": "narrate the month_3_target above in plain words"
  }},
  "your_safety_number": "one paragraph stating emergency_fund_months and one concrete way to grow it"
}}
"""


def build_fix_finance_report_prompt(calculated_data: dict) -> str:
    """Fills the template — mirrors build_report_prompt() style already
    used in server.py for Notice Shield."""
    # debt_payoff_order_list is a list of dicts; render it as readable
    # text for the prompt rather than raw JSON.
    payoff_list = calculated_data.get("debt_payoff_order_list", [])
    payoff_text = "\n".join(
        f"- {d['name']}: {d['priority_reason']}" for d in payoff_list
    ) or "No debts flagged."

    safe_data = dict(calculated_data)
    safe_data["debt_payoff_order_list"] = payoff_text

    return FIX_FINANCE_USER_PROMPT_TEMPLATE.format(**safe_data)


# ---------------------------------------------------------------------------
# Route registration function — call this once from server.py
# ---------------------------------------------------------------------------

def register_fix_finance_routes(app, razorpay_create_order, call_claude,
                                  RAZORPAY_KEY_ID, RAZORPAY_KEY_SECRET,
                                  razorpay_configured):
    """
    Wires all Fix Finance routes onto the existing Flask app.

    Call this from server.py, after all the existing functions/constants
    it depends on are already defined — see the wiring snippet at the
    bottom of this file for the exact lines to add.
    """

    @app.route("/api/fix-finance/analyze", methods=["POST"])
    def fix_finance_analyze():
        """
        Free tier. Pure calculation, no AI call, no payment needed.
        Expects JSON matching FinanceInput's field names (see
        fix_finance_calc.py) — monthly_income, monthly_essential_expenses,
        total_emi, home_loan_outstanding, home_loan_emi,
        home_loan_repaid_pct, home_loan_days_overdue, cc_outstanding,
        cc_monthly_rate_pct, cc_min_due_pct, liquid_savings,
        other_monthly_debt. Missing fields default to 0.
        """
        payload = request.get_json(force=True) or {}

        if not payload.get("monthly_income"):
            return jsonify({"error": "Monthly income is required."}), 400

        try:
            calculated = build_calculated_data(payload)
        except Exception as e:
            return jsonify({"error": f"Could not calculate your numbers: {str(e)}"}), 400

        return jsonify(calculated)

    @app.route("/api/fix-finance/create-order", methods=["POST"])
    def fix_finance_create_order():
        if not razorpay_configured:
            return jsonify({"error": "Payment is not configured on this server."}), 500

        data = request.get_json(force=True) or {}
        amount = data.get("amount", 19900)  # paise; 19900 = Rs.199
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

    @app.route("/api/fix-finance/verify-payment", methods=["POST"])
    def fix_finance_verify_payment():
        if not RAZORPAY_KEY_SECRET:
            return jsonify({"error": "Payment is not configured on this server."}), 500

        data = request.get_json(force=True) or {}
        order_id = data.get("razorpay_order_id")
        payment_id = data.get("razorpay_payment_id")
        signature = data.get("razorpay_signature")

        if not all([order_id, payment_id, signature]):
            return jsonify({"error": "Missing payment verification fields."}), 400

        body = f"{order_id}|{payment_id}"
        expected_signature = hmac.new(
            RAZORPAY_KEY_SECRET.encode(), body.encode(), hashlib.sha256
        ).hexdigest()

        if not hmac.compare_digest(expected_signature, signature):
            return jsonify({"error": "Payment signature did not match."}), 400

        FIX_FINANCE_VERIFIED_PAYMENTS.add(payment_id)
        return jsonify({"status": "verified", "payment_id": payment_id})

    def _get_or_generate_report(payload: dict, payment_id: str) -> dict:
        """Shared by /report and /download-pdf so a repeat call to either
        never triggers a second Claude API call for the same payment_id."""
        if payment_id in FIX_FINANCE_REPORT_CACHE:
            return FIX_FINANCE_REPORT_CACHE[payment_id]

        calculated = build_calculated_data(payload)
        prompt = build_fix_finance_report_prompt(calculated)
        raw_text = call_claude(prompt, max_tokens=1500)

        cleaned = raw_text.replace("```json", "").replace("```", "").strip()
        report_json = json.loads(cleaned)

        result = {"calculated": calculated, "report": report_json}
        FIX_FINANCE_REPORT_CACHE[payment_id] = result
        return result

    @app.route("/api/fix-finance/report", methods=["POST"])
    def fix_finance_report():
        """
        PAID. Expects JSON: { ...finance fields..., payment_id }
        Returns the calculated numbers + Claude's plain-English report,
        as JSON — the frontend can render this directly, and the same
        payment_id can also be used to fetch the PDF via /download-pdf.
        """
        data = request.get_json(force=True) or {}
        payment_id = data.get("payment_id")

        if payment_id not in FIX_FINANCE_VERIFIED_PAYMENTS:
            return jsonify({"error": "Payment not verified. Please complete payment first."}), 402

        try:
            result = _get_or_generate_report(data, payment_id)
        except json.JSONDecodeError:
            return jsonify({"error": "Could not parse the generated report. Please try again."}), 502
        except Exception as e:
            return jsonify({"error": f"Could not generate report: {str(e)}"}), 502

        return jsonify(result)

    @app.route("/api/fix-finance/download-pdf", methods=["POST"])
    def fix_finance_download_pdf():
        """
        PAID. Same input shape as /report. Returns the actual PDF file.
        If /report was already called for this payment_id, reuses the
        cached report instead of calling Claude again.
        """
        data = request.get_json(force=True) or {}
        payment_id = data.get("payment_id")
        user_name = data.get("name", "")

        if payment_id not in FIX_FINANCE_VERIFIED_PAYMENTS:
            return jsonify({"error": "Payment not verified."}), 402

        try:
            result = _get_or_generate_report(data, payment_id)
        except json.JSONDecodeError:
            return jsonify({"error": "Could not parse the generated report. Please try again."}), 502
        except Exception as e:
            return jsonify({"error": f"Could not generate report: {str(e)}"}), 502

        pdf_buf = build_fix_finance_pdf(
            result["calculated"], result["report"], user_name=user_name
        )

        return send_file(
            pdf_buf,
            mimetype="application/pdf",
            as_attachment=True,
            download_name="fix-your-finance-now-report.pdf",
        )


# ---------------------------------------------------------------------------
# WIRING SNIPPET — add these lines to server.py
# ---------------------------------------------------------------------------
#
# 1. Near the top, alongside your other imports:
#
#       from fix_finance_routes import register_fix_finance_routes
#
# 2. After razorpay_configured is defined (it already exists in your
#    server.py, right after razorpay_create_order), add this one call:
#
#       register_fix_finance_routes(
#           app,
#           razorpay_create_order,
#           call_claude,
#           RAZORPAY_KEY_ID,
#           RAZORPAY_KEY_SECRET,
#           razorpay_configured,
#       )
#
# That's it — no other changes to server.py are needed. This file does
# not touch any existing Notice Shield route, variable, or import.
