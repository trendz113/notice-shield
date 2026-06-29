"""
fix_finance_calc.py

Pure calculation engine for the "Fix Your Finance Now" product.
No AI calls happen here — every number the report uses is computed
deterministically in this file. The Claude API is only used later,
in the report-narration step, to explain these already-computed
numbers in plain language.

Drop this into your shared backend, e.g.:
    /engine/fix_finance_calc.py

Usage (typical Flask route):

    from engine.fix_finance_calc import build_calculated_data

    @app.route("/api/fix-finance/analyze", methods=["POST"])
    def analyze():
        payload = request.get_json()
        data = build_calculated_data(payload)
        return jsonify(data)

The same `build_calculated_data()` output dict is what you pass into
the Claude report-generation prompt after payment is confirmed.
"""

from dataclasses import dataclass, field
from typing import Optional


# ---------------------------------------------------------------------------
# Input shape
# ---------------------------------------------------------------------------

@dataclass
class FinanceInput:
    # Income
    monthly_income: float                  # take-home, all sources combined

    # Expenses
    monthly_essential_expenses: float       # rent/groceries/school fees/etc, EXCLUDING EMIs and CC min-due

    # Debts — EMIs (home/personal/car/bike/gold/education loans combined)
    total_emi: float = 0.0

    # Home loan specific (optional — only if they have one)
    home_loan_outstanding: float = 0.0
    home_loan_emi: float = 0.0
    home_loan_repaid_pct: float = 0.0       # what % of original principal has been repaid
    home_loan_days_overdue: int = 0         # 0 if current

    # Credit card specific (optional)
    cc_outstanding: float = 0.0
    cc_monthly_rate_pct: float = 3.0        # default ~36% APR, typical Indian card
    cc_min_due_pct: float = 5.0             # typical min-due as % of outstanding

    # Savings / liquid assets (counts toward emergency fund — NOT FD locked for years, NOT EPF)
    liquid_savings: float = 0.0

    # Other monthly debt obligations not captured above (other loans' EMIs etc.)
    other_monthly_debt: float = 0.0


# ---------------------------------------------------------------------------
# Core metrics
# ---------------------------------------------------------------------------

def debt_to_income_ratio(inp: FinanceInput) -> float:
    """
    % of take-home income going to all EMIs + credit card minimum due.
    Banks generally consider above 50% risky/unaffordable for new credit.
    """
    cc_min_due = inp.cc_outstanding * (inp.cc_min_due_pct / 100)
    total_debt_payment = inp.total_emi + inp.other_monthly_debt + cc_min_due

    if inp.monthly_income <= 0:
        return 0.0

    ratio = (total_debt_payment / inp.monthly_income) * 100
    return round(ratio, 1)


def is_debt_ratio_dangerous(ratio_pct: float) -> bool:
    return ratio_pct >= 50.0


def total_monthly_debt_payment(inp: FinanceInput) -> float:
    cc_min_due = inp.cc_outstanding * (inp.cc_min_due_pct / 100)
    return round(inp.total_emi + inp.other_monthly_debt + cc_min_due, 2)


def monthly_surplus(inp: FinanceInput) -> float:
    """What's left after essentials + all debt payments. Can be negative."""
    total_out = inp.monthly_essential_expenses + total_monthly_debt_payment(inp)
    return round(inp.monthly_income - total_out, 2)


# ---------------------------------------------------------------------------
# Emergency fund
# ---------------------------------------------------------------------------

def emergency_fund_months(inp: FinanceInput) -> float:
    """
    How many months their current liquid savings would cover
    essential expenses + EMIs if income stopped today.
    """
    monthly_burn = inp.monthly_essential_expenses + total_monthly_debt_payment(inp)
    if monthly_burn <= 0:
        return 0.0
    months = inp.liquid_savings / monthly_burn
    return round(months, 1)


def months_to_build_safe_emergency_fund(inp: FinanceInput, target_months: int = 6) -> Optional[float]:
    """
    Months needed, at current surplus, to reach `target_months` of
    emergency cover. Returns None if surplus is zero/negative (i.e.
    they cannot build savings at the current rate — the report
    should flag this rather than show a number).
    """
    monthly_burn = inp.monthly_essential_expenses + total_monthly_debt_payment(inp)
    target_corpus = monthly_burn * target_months
    gap = target_corpus - inp.liquid_savings

    if gap <= 0:
        return 0.0  # already there

    surplus = monthly_surplus(inp)
    if surplus <= 0:
        return None

    return round(gap / surplus, 1)


