"""
PDF extraction for Form16 and AIS documents.
Every extracted field carries a confidence tag (high/medium/low) so the
calling code can force user confirmation on anything uncertain — this is
never optional, because the downstream report is paid and has real
financial consequences if a figure is wrong.
"""
import re
import io
from pypdf import PdfReader
import pdfplumber


def decrypt_ais_pdf(file_path_or_buffer, pan: str, dob_ddmmyyyy: str):
    """
    AIS PDFs from the IT portal are password protected.
    Password convention: PAN (lowercase) + DOB in DDMMYYYY format.
    e.g. PAN 'ABCDE1234A' + DOB 21-01-1991 -> 'abcde1234a21011991'
    """
    password = pan.lower().strip() + dob_ddmmyyyy.strip()
    reader = PdfReader(file_path_or_buffer)
    if reader.is_encrypted:
        result = reader.decrypt(password)
        if result == 0:
            raise ValueError(
                "Could not unlock this AIS PDF. Double check the PAN and "
                "date of birth match the ones used to download it."
            )
    return reader


def _money(raw: str):
    """Parse an Indian-formatted number string like '12,50,000' or '8,200.00' to int."""
    if raw is None:
        return None
    cleaned = re.sub(r"[^\d.]", "", raw)
    if not cleaned:
        return None
    try:
        return int(round(float(cleaned)))
    except ValueError:
        return None


def _find_after_label(text: str, labels: list[str], window: int = 80):
    """Search for the first matching label and pull a money value shortly after it."""
    for label in labels:
        idx = text.find(label)
        if idx == -1:
            continue
        snippet = text[idx: idx + len(label) + window]
        match = re.search(r"Rs\.?\s*([\d,]+(?:\.\d+)?)", snippet)
        if match:
            return _money(match.group(1)), "high"
    return None, None


def extract_form16(file_path_or_buffer):
    """
    Form16 layout is fairly standardized (mandated fields), but employer
    templates vary in spacing/ordering, so we search by label rather than
    fixed position, and fall back to table scanning with lower confidence
    when a label isn't found directly.
    """
    out = {}
    with pdfplumber.open(file_path_or_buffer) as pdf:
        full_text = "\n".join(page.extract_text() or "" for page in pdf.pages)
        tables = []
        for page in pdf.pages:
            tables.extend(page.extract_tables() or [])

    fields = {
        "gross_salary": [
            "Gross Salary",
            "Salary as per provisions contained in section 17(1)",
        ],
        "standard_deduction": ["Standard Deduction"],
        "chargeable_income": ['Income chargeable under the head "Salaries"'],
        "total_tds": [
            "Total amount of tax deducted",
            "Tax Deducted at Source",
        ],
        "pan": [],
        "tan": [],
    }

    for key, labels in fields.items():
        if key in ("pan", "tan"):
            continue
        value, confidence = _find_after_label(full_text, labels)
        if value is not None:
            out[key] = {"value": value, "confidence": confidence}

    pan_match = re.search(r"PAN[:\s]*([A-Z]{5}\d{4}[A-Z])", full_text)
    if pan_match:
        out["pan"] = {"value": pan_match.group(1), "confidence": "high"}

    tan_match = re.search(r"TAN[:\s]*([A-Z]{4}\d{5}[A-Z])", full_text)
    if tan_match:
        out["tan"] = {"value": tan_match.group(1), "confidence": "high"}

    # Fallback: if total_tds wasn't found by label, scan tables for a
    # plausible TDS total row — flagged medium confidence since this is
    # a heuristic, not a precise label match.
    if "total_tds" not in out:
        for table in tables:
            for row in table:
                row_text = " ".join(c for c in row if c)
                if row_text and re.search(r"total.*tax.*deduct", row_text, re.I):
                    nums = re.findall(r"[\d,]+(?:\.\d+)?", row_text)
                    if nums:
                        out["total_tds"] = {
                            "value": _money(nums[-1]),
                            "confidence": "medium",
                        }
                        break

    return out


def extract_ais(file_path_or_buffer):
    """
    AIS PDF has Part A (general/PAN info) and Part B with sections:
    TDS/TCS Information, SFT Information, Demand/Refund, Other Information.
    We extract the few figures the risk-analysis engine actually needs,
    plus a raw list of SFT transaction rows for the high-value-transaction check.
    """
    out = {}
    with pdfplumber.open(file_path_or_buffer) as pdf:
        full_text = "\n".join(page.extract_text() or "" for page in pdf.pages)

    interest_value, conf = _find_after_label(
        full_text, ["Interest from savings bank", "SFT-016"]
    )
    if interest_value is not None:
        out["interest_income"] = {"value": interest_value, "confidence": conf}

    tds_value, conf = _find_after_label(
        full_text, ["TDS on salary", "Salary TDS", "Tax Deducted"]
    )
    if tds_value is not None:
        out["salary_tds_reported"] = {"value": tds_value, "confidence": conf}

    salary_value, conf = _find_after_label(
        full_text, ["Salary reported", "Salary received"]
    )
    if salary_value is not None:
        out["salary_amount_reported"] = {"value": salary_value, "confidence": conf}

    refund_int_value, conf = _find_after_label(
        full_text, ["Interest on income tax refund"]
    )
    if refund_int_value is not None:
        out["tax_refund_interest"] = {"value": refund_int_value, "confidence": conf}

    # SFT rows — collect any line starting with an SFT code, for the
    # high-value-transaction-vs-declared-income check downstream.
    sft_rows = []
    for line in full_text.splitlines():
        m = re.match(r"\s*(SFT-\d+)\s+(.*?)\s+Rs\.?\s*([\d,]+(?:\.\d+)?)", line)
        if m:
            sft_rows.append({
                "code": m.group(1),
                "raw_row": line.strip(),
                "amount": _money(m.group(3)),
            })
    if sft_rows:
        out["sft_transactions"] = {"value": sft_rows, "confidence": "high"}

    return out
