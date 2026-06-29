"""
fix_finance_pdf.py

Generates the professional, paid PDF report for "Fix Your Finance Now".
Takes the calculated data (from fix_finance_calc.py) and the Claude-written
narration (from the report-generation prompt) and lays it out as a clean,
multi-section PDF — not a wall of text.

Mirrors the role excel_report.py plays for Notice Shield: this is the
paid, downloadable deliverable. Output is a BytesIO buffer, ready to be
returned via Flask's send_file(), same pattern as /api/download-excel.

Add to requirements.txt:
    reportlab==4.2.2
"""

import io
import os
from datetime import datetime

from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.lib import colors
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle,
    HRFlowable, PageBreak, KeepTogether,
)
from reportlab.lib.enums import TA_LEFT, TA_CENTER
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont


# ---------------------------------------------------------------------------
# Font registration — Helvetica's built-in glyphs do NOT include the ₹
# symbol and render it as a solid black box. DejaVu Sans does include it
# (U+20B9) and ships by default on Debian/Ubuntu, which is what Railway's
# Python buildpack uses. We embed it directly into the PDF at build time,
# so the output file doesn't depend on fonts being present at *read* time —
# only at *generation* time, which is this server.
#
# If DejaVu Sans isn't found (e.g. a different base image), we fall back
# to Helvetica and replace ₹ with "Rs." everywhere so nothing renders as
# a black box either way.
# ---------------------------------------------------------------------------
_DEJAVU_REGULAR = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
_DEJAVU_BOLD = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
_DEJAVU_OBLIQUE = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Oblique.ttf"

RUPEE_FONT_AVAILABLE = os.path.exists(_DEJAVU_REGULAR) and os.path.exists(_DEJAVU_BOLD)

if RUPEE_FONT_AVAILABLE:
    pdfmetrics.registerFont(TTFont("BodyFont", _DEJAVU_REGULAR))
    pdfmetrics.registerFont(TTFont("BodyFont-Bold", _DEJAVU_BOLD))
    if os.path.exists(_DEJAVU_OBLIQUE):
        pdfmetrics.registerFont(TTFont("BodyFont-Oblique", _DEJAVU_OBLIQUE))
    else:
        pdfmetrics.registerFont(TTFont("BodyFont-Oblique", _DEJAVU_REGULAR))
    # Map the family so inline <b> and <i> tags inside Paragraph text work
    # automatically, instead of requiring explicit fontName overrides.
    from reportlab.pdfbase.pdfmetrics import registerFontFamily
    registerFontFamily(
        "BodyFont",
        normal="BodyFont",
        bold="BodyFont-Bold",
        italic="BodyFont-Oblique",
        boldItalic="BodyFont-Bold",
    )
    FONT_REGULAR = "BodyFont"
    FONT_BOLD = "BodyFont-Bold"
    FONT_OBLIQUE = "BodyFont-Oblique"
    RUPEE = "\u20b9"  # ₹
else:
    FONT_REGULAR = "Helvetica"
    FONT_BOLD = "Helvetica-Bold"
    FONT_OBLIQUE = "Helvetica-Oblique"
    RUPEE = "Rs. "  # safe fallback, no glyph risk


def fmt_money(amount) -> str:
    """Formats a number as a currency string using whichever symbol is safe
    for the currently registered font. Always use this instead of an
    inline f-string with ₹, anywhere in this file."""
    try:
        value = f"{float(amount):,.0f}"
    except (TypeError, ValueError):
        value = "0"
    if RUPEE == "Rs. ":
        return f"Rs. {value}"
    return f"{RUPEE}{value}"


# ---------------------------------------------------------------------------
# Brand colors — adjust to match salarybit.in's actual palette if different
# ---------------------------------------------------------------------------
COLOR_PRIMARY = colors.HexColor("#1a3a5c")     # deep blue — headers
COLOR_DANGER = colors.HexColor("#c0392b")      # red — danger zone callouts
COLOR_SAFE = colors.HexColor("#1e8449")        # green — safe/good callouts
COLOR_WARNING = colors.HexColor("#b9770e")     # amber — caution
COLOR_MUTED = colors.HexColor("#5a6b7d")       # grey-blue — secondary text
COLOR_BG_LIGHT = colors.HexColor("#f4f7fa")    # light panel background