# ---------------------------------------------------------------------------
# Credit card compounding payoff
# ---------------------------------------------------------------------------

def cc_minimum_due_payoff(inp: FinanceInput, max_months: int = 600) -> dict:
    """
    Simulates paying ONLY the minimum due each month, with interest
    compounding monthly on the remaining balance (standard Indian
    credit card behaviour: interest applies to whatever is left
    after the minimum payment, and the min-due % is recalculated
    each month on the new, smaller outstanding balance).

    Returns months to clear, total interest paid, and a capped flag
    if it would never realistically clear (very common with min-due-only).
    """
    if inp.cc_outstanding <= 0:
        return {
            "years_to_clear": 0,
            "months_to_clear": 0,
            "total_interest_paid": 0,
            "never_clears": False,
        }

    balance = inp.cc_outstanding
    rate = inp.cc_monthly_rate_pct / 100
    min_pct = inp.cc_min_due_pct / 100
    min_floor = 200  # banks set an absolute floor min-due, doesn't go below this

    total_interest = 0.0
    months = 0

    while balance > 1 and months < max_months:
        interest = balance * rate
        total_interest += interest
        balance_with_interest = balance + interest

        payment = max(balance_with_interest * min_pct, min_floor)
        payment = min(payment, balance_with_interest)  # don't overpay on the last cycle

        balance = balance_with_interest - payment
        months += 1

    never_clears = months >= max_months and balance > 1

    return {
        "years_to_clear": round(months / 12, 1) if not never_clears else None,
        "months_to_clear": months if not never_clears else None,
        "total_interest_paid": round(total_interest, 0) if not never_clears else None,
        "never_clears": never_clears,
    }


def cc_accelerated_payoff_target(inp: FinanceInput, target_years: float = 2.0) -> Optional[float]:
    """
    Fixed monthly payment needed to clear the CC balance in
    `target_years`, instead of minimum-due-only. Standard amortizing
    loan formula, using the card's monthly interest rate.
    """
    if inp.cc_outstanding <= 0:
        return 0.0

    r = inp.cc_monthly_rate_pct / 100
    n = int(target_years * 12)

    if r == 0:
        return round(inp.cc_outstanding / n, 0)

    # Standard EMI formula: P * r * (1+r)^n / ((1+r)^n - 1)
    factor = (1 + r) ** n
    payment = inp.cc_outstanding * r * factor / (factor - 1)
    return round(payment, 0)


# ---------------------------------------------------------------------------
# Home loan default risk
# ---------------------------------------------------------------------------

def months_before_default_risk(inp: FinanceInput) -> Optional[float]:
    """
    How many EMI cycles their current liquid savings could absorb
    if their income stopped today — i.e. a runway figure specific
    to the home loan EMI, distinct from the general emergency fund.
    Returns None if there's no home loan.
    """
    if inp.home_loan_emi <= 0:
        return None

    if inp.home_loan_emi <= 0:
        return None

    months = inp.liquid_savings / inp.home_loan_emi
    return round(months, 1)


def sarfaesi_applies(inp: FinanceInput) -> bool:
    """
    SARFAESI enforcement does not apply once 80%+ of the original
    loan principal has been repaid. Used to decide whether the
    report should mention auction risk at all for this person.
    """
    if inp.home_loan_outstanding <= 0:
        return False
    return inp.home_loan_repaid_pct < 80.0


def home_loan_default_stage(inp: FinanceInput) -> dict:
    """
    Maps current days-overdue to the real SARFAESI timeline, so the
    report can tell the person exactly where they stand right now —
    not just a generic future warning.

    Stages, based on real timelines:
      0 days        -> current, no risk yet
      1-89 days      -> late, reported to CIBIL after 30 days, but not yet NPA
      90-149 days    -> NPA classified, 60-day demand notice period begins
      150+ days      -> demand notice period likely expired, possession/auction risk active
    """
    days = inp.home_loan_days_overdue

    if days <= 0:
        return {
            "stage": "current",
            "days_overdue": 0,
            "message_key": "no_overdue",
        }
    elif days < 30:
        return {
            "stage": "late_not_yet_reported",
            "days_overdue": days,
            "message_key": "late_under_30",
        }
    elif days < 90:
        return {
            "stage": "reported_late",
            "days_overdue": days,
            "message_key": "reported_30_to_90",
        }
    elif days < 150:
        return {
            "stage": "npa_demand_notice_period",
            "days_overdue": days,
            "message_key": "npa_60_day_notice",
        }
    else:
        return {
            "stage": "possession_auction_risk",
            "days_overdue": days,
            "message_key": "auction_risk_active",
        }


