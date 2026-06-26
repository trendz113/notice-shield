"""
Analysis engine: takes confirmed Form16 + AIS figures (after the user has
reviewed/edited any low/medium-confidence extractions) and runs deterministic
comparisons across the 8 risk buckets. No AI involved here — this is pure
diffing of numbers, which means it's auditable and never hallucinates a
mismatch that isn't actually in the data.

The output feeds two things downstream: the Claude-written plain-English
report, and the Excel workbook. Both should describe exactly what this
engine found — nothing more, nothing invented.
"""

MISMATCH_THRESHOLD_RS = 500  # ignore differences this small; rounding/paise noise


def _diff(a, b):
    if a is None or b is None:
        return None
    return abs(a - b)


def analyze(form16: dict, ais: dict, extra: dict | None = None):
    """
    form16 / ais: dicts of {field: {"value": ..., "confidence": ...}} as
    returned by extraction.py, AFTER user confirmation/edits.
    extra: optional dict for fields the engine can't get from PDFs alone,
    e.g. {"revised_itr_exemption": "10(14)", "original_itr_exemption": "HRA",
          "gst_turnover": 1800000, "itr_turnover": 1200000}
    """
    extra = extra or {}
    flags = []

    def val(source, key):
        entry = source.get(key)
        return entry["value"] if entry else None

    # Bucket 1: Salary mismatch (Form16 vs AIS)
    f16_salary = val(form16, "gross_salary")
    ais_salary = val(ais, "salary_amount_reported")
    d = _diff(f16_salary, ais_salary)
    if d is not None and d > MISMATCH_THRESHOLD_RS:
        flags.append({
            "bucket": "salary_mismatch",
            "severity": "red" if d > 50000 else "amber",
            "detail": f"Form16 shows gross salary of Rs.{f16_salary:,}, "
                      f"but AIS shows Rs.{ais_salary:,} reported by your employer "
                      f"to the tax department. Difference: Rs.{d:,}.",
        })

    # Bucket 2: Interest income omitted (AIS has it, presumably ITR doesn't —
    # caller should pass whether this was declared; default assumes not declared
    # if no override given, since this is the common failure mode)
    interest = val(ais, "interest_income")
    declared_interest = extra.get("interest_declared_in_itr")
    if interest and interest > MISMATCH_THRESHOLD_RS:
        if declared_interest is None or declared_interest < interest - MISMATCH_THRESHOLD_RS:
            flags.append({
                "bucket": "interest_income_omitted",
                "severity": "amber" if interest < 10000 else "red",
                "detail": f"Your bank reported Rs.{interest:,} interest income to AIS. "
                          f"Make sure this is included in your ITR under 'Income from Other Sources'.",
            })

    # Bucket 3: TDS credit mismatch
    f16_tds = val(form16, "total_tds")
    ais_tds = val(ais, "salary_tds_reported")
    d = _diff(f16_tds, ais_tds)
    if d is not None and d > MISMATCH_THRESHOLD_RS:
        flags.append({
            "bucket": "tds_mismatch",
            "severity": "red" if d > 10000 else "amber",
            "detail": f"Form16 shows Rs.{f16_tds:,} TDS deducted, but AIS shows "
                      f"Rs.{ais_tds:,} credited. Difference: Rs.{d:,}. "
                      f"Claiming the higher figure without it matching 26AS will likely "
                      f"trigger a refund-adjustment notice.",
        })

    # Bucket 4: Capital gains errors — only if caller supplies this data
    cg_declared = extra.get("capital_gains_declared")
    cg_ais = extra.get("capital_gains_in_ais")
    d = _diff(cg_declared, cg_ais)
    if d is not None and d > MISMATCH_THRESHOLD_RS:
        flags.append({
            "bucket": "capital_gains_mismatch",
            "severity": "red",
            "detail": f"AIS shows capital gains transactions totalling Rs.{cg_ais:,}, "
                      f"but Rs.{cg_declared:,} was declared. Check for omitted sale transactions.",
        })

    # Bucket 5: High-value transactions vs declared income
    sft_rows = ais.get("sft_transactions", {}).get("value", [])
    total_income = f16_salary or 0
    high_value_total = sum(r["amount"] for r in sft_rows if r.get("amount"))
    if high_value_total > 0 and total_income > 0 and high_value_total > total_income * 0.5:
        flags.append({
            "bucket": "high_value_vs_income",
            "severity": "amber",
            "detail": f"AIS shows Rs.{high_value_total:,} in high-value transactions "
                      f"(investments, large deposits etc.) against declared income of "
                      f"Rs.{total_income:,}. Keep source-of-funds documents ready in case asked.",
        })

    # Bucket 6: GST turnover vs ITR turnover gap (business/professional users only)
    gst_turnover = extra.get("gst_turnover")
    itr_turnover = extra.get("itr_turnover")
    d = _diff(gst_turnover, itr_turnover)
    if d is not None and d > MISMATCH_THRESHOLD_RS:
        flags.append({
            "bucket": "gst_itr_gap",
            "severity": "red" if d > 100000 else "amber",
            "detail": f"GST returns show turnover of Rs.{gst_turnover:,}, but ITR shows "
                      f"Rs.{itr_turnover:,}. This Rs.{d:,} gap is a common scrutiny trigger.",
        })

    # Bucket 7: Exemption "swap" between original and revised ITR
    orig_exemption = extra.get("original_itr_exemption")
    revised_exemption = extra.get("revised_itr_exemption")
    if orig_exemption and revised_exemption and orig_exemption != revised_exemption:
        flags.append({
            "bucket": "exemption_swap",
            "severity": "red",
            "detail": f"Your original return claimed '{orig_exemption}', but the revised "
                      f"return switched to '{revised_exemption}'. This exact pattern is "
                      f"currently being flagged by the department's nudge campaign.",
        })

    # Bucket 8: Stale AIS data
    ais_download_date = extra.get("ais_download_date")  # ISO date string
    filing_date = extra.get("filing_date")
    if ais_download_date and filing_date and ais_download_date < filing_date:
        flags.append({
            "bucket": "stale_ais",
            "severity": "amber",
            "detail": "Your AIS was downloaded before you filed. Banks and employers "
                      "can update AIS data after the fact — re-download a fresh copy "
                      "before filing or revising, so you're checking against current data.",
        })

    if not flags:
        verdict = "green"
    elif any(f["severity"] == "red" for f in flags):
        verdict = "red"
    else:
        verdict = "amber"

    return {"verdict": verdict, "flags": flags}


if __name__ == "__main__":
    import json
    form16 = {
        "gross_salary": {"value": 1250000, "confidence": "high"},
        "total_tds": {"value": 142000, "confidence": "high"},
    }
    ais = {
        "salary_amount_reported": {"value": 1250000, "confidence": "high"},
        "salary_tds_reported": {"value": 142000, "confidence": "high"},
        "interest_income": {"value": 8200, "confidence": "high"},
        "sft_transactions": {"value": [
            {"code": "SFT-016", "amount": 8200},
            {"code": "SFT-017", "amount": 50000},
        ]},
    }
    print(json.dumps(analyze(form16, ais), indent=2))