def _styles():
    base = getSampleStyleSheet()

    base.add(ParagraphStyle(
        name="ReportTitle", fontName=FONT_BOLD, fontSize=22,
        textColor=COLOR_PRIMARY, spaceAfter=4, leading=26,
    ))
    base.add(ParagraphStyle(
        name="ReportSubtitle", fontName=FONT_REGULAR, fontSize=10,
        textColor=COLOR_MUTED, spaceAfter=18,
    ))
    base.add(ParagraphStyle(
        name="SectionHeading", fontName=FONT_BOLD, fontSize=14,
        textColor=COLOR_PRIMARY, spaceBefore=18, spaceAfter=8,
    ))
    base.add(ParagraphStyle(
        name="BodyTextCustom", fontName=FONT_REGULAR, fontSize=10.5,
        textColor=colors.HexColor("#1f2937"), leading=15, spaceAfter=8,
    ))
    base.add(ParagraphStyle(
        name="HeadlineBanner", fontName=FONT_BOLD, fontSize=13,
        textColor=colors.white, leading=18, alignment=TA_LEFT,
    ))
    base.add(ParagraphStyle(
        name="BulletText", fontName=FONT_REGULAR, fontSize=10.5,
        textColor=colors.HexColor("#1f2937"), leading=15,
        leftIndent=14, spaceAfter=6,
    ))
    base.add(ParagraphStyle(
        name="PlanMonth", fontName=FONT_BOLD, fontSize=11,
        textColor=COLOR_PRIMARY, spaceAfter=2,
    ))
    base.add(ParagraphStyle(
        name="FooterNote", fontName=FONT_OBLIQUE, fontSize=8.5,
        textColor=COLOR_MUTED, spaceBefore=20,
    ))
    return base


def _danger_color_for_ratio(is_dangerous: bool):
    return COLOR_DANGER if is_dangerous else COLOR_SAFE


def _headline_banner(headline_text: str, is_dangerous: bool, styles):
    """Top-of-report colored banner stating the single biggest truth."""
    bg = _danger_color_for_ratio(is_dangerous)
    table = Table(
        [[Paragraph(headline_text, styles["HeadlineBanner"])]],
        colWidths=[170 * mm],
    )
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), bg),
        ("LEFTPADDING", (0, 0), (-1, -1), 14),
        ("RIGHTPADDING", (0, 0), (-1, -1), 14),
        ("TOPPADDING", (0, 0), (-1, -1), 12),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 12),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
    ]))
    return table


def _key_numbers_panel(calculated: dict, styles):
    """A clean 2-column panel of the headline numbers, like a dashboard strip."""
    rows = [
        ["Monthly Income", fmt_money(calculated.get('monthly_income', 0))],
        ["Total Monthly Debt Payments", fmt_money(calculated.get('total_monthly_debt_payment', 0))],
        ["Debt-to-Income Ratio", f"{calculated.get('debt_to_income_pct', 0)}%"],
        ["Emergency Fund Cover", f"{calculated.get('emergency_fund_months', 0)} months"],
    ]

    table_data = [[Paragraph(f"<b>{label}</b>", styles["BodyTextCustom"]),
                    Paragraph(value, styles["BodyTextCustom"])] for label, value in rows]

    t = Table(table_data, colWidths=[95 * mm, 75 * mm])
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), COLOR_BG_LIGHT),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.white),
        ("LEFTPADDING", (0, 0), (-1, -1), 10),
        ("TOPPADDING", (0, 0), (-1, -1), 7),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
    ]))
    return t


def _bullet_list(items: list, styles, style_name="BulletText"):
    flowables = []
    for item in items:
        flowables.append(Paragraph(f"•&nbsp;&nbsp;{item}", styles[style_name]))
    return flowables