def estimate_cibil_drop(inp: FinanceInput) -> dict:
    """
    Conservative published ranges (not individualized — CIBIL's
    actual model factors in full credit history we don't have).
    Distinguishes home loan vs other loan severity, and minor
    (<90 days) vs major (90+ days, NPA) default impact.
    """
    has_home_loan = inp.home_loan_outstanding > 0

    return {
        "minor_default_range": "25 to 50 points (if caught and fixed within 30-60 days)",
        "major_default_range": "80 to 100 points" if has_home_loan else "50 to 75 points",
        "recovery_months_minimum": 12,
        "recovery_months_full": 36,
        "record_visible_years": 7,
    }


# ---------------------------------------------------------------------------
# Debt payoff order (avalanche method — highest interest rate first)
# ---------------------------------------------------------------------------

def debt_payoff_order(inp: FinanceInput) -> list:
    """
    Returns debts ranked highest-interest-first. This is the
    mathematically optimal order to direct any extra surplus toward,
    while paying minimums on everything else.

    Note: home loan is usually excluded from "attack this first"
    advice since it's typically the lowest-rate, longest-tenure, and
    has tax benefits — but we still surface it for completeness.
    """
    debts = []

    if inp.cc_outstanding > 0:
        debts.append({
            "name": "Credit Card",
            "outstanding": inp.cc_outstanding,
            "annual_rate_pct": round(inp.cc_monthly_rate_pct * 12, 1),
            "priority_reason": "Highest interest rate — always pay this off first.",
        })

    if inp.other_monthly_debt > 0:
        debts.append({
            "name": "Other loans (personal/car/bike/gold/education)",
            "outstanding": None,  # not collected at this granularity in v1
            "annual_rate_pct": None,
            "priority_reason": "Usually higher rate than home loan — pay after credit card.",
        })

    if inp.home_loan_outstanding > 0:
        debts.append({
            "name": "Home Loan",
            "outstanding": inp.home_loan_outstanding,
            "annual_rate_pct": None,
            "priority_reason": "Lowest interest rate of your debts — pay the minimum EMI on time, don't rush extra payments here until the above are cleared.",
        })

    return debts


# ---------------------------------------------------------------------------
# 90-day plan targets
# ---------------------------------------------------------------------------

def ninety_day_targets(inp: FinanceInput) -> dict:
    """
    Simple, achievable month-by-month targets based on current surplus.
    Kept deliberately conservative — better to under-promise.
    """
    surplus = monthly_surplus(inp)
    cc_payoff = cc_minimum_due_payoff(inp)

    if surplus <= 0:
        return {
            "month_1_target": "Find ₹{} extra by cutting non-essential spending — you currently have no surplus left at month end.".format(
                abs(round(surplus, 0)) if surplus < 0 else 1000
            ),
            "month_2_target": "Once you have a small surplus, redirect all of it to your highest-interest debt.",
            "month_3_target": "Recheck your numbers — confirm your debt-to-income ratio has started to fall.",
        }

    # Roughly: month 1 build a small buffer, month 2-3 attack highest interest debt
    buffer_target = min(surplus * 1, inp.monthly_essential_expenses * 0.5)
    debt_attack_amount = round(surplus * 0.7, 0)

    return {
        "month_1_target": f"Set aside ₹{round(buffer_target, 0)} as a starter emergency buffer, separate from your spending account.",
        "month_2_target": f"Put ₹{debt_attack_amount} extra toward your highest-interest debt (see payoff order above), on top of minimums.",
        "month_3_target": f"Continue the ₹{debt_attack_amount} extra payment. Recheck your CIBIL report to confirm no new late marks.",
    }


# ---------------------------------------------------------------------------
# Master function — builds the full dict the Claude prompt expects
# ---------------------------------------------------------------------------

