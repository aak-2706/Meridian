from __future__ import annotations

import html
import io
from datetime import datetime



def _plain(value) -> str:
    return str(value if value is not None else "").replace("\u2011", "-").replace("\u2013", "-").replace("\u2014", "-").encode("ascii", "replace").decode("ascii")


def _paragraph(value, style) -> Paragraph:
    from reportlab.platypus import Paragraph
    safe = html.escape(_plain(value)).replace("\n", "<br/>")
    return Paragraph(safe, style)


def _money(value, unit: str) -> str:
    if unit == "%":
        return f"{value:,.2f}%"
    return f"{value:,.2f}" if "share" in unit.lower() else f"{value:,.0f} {unit}".strip()


def build_memo_pdf(result: dict) -> bytes:
    from reportlab.lib import colors
    from reportlab.lib.enums import TA_LEFT
    from reportlab.lib.pagesizes import letter
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import inch
    from reportlab.platypus import LongTable, Paragraph, SimpleDocTemplate, TableStyle

    green = colors.HexColor("#176c50")
    ink = colors.HexColor("#17251f")
    muted = colors.HexColor("#718078")
    rule_color = colors.HexColor("#dce5df")
    memo = result["memo"]
    base = getSampleStyleSheet()
    styles = {
        "title": ParagraphStyle("MeridianTitle", parent=base["Title"], fontName="Helvetica-Bold", fontSize=23, leading=27, textColor=ink, alignment=TA_LEFT, spaceAfter=7),
        "subtitle": ParagraphStyle("MeridianSubtitle", parent=base["Normal"], fontName="Helvetica", fontSize=9, leading=13, textColor=muted, spaceAfter=18),
        "section": ParagraphStyle("MeridianSection", parent=base["Heading2"], fontName="Helvetica-Bold", fontSize=13, leading=16, textColor=green, spaceBefore=13, spaceAfter=7, keepWithNext=True),
        "subsection": ParagraphStyle("MeridianSubsection", parent=base["Heading3"], fontName="Helvetica-Bold", fontSize=10, leading=13, textColor=ink, spaceBefore=8, spaceAfter=3, keepWithNext=True),
        "body": ParagraphStyle("MeridianBody", parent=base["BodyText"], fontName="Helvetica", fontSize=8.5, leading=12, textColor=ink, spaceAfter=6),
        "small": ParagraphStyle("MeridianSmall", parent=base["BodyText"], fontName="Helvetica", fontSize=7, leading=9, textColor=muted, spaceAfter=4),
        "cell": ParagraphStyle("MeridianCell", parent=base["BodyText"], fontName="Helvetica", fontSize=6.6, leading=8.5, textColor=ink),
        "cellhead": ParagraphStyle("MeridianCellHead", parent=base["BodyText"], fontName="Helvetica-Bold", fontSize=6.5, leading=8, textColor=colors.white),
    }
    story = [
        _paragraph(memo.get("title", "Comparative Equity Research Memo"), styles["title"]),
        _paragraph(f"{', '.join(memo.get('periods', []))} | Generated {result.get('created_at', datetime.now().isoformat())} | Evidence-limited research; not an investment recommendation.", styles["subtitle"]),
        _paragraph("Executive summary", styles["section"]),
        _paragraph(memo.get("executive_summary", ""), styles["body"]),
        _paragraph("Comparative assessment", styles["section"]),
        _paragraph(memo.get("comparative_assessment", ""), styles["body"]),
        _paragraph("Companies and rationale", styles["section"]),
        _paragraph(f"Compared: {' vs '.join(memo.get('companies', []))}. {memo.get('comparison_reason', 'No comparison rationale supplied.')}", styles["body"]),
        _paragraph("Coverage and data gaps", styles["section"]),
    ]
    for coverage in memo.get("coverage", {}).values():
        coverage_text = f"{coverage.get('company', 'Company')}: {len(coverage.get('documents', []))} stored document(s); missing periods: {', '.join(coverage.get('missing_periods', [])) or 'none identified'}; missing document types: {', '.join(coverage.get('missing_document_types', [])) or 'none identified'}."
        story.append(_paragraph(coverage_text, styles["body"]))
        for limitation in coverage.get("processing_limitations", []):
            story.append(_paragraph("Processing note: " + limitation, styles["small"]))
        if coverage.get("transcript_note"):
            story.append(_paragraph("Transcript coverage: " + coverage["transcript_note"], styles["small"]))
    story.extend([_paragraph("Reported financial comparison", styles["section"]),
        _paragraph("Financial facts retain their reported currency. Percentages and free cash flow are calculated only when inputs are comparable and available.", styles["small"])])
    financial_rows = [[_paragraph(label, styles["cellhead"]) for label in ("Company", "Period", "Metric", "Value", "Source / calculation", "Evidence")]]
    for company_data in memo.get("financial", {}).get("metrics", {}).values():
        for period, metrics in company_data.get("periods", {}).items():
            for name, metric in metrics.items():
                if not isinstance(metric, dict) or metric.get("value") is None:
                    continue
                value = _money(float(metric["value"]), str(metric.get("unit", "")))
                formula = metric.get("formula") or f"Reported XBRL tag: {metric.get('tag', 'not supplied')}"
                evidence_ids = ", ".join(metric.get("evidence_ids", [])) or "Unavailable"
                financial_rows.append([_paragraph(company_data.get("company"), styles["cell"]),
                    _paragraph(period, styles["cell"]), _paragraph(name.replace("_", " ").title(), styles["cell"]),
                    _paragraph(value, styles["cell"]), _paragraph(formula, styles["cell"]), _paragraph(evidence_ids, styles["cell"])])
    if len(financial_rows) == 1:
        financial_rows.append([_paragraph("No reported financial values available.", styles["cell"]), "", "", "", "", ""])
    table = LongTable(financial_rows, colWidths=[1.1*inch, .55*inch, .85*inch, .75*inch, 2.0*inch, 1.0*inch], repeatRows=1, hAlign="LEFT")
    table.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, 0), green), ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("GRID", (0, 0), (-1, -1), .35, rule_color), ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f5f8f6")]),
        ("LEFTPADDING", (0, 0), (-1, -1), 5), ("RIGHTPADDING", (0, 0), (-1, -1), 5),
        ("TOPPADDING", (0, 0), (-1, -1), 5), ("BOTTOMPADDING", (0, 0), (-1, -1), 5)]))
    story.extend([table, _paragraph("Risk disclosures", styles["section"])])
    for agent in memo.get("risks", []):
        story.append(_paragraph(agent.get("company", "Company"), styles["subsection"]))
        findings = agent.get("findings", [])
        if not findings:
            story.append(_paragraph("No evidence-backed finding was produced from retrieved disclosures.", styles["body"]))
        for finding in findings:
            story.append(_paragraph(f"{finding.get('category', 'Risk')} ({finding.get('severity', 'unrated')}) - {finding.get('description', '')}", styles["body"]))
            story.append(_paragraph(f"Evidence: {', '.join(finding.get('evidence_ids', []))}. Uncertainty: {finding.get('uncertainty', '')}", styles["small"]))
    story.append(_paragraph("Strategy and management outlook", styles["section"]))
    for agent in memo.get("strategies", []):
        story.append(_paragraph(agent.get("company", "Company"), styles["subsection"]))
        findings = agent.get("findings", [])
        if not findings:
            story.append(_paragraph("No evidence-backed strategic statement was identified.", styles["body"]))
        for finding in findings:
            story.append(_paragraph(f"{finding.get('category', 'Disclosure')} - {finding.get('description', '')}", styles["body"]))
            story.append(_paragraph(f"Evidence: {', '.join(finding.get('evidence_ids', []))}. Attribution: {finding.get('uncertainty', '')}", styles["small"]))
    story.extend([_paragraph("Trends, similarities, and differences", styles["section"]),
        _paragraph("Risk or strategy changes are not inferred from wording differences alone. Missing evidence for one company does not establish absence of an exposure.", styles["body"])])
    for item in memo.get("similarities", []):
        story.append(_paragraph(f"Shared evidence category: {item.get('category', 'Unspecified')} | Evidence: {', '.join(item.get('evidence_ids', []))}", styles["body"]))
    for item in memo.get("differences", []):
        story.append(_paragraph(f"Observed for {item.get('company', 'one company')}: {item.get('category', 'Unspecified')} - {item.get('interpretation', '')} | Evidence: {', '.join(item.get('evidence_ids', []))}", styles["body"]))
    if not memo.get("similarities") and not memo.get("differences"):
        story.append(_paragraph("No shared or one-sided category was identified in the retrieved evidence.", styles["body"]))
    story.extend([_paragraph("SWOT assessment", styles["section"]),
        _paragraph(memo.get("swot_assessment", "Not assessed; analyst review required."), styles["body"])])
    story.extend([_paragraph("Evidence validation and limitations", styles["section"]),
        _paragraph(f"Checked {memo.get('validation', {}).get('claims_checked', 0)} claim(s); retained {memo.get('validation', {}).get('claims_supported', 0)} and removed {memo.get('validation', {}).get('claims_removed', 0)}. Method: {memo.get('validation', {}).get('method', '')}", styles["body"])])
    for limitation in memo.get("limitations", []):
        story.append(_paragraph("- " + limitation, styles["body"]))
    story.append(_paragraph("Sources and evidence register", styles["section"]))
    for item in memo.get("evidence", []):
        location = f"page {item['page_number']}" if item.get("page_number") else (f"table {item['table_id']}" if item.get("table_id") else "location unavailable")
        source = item.get("source_url") or "uploaded original"
        text = f"{item.get('evidence_id', '')} | {item.get('company', '')} | {item.get('document_type', '')} {item.get('fiscal_period', '')} | {item.get('document_title', '')} | {location} | {source}"
        story.append(_paragraph(text, styles["small"]))

    buffer = io.BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=letter, rightMargin=.65*inch, leftMargin=.65*inch,
        topMargin=.65*inch, bottomMargin=.62*inch, title=_plain(memo.get("title", "Meridian research memo")),
        author="Meridian")
    def page_chrome(canvas, document):
        canvas.saveState()
        canvas.setStrokeColor(rule_color)
        canvas.line(.65*inch, .48*inch, 7.85*inch, .48*inch)
        canvas.setFont("Helvetica", 7)
        canvas.setFillColor(muted)
        canvas.drawString(.65*inch, .31*inch, "MERIDIAN | SOURCE-BASED EQUITY RESEARCH")
        canvas.drawRightString(7.85*inch, .31*inch, f"Page {document.page}")
        canvas.restoreState()
    doc.build(story, onFirstPage=page_chrome, onLaterPages=page_chrome)
    return buffer.getvalue()