def build_fix_finance_pdf(calculated: dict, report: dict, user_name: str = "") -> io.BytesIO:
    """
    calculated : dict   -> output of fix_finance_calc.build_calculated_data()
    report     : dict   -> Claude's narration, matching the JSON schema from
                            the report-generation prompt (headline,
                            whats_wrong, what_happens_if_you_dont_fix_this,
                            your_way_out, ninety_day_plan, your_safety_number)
    user_name  : str    -> optional, shown in the footer/header

    Returns a BytesIO buffer ready for Flask's send_file().
    """
    styles = _styles()
    buf = io.BytesIO()

    doc = SimpleDocTemplate(
        buf, pagesize=A4,
        leftMargin=20 * mm, rightMargin=20 * mm,
        topMargin=18 * mm, bottomMargin=18 * mm,
    )

    story = []

    # --- Header ---
    story.append(Paragraph("Fix Your Finance Now", styles["ReportTitle"]))
    subtitle = f"Your Personal Financial Reality Check"
    if user_name:
        subtitle += f"  ·  Prepared for {user_name}"
    subtitle += f"  ·  {datetime.now().strftime('%d %B %Y')}"
    story.append(Paragraph(subtitle, styles["ReportSubtitle"]))

    # --- Headline banner ---
    is_dangerous = bool(calculated.get("is_debt_ratio_dangerous", False))
    story.append(_headline_banner(report.get("headline", ""), is_dangerous, styles))
    story.append(Spacer(1, 14))

    # --- Key numbers panel ---
    story.append(_key_numbers_panel(calculated, styles))
    story.append(Spacer(1, 10))

    # --- What's Wrong ---
    story.append(Paragraph("What's Actually Wrong", styles["SectionHeading"]))
    story.append(HRFlowable(width="100%", thickness=0.6, color=COLOR_PRIMARY, spaceAfter=8))
    whats_wrong = report.get("whats_wrong", [])
    story.extend(_bullet_list(whats_wrong, styles))
    story.append(Spacer(1, 6))

    # --- What happens if you don't fix this ---
    story.append(Paragraph("What Happens If You Don't Fix This", styles["SectionHeading"]))
    story.append(HRFlowable(width="100%", thickness=0.6, color=COLOR_DANGER, spaceAfter=8))

    consequences = report.get("what_happens_if_you_dont_fix_this", {})

    if consequences.get("credit_card"):
        story.append(Paragraph("<b>Your Credit Card</b>", styles["BodyTextCustom"]))
        story.append(Paragraph(consequences["credit_card"], styles["BodyTextCustom"]))
        story.append(Spacer(1, 6))

    if consequences.get("home_loan"):
        story.append(Paragraph("<b>Your Home Loan</b>", styles["BodyTextCustom"]))
        story.append(Paragraph(consequences["home_loan"], styles["BodyTextCustom"]))
        story.append(Spacer(1, 6))

    if consequences.get("cibil_general"):
        story.append(Paragraph("<b>Your Credit Score</b>", styles["BodyTextCustom"]))
        story.append(Paragraph(consequences["cibil_general"], styles["BodyTextCustom"]))

    story.append(Spacer(1, 6))

    # --- Your Way Out ---
    story.append(Paragraph("Your Way Out — In Order", styles["SectionHeading"]))
    story.append(HRFlowable(width="100%", thickness=0.6, color=COLOR_SAFE, spaceAfter=8))
    way_out = report.get("your_way_out", [])
    numbered = [f"{i+1}. {step}" for i, step in enumerate(way_out)]
    for line in numbered:
        story.append(Paragraph(line, styles["BulletText"]))

    story.append(Spacer(1, 6))

    # --- 90-Day Plan ---
    story.append(Paragraph("Your 90-Day Plan", styles["SectionHeading"]))
    story.append(HRFlowable(width="100%", thickness=0.6, color=COLOR_PRIMARY, spaceAfter=10))

    plan = report.get("ninety_day_plan", {})
    plan_rows = [
        ("Month 1", plan.get("month_1", "")),
        ("Month 2", plan.get("month_2", "")),
        ("Month 3", plan.get("month_3", "")),
    ]
    for label, text in plan_rows:
        block = [
            Paragraph(label, styles["PlanMonth"]),
            Paragraph(text, styles["BodyTextCustom"]),
            Spacer(1, 8),
        ]
        story.append(KeepTogether(block))

    # --- Safety Number ---
    story.append(Paragraph("Your Safety Number", styles["SectionHeading"]))
    story.append(HRFlowable(width="100%", thickness=0.6, color=COLOR_WARNING, spaceAfter=8))
    story.append(Paragraph(report.get("your_safety_number", ""), styles["BodyTextCustom"]))

    # --- Footer / disclaimer ---
    story.append(Spacer(1, 16))
    story.append(Paragraph(
        "This report is generated from the numbers you provided and is meant to help you "
        "understand your financial position in plain language. It is not personalized "
        "financial, tax, or legal advice. For decisions involving large sums or legal "
        "notices, consider speaking with a qualified financial advisor or chartered accountant.",
        styles["FooterNote"]
    ))
    story.append(Paragraph("Fix Your Finance Now — by SalaryBit", styles["FooterNote"]))

    doc.build(story)
    buf.seek(0)
    return buf