def build_calculated_data(payload: dict) -> dict:
    """
    Takes the raw form payload (dict, e.g. from request.get_json())
    and returns the fully calculated dict — ready to pass directly
    into the report-generation prompt's .format(**calculated_data) call.

    Expected payload keys map 1:1 to FinanceInput fields. Missing
    optional keys default to 0.
    """
    inp = FinanceInput(
        monthly_income=float(payload.get("monthly_income", 0)),
        monthly_essential_expenses=float(payload.get("monthly_essential_expenses", 0)),
        total_emi=float(payload.get("total_emi", 0)),
        home_loan_outstanding=float(payload.get("home_loan_outstanding", 0)),
        home_loan_emi=float(payload.get("home_loan_emi", 0)),
        home_loan_repaid_pct=float(payload.get("home_loan_repaid_pct", 0)),
        home_loan_days_overdue=int(payload.get("home_loan_days_overdue", 0)),
        cc_outstanding=float(payload.get("cc_outstanding", 0)),
        cc_monthly_rate_pct=float(payload.get("cc_monthly_rate_pct", 3.0)),
        cc_min_due_pct=float(payload.get("cc_min_due_pct", 5.0)),
        liquid_savings=float(payload.get("liquid_savings", 0)),
        other_monthly_debt=float(payload.get("other_monthly_debt", 0)),
    )

    dti_ratio = debt_to_income_ratio(inp)
    cc_payoff = cc_minimum_due_payoff(inp)
    cc_accel_2yr = cc_accelerated_payoff_target(inp, target_years=2.0)
    home_stage = home_loan_default_stage(inp)
    cibil = estimate_cibil_drop(inp)
    plan = ninety_day_targets(inp)

    return {
        # Income & debt
        "monthly_income": round(inp.monthly_income, 0),
        "total_monthly_debt_payment": total_monthly_debt_payment(inp),
        "debt_to_income_pct": dti_ratio,
        "is_debt_ratio_dangerous": is_debt_ratio_dangerous(dti_ratio),

        # Emergency fund
        "liquid_savings": round(inp.liquid_savings, 0),
        "monthly_essential_expenses": round(inp.monthly_essential_expenses, 0),
        "emergency_fund_months": emergency_fund_months(inp),
        "months_to_build_6mo_fund": months_to_build_safe_emergency_fund(inp, target_months=6),

        # Credit card
        "cc_outstanding": round(inp.cc_outstanding, 0),
        "cc_monthly_rate": inp.cc_monthly_rate_pct,
        "cc_years_to_clear": cc_payoff["years_to_clear"],
        "cc_total_interest": cc_payoff["total_interest_paid"],
        "cc_never_clears": cc_payoff["never_clears"],
        "cc_accelerated_payment": cc_accel_2yr,

        # Home loan
        "home_loan_outstanding": round(inp.home_loan_outstanding, 0),
        "home_loan_emi": round(inp.home_loan_emi, 0),
        "repaid_over_80_pct": inp.home_loan_repaid_pct >= 80.0,
        "sarfaesi_applies": sarfaesi_applies(inp),
        "is_overdue": inp.home_loan_days_overdue > 0,
        "days_overdue": inp.home_loan_days_overdue,
        "home_loan_stage": home_stage["stage"],
        "home_loan_stage_message_key": home_stage["message_key"],
        "months_before_default_risk": months_before_default_risk(inp),

        # CIBIL
        "cibil_minor_drop_range": cibil["minor_default_range"],
        "cibil_major_drop_range": cibil["major_default_range"],
        "cibil_recovery_months": cibil["recovery_months_minimum"],
        "cibil_full_recovery_months": cibil["recovery_months_full"],
        "cibil_record_years": cibil["record_visible_years"],

        # Plan
        "debt_payoff_order_list": debt_payoff_order(inp),
        "month_1_target": plan["month_1_target"],
        "month_2_target": plan["month_2_target"],
        "month_3_target": plan["month_3_target"],
    }


# ---------------------------------------------------------------------------
# Quick manual test (run: python fix_finance_calc.py)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import json

    sample_payload = {
        "monthly_income": 85000,
        "monthly_essential_expenses": 35000,
        "total_emi": 22000,
        "home_loan_outstanding": 2200000,
        "home_loan_emi": 18000,
        "home_loan_repaid_pct": 15,
        "home_loan_days_overdue": 0,
        "cc_outstanding": 65000,
        "cc_monthly_rate_pct": 3.5,
        "cc_min_due_pct": 5,
        "liquid_savings": 40000,
        "other_monthly_debt": 4000,
    }

    result = build_calculated_data(sample_payload)
    print(json.dumps(result, indent=2, default=str))
