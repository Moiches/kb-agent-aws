"""Generate the one PDF document in the sample knowledge base.

The knowledge base is mostly Markdown, but we include a single PDF so the ingestion
pipeline exercises the PDF loader and so citations can carry a page number.

Run from the repo root:  python scripts/make_sample_pdf.py
"""

from pathlib import Path

from reportlab.lib.enums import TA_JUSTIFY
from reportlab.lib.pagesizes import LETTER
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer

OUTPUT = Path(__file__).resolve().parents[1] / "sample-docs" / "remote-work-policy.pdf"

TITLE = "Remote Work Policy"

# (style, text) pairs. "h1"/"h2" are headings, "p" is body copy, "b" is a bullet.
CONTENT = [
    ("meta", "Document owner: People Operations &nbsp;&nbsp;|&nbsp;&nbsp; "
             "Last reviewed: 2026-02-11 &nbsp;&nbsp;|&nbsp;&nbsp; Applies to: All Northwind employees"),

    ("h2", "1. Eligibility"),
    ("p", "All full-time employees are eligible to work remotely unless their role requires physical "
          "presence, such as hardware laboratory work or on-site customer engagements. Contractors "
          "follow the terms of their individual statement of work rather than this policy."),
    ("p", "New employees work from an office or a co-working space for their first four weeks so that "
          "onboarding, equipment setup, and security training can be completed in person where "
          "practical. Managers may waive this requirement for employees hired into fully distributed "
          "teams."),

    ("h2", "2. Core collaboration hours"),
    ("p", "Employees may set their own schedule, provided they are reachable during core collaboration "
          "hours of 10:00 to 15:00 in their assigned team timezone. Meetings are scheduled inside core "
          "hours wherever possible. Employees who need to work outside these hours on a recurring basis "
          "must agree the arrangement with their manager and record it in the team calendar."),

    ("h2", "3. Equipment and expenses"),
    ("p", "Northwind provides a laptop, an external monitor, a keyboard, and a mouse to every remote "
          "employee. Employees may claim a one-time home office allowance of USD 750 within their first "
          "90 days, and an annual allowance of USD 250 thereafter for replacements and ergonomic "
          "improvements."),
    ("b", "Internet connectivity is reimbursed up to USD 60 per month against a receipt."),
    ("b", "Co-working memberships are reimbursed up to USD 300 per month with manager approval."),
    ("b", "Furniture purchased with the allowance remains the property of the employee."),
    ("b", "Laptops and monitors remain the property of Northwind and must be returned on termination."),

    ("h2", "4. Security requirements for remote work"),
    ("p", "Remote work does not relax any security control. Employees must use the company-issued device "
          "for all work involving customer data, keep full-disk encryption enabled, and connect through "
          "the company VPN when accessing internal administrative systems."),
    ("b", "Public or shared Wi-Fi may be used only with the VPN active."),
    ("b", "Screens must be locked whenever the device is unattended, including at home."),
    ("b", "Customer data must never be copied to personal devices or personal cloud storage."),
    ("b", "Lost or stolen devices must be reported to security within 24 hours."),

    ("h2", "5. Working from another country"),
    ("p", "Employees may work from a country other than their country of employment for up to 30 "
          "calendar days per year without prior approval, provided they remain tax-resident in their "
          "home country. Stays longer than 30 days require written approval from People Operations "
          "because they can create payroll, tax, and permanent-establishment obligations for the "
          "company."),
    ("p", "Some countries are excluded entirely for sanctions or data protection reasons. People "
          "Operations maintains the current exclusion list and reviews it quarterly."),

    ("h2", "6. Office attendance"),
    ("p", "Teams may designate up to four in-person days per quarter for planning or team building. "
          "Travel and accommodation for designated in-person days are paid by the company. Employees "
          "who cannot attend for accessibility, caregiving, or health reasons are accommodated remotely "
          "and are not disadvantaged in performance review."),

    ("h2", "7. Policy exceptions"),
    ("p", "Exceptions to this policy require written approval from the employee's manager and from "
          "People Operations. Approved exceptions are recorded in the employee's file and reviewed at "
          "each performance cycle."),
]


def build() -> None:
    styles = getSampleStyleSheet()
    body = ParagraphStyle(
        "Body", parent=styles["BodyText"], fontSize=10.5, leading=15,
        alignment=TA_JUSTIFY, spaceAfter=8,
    )
    meta = ParagraphStyle(
        "Meta", parent=body, fontSize=8.5, textColor="#555555", spaceAfter=16,
    )
    h1 = ParagraphStyle("H1", parent=styles["Heading1"], fontSize=18, spaceAfter=4)
    h2 = ParagraphStyle("H2", parent=styles["Heading2"], fontSize=12.5, spaceBefore=12, spaceAfter=6)
    bullet = ParagraphStyle("Bullet", parent=body, leftIndent=16, bulletIndent=4, spaceAfter=4)

    style_map = {"meta": meta, "h2": h2, "p": body, "b": bullet}

    doc = SimpleDocTemplate(
        str(OUTPUT), pagesize=LETTER,
        leftMargin=1 * inch, rightMargin=1 * inch,
        topMargin=0.9 * inch, bottomMargin=0.9 * inch,
        title=TITLE, author="Northwind Analytics",
    )

    flow = [Paragraph(TITLE, h1), Spacer(1, 2)]
    for kind, text in CONTENT:
        if kind == "b":
            flow.append(Paragraph(text, bullet, bulletText="•"))
        else:
            flow.append(Paragraph(text, style_map[kind]))

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    doc.build(flow)
    print(f"wrote {OUTPUT} ({OUTPUT.stat().st_size:,} bytes)")


if __name__ == "__main__":
    build()