# ---------------------------------------------------------------------------
# Quick manual test
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    sample_calculated = {
        "monthly_income": 85000,
        "total_monthly_debt_payment": 29250,
        "debt_to_income_pct": 34.4,
        "is_debt_ratio_dangerous": False,
        "emergency_fund_months": 0.6,
    }
    sample_report = {
        "headline": "You are spending close to safe limits, but your emergency fund is dangerously thin at under 1 month of cover.",
        "whats_wrong": [
            "You only have 0.6 months of expenses saved — one bad month and you can't pay your bills.",
            "Your credit card balance is growing faster than you're paying it down.",
            "You're not behind on your home loan yet, but you have very little cushion if something goes wrong.",
        ],
        "what_happens_if_you_dont_fix_this": {
            "credit_card": "You owe ₹65,000 on your card. If you keep paying only the minimum, it will take you nearly 17 years to clear it, and you'll pay around ₹1,30,000 in interest — twice what you borrowed.",
            "home_loan": "You haven't missed a payment yet, which is good. But your savings could only cover about 2 months of EMI if your income stopped. If you miss 3 EMIs in a row, the bank can mark your loan as bad debt and start the process to take your house — usually within 5 to 7 months of the first missed payment.",
            "cibil_general": "If you do miss payments for 90 days, your credit score could drop by 80 to 100 points, and this stays visible on your record for up to 7 years.",
        },
        "your_way_out": [
            "Pay more than the minimum on your credit card every month — even an extra ₹2,000 makes a big difference.",
            "Build a small emergency fund first, even ₹15,000-20,000, before aggressively paying down debt.",
            "Call your bank if you ever feel at risk of missing an EMI — ask about restructuring before you miss a payment, not after.",
        ],
        "ninety_day_plan": {
            "month_1": "Set aside ₹17,500 as a starter emergency buffer, kept separate from your spending account.",
            "month_2": "Put an extra ₹14,500 toward your credit card balance, on top of the minimum due.",
            "month_3": "Continue the extra ₹14,500 payment. Check your credit score to confirm no new late marks.",
        },
        "your_safety_number": "You currently have 0.6 months of safety cover. Aim for at least 3 months. At your current pace, building a 6-month cushion would take about 17 months — focus on the 90-day plan above to get there faster.",
    }

    pdf_buf = build_fix_finance_pdf(sample_calculated, sample_report, user_name="Test User")
    with open("/home/claude/sample_fix_finance_report.pdf", "wb") as f:
        f.write(pdf_buf.read())
    print("Sample PDF generated successfully.")
