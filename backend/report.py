"""PDF report for one finished (or partly finished) job.

    python -m backend.report <job_id>      # writes /tmp/report_<job_id>.pdf
"""
from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.lib.fonts import addMapping
from reportlab.platypus import PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

FONT, FONT_B = "Helvetica", "Helvetica-Bold"
AVAIL = A4[0] - 40 * mm


def _setup_fonts() -> None:
    global FONT, FONT_B
    d = Path("/usr/share/fonts/truetype/dejavu")
    r, b = d / "DejaVuSans.ttf", d / "DejaVuSans-Bold.ttf"
    if r.is_file() and b.is_file():
        pdfmetrics.registerFont(TTFont("DejaVu", str(r)))
        pdfmetrics.registerFont(TTFont("DejaVu-Bold", str(b)))
        for bold in (0, 1):
            for ital in (0, 1):
                addMapping("DejaVu", bold, ital, "DejaVu-Bold" if bold else "DejaVu")
        FONT, FONT_B = "DejaVu", "DejaVu-Bold"


def _styles() -> dict:
    base = getSampleStyleSheet()["Normal"]
    def st(name, **kw):
        return ParagraphStyle(name, parent=base, fontName=kw.pop("fontName", FONT), **kw)
    return {
        "title": st("t", fontName=FONT_B, fontSize=24, leading=28, spaceAfter=6),
        "sub": st("s", fontSize=11, leading=15, textColor=colors.HexColor("#444444")),
        "h1": st("h1", fontName=FONT_B, fontSize=16, leading=20, spaceBefore=4, spaceAfter=8, textColor=colors.HexColor("#0f3d7a")),
        "h2": st("h2", fontName=FONT_B, fontSize=12.5, leading=16, spaceBefore=8, spaceAfter=4),
        "h3": st("h3", fontName=FONT_B, fontSize=10.5, leading=14, spaceBefore=6, spaceAfter=2),
        "h4": st("h4", fontName=FONT_B, fontSize=9.5, leading=13, spaceBefore=4, spaceAfter=2),
        "body": st("b", fontSize=9, leading=12.5, spaceAfter=3),
        "bullet": st("bl", fontSize=9, leading=12.5, leftIndent=12, bulletIndent=2, spaceAfter=2),
        "cell": st("c", fontSize=7.5, leading=9.5),
        "cellb": st("cb", fontName=FONT_B, fontSize=7.5, leading=9.5),
        "small": st("sm", fontSize=7.5, leading=10, textColor=colors.HexColor("#666666")),
    }


def _c(s) -> str:
    s = "" if s is None else str(s)
    if FONT == "Helvetica":
        s = s.encode("cp1252", "replace").decode("cp1252")
    return s


def _inline(s) -> str:
    s = escape(_c(s))
    s = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", s)
    s = re.sub(r"`(.+?)`", r"<font face='Courier'>\1</font>", s)
    s = re.sub(r"\[([^\]]+)\]\((https?://[^)\s']+)\)", r"<link href='\2' color='#1a56db'>\1</link>", s)
    return s


def _p(text, style, **kw) -> Paragraph:
    try:
        return Paragraph(text, style, **kw)
    except Exception:
        return Paragraph(re.sub(r"<[^>]+>", "", text), style, **kw)


def _load(path: Path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return None


def _latest(jd: Path, pattern: str):
    files = sorted(jd.glob(pattern), key=lambda p: p.stat().st_mtime)
    return files[-1] if files else None


def _val(m):
    if isinstance(m, dict):
        for k in ("value", "latest", "current"):
            if k in m:
                return m[k]
        for v in m.values():
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                return v
        return None
    return m


def _fmt(v) -> str:
    if v is None or v == "":
        return "-"
    if isinstance(v, bool):
        return str(v)
    if isinstance(v, (int, float)):
        if abs(v) >= 1000:
            return f"{v:,.0f}"
        return f"{v:.3f}".rstrip("0").rstrip(".")
    return _c(v)


def _item_text(it) -> str:
    if isinstance(it, str):
        return it
    if isinstance(it, dict):
        for k in ("text", "statement", "claim", "summary", "headline", "title", "event", "reason"):
            if it.get(k):
                return str(it[k])
        long = [v for v in it.values() if isinstance(v, str) and len(v) > 20]
        return long[0] if long else json.dumps(it)[:200]
    return str(it)


def _table(rows, S, widths=None, header=True) -> Table:
    n = max(len(r) for r in rows)
    data = []
    for i, r in enumerate(rows):
        r = list(r) + [""] * (n - len(r))
        data.append([_p(_inline(c), S["cellb"] if (header and i == 0) else S["cell"]) for c in r])
    t = Table(data, colWidths=widths or [AVAIL / n] * n, repeatRows=1 if header else 0)
    style = [("GRID", (0, 0), (-1, -1), 0.3, colors.HexColor("#bbbbbb")),
             ("VALIGN", (0, 0), (-1, -1), "TOP"),
             ("LEFTPADDING", (0, 0), (-1, -1), 3), ("RIGHTPADDING", (0, 0), (-1, -1), 3)]
    if header:
        style.append(("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#e8eef7")))
    t.setStyle(TableStyle(style))
    return t


def _md(text: str, S) -> list:
    out, lines, i = [], text.splitlines(), 0
    while i < len(lines):
        ln = lines[i].rstrip()
        if ln.lstrip().startswith("|"):
            rows = []
            while i < len(lines) and lines[i].strip().startswith("|"):
                cells = [c.strip() for c in lines[i].strip().strip("|").split("|")]
                if not (cells and all(re.fullmatch(r":?-{2,}:?", c) for c in cells)):
                    rows.append(cells)
                i += 1
            if rows:
                out.append(_table(rows, S))
                out.append(Spacer(1, 4))
            continue
        i += 1
        m = re.match(r"(#{1,6})\s+(.*)", ln)
        if m:
            out.append(_p(_inline(m.group(2)), S["h" + str(min(len(m.group(1)) + 1, 4))]))
            continue
        m = re.match(r"\s*[-*]\s+(.*)", ln)
        if m:
            out.append(_p(_inline(m.group(1)), S["bullet"], bulletText="\u2022" if FONT != "Helvetica" else "-"))
            continue
        if not ln.strip():
            out.append(Spacer(1, 3))
        else:
            out.append(_p(_inline(ln), S["body"]))
    return out


def _cover(jd, meta, S, company):
    el = [Spacer(1, 50 * mm), _p(_inline(company or "Analysis"), S["title"]),
          _p("FinAnalyst job report", S["sub"]), Spacer(1, 8)]
    objective = meta.get("objective") or ""
    swot = _load(_latest(jd, "swot/*/swot_final.json")) if _latest(jd, "swot/*/swot_final.json") else None
    if not objective and swot:
        objective = swot.get("objective") or ""
    rows = [["Question", objective or "-"], ["Job ID", meta.get("job_id") or jd.name],
            ["Status", meta.get("status") or "-"], ["Created", meta.get("created") or "-"],
            ["Report generated", datetime.now().strftime("%Y-%m-%d %H:%M")],
            ["Model", os.environ.get("VULTR_CHAT_MODEL") or "-"]]
    af = _load(_latest(jd, "finance/*/approved_financials.json")) if _latest(jd, "finance/*/approved_financials.json") else None
    if af:
        rows.append(["Financials approved", f"{af.get('approved_at', '-')} by {af.get('approved_by', '-')}"])
    el.append(_table(rows, S, widths=[35 * mm, AVAIL - 35 * mm], header=False))
    el += [Spacer(1, 10), _p("Numbers come from SEC filings and the sources cited in each section. "
                            "Model-written text is analysis, not fact. Check the sources before relying on it.", S["small"]),
           PageBreak()]
    return el


def _financials(jd, S):
    f = _latest(jd, "finance/*/approved_financials.json")
    d = _load(f) if f else None
    if not d:
        return []
    el = [_p("Financial statements", S["h1"])]
    periods = sorted(d.get("periods") or [])[-6:]
    fields = d.get("fields") or {}
    rows = [["Item"] + [_c(p) for p in periods]]
    for name, fd in fields.items():
        ser = (fd or {}).get("series") or {}
        rows.append([name + (" (partial)" if (fd or {}).get("partial") else "")] + [_fmt(ser.get(p)) for p in periods])
    first = AVAIL * 0.22
    el.append(_table(rows, S, widths=[first] + [(AVAIL - first) / max(len(periods), 1)] * len(periods)))
    el.append(_p(f"Currency: {_c(d.get('currency'))} | units: {_c(d.get('units'))} | scope: {_c(d.get('entity_scope'))} | "
                 f"{len(d.get('periods') or [])} periods approved, latest {len(periods)} shown.", S["small"]))
    warns = d.get("accepted_warnings") or []
    if warns:
        el.append(_p(f"Warnings accepted at approval ({len(warns)})", S["h3"]))
        for w in warns[:25]:
            el.append(_p(_inline(f"[{w.get('severity', '')}] {w.get('message', '')}"), S["bullet"], bulletText="-"))
    el.append(PageBreak())
    return el


def _peers(jd, S):
    d = _load(jd / "peers" / "peer_comparison.json")
    if not d:
        return []
    el = [_p("Peer comparison", S["h1"])]
    metrics = list(((d.get("target") or {}).get("metrics") or {}).keys())
    peers = d.get("peers") or []
    med = d.get("median") or {}
    head = [_c((d.get("target") or {}).get("company") or "Target"), "Median"] + [_c(p.get("ticker") or p.get("name", "")[:12]) for p in peers]
    rows = [["Ratio"] + head]
    for k in metrics:
        row = [k.replace("_", " "), _fmt(_val(d["target"]["metrics"].get(k))), _fmt(_val(med.get(k)))]
        row += [_fmt(_val((p.get("values_used") or {}).get(k))) for p in peers]
        rows.append(row)
    el.append(_table(rows, S))
    el.append(_p(_inline(f"Source: {d.get('source', 'SEC filings')}. Peers: " + ", ".join(_c(p.get("name", "")) for p in peers)), S["small"]))
    for w in d.get("warnings") or []:
        el.append(_p(_inline(w), S["bullet"], bulletText="-"))
    el.append(PageBreak())
    return el


def _news(jd, S):
    f = _latest(jd, "news/sentiment_*/company_sentiment.json")
    d = _load(f) if f else None
    if not d:
        return []
    el = [_p("News and sentiment", S["h1"])]
    for comp, c in (d.get("companies") or {}).items():
        sc = c.get("signal_counts") or {}
        el.append(_p(_inline(f"**{comp}**: {c.get('n_articles', '-')} articles scored, net sentiment {_fmt(c.get('net_sentiment'))}. "
                             + (", ".join(f"{k}: {v}" for k, v in sc.items()))), S["body"]))
        for title, key in (("Top opportunities", "top_opportunities"), ("Top threats", "top_threats")):
            items = c.get(key) or []
            if items:
                el.append(_p(title, S["h3"]))
                for it in items[:6]:
                    el.append(_p(_inline(_item_text(it)), S["bullet"], bulletText="-"))
    v = _load(_latest(jd, "news/verify_*/verify_report.json")) if _latest(jd, "news/verify_*/verify_report.json") else None
    if v:
        st = v.get("statuses") or {}
        el.append(_p("Page checks", S["h3"]))
        el.append(_p(_inline(f"{v.get('n_events', '-')} events checked: " + ", ".join(f"{k} {n}" for k, n in st.items())), S["body"]))
    el.append(PageBreak())
    return el


def _md_section(title, files, S):
    el = []
    for f in files:
        try:
            txt = Path(f).read_text(encoding="utf-8")
        except Exception:
            continue
        el += _md(txt, S)
        el.append(Spacer(1, 8))
    if not el:
        return []
    return [_p(title, S["h1"])] + el + [PageBreak()]


def _audit(jd, S):
    logs = sorted((jd / "logs").glob("*.log")) if (jd / "logs").is_dir() else []
    if not logs:
        return []
    rows = [["Step log", "Lines", "Warnings and errors (first 3)"]]
    for f in logs:
        try:
            lines = f.read_text(encoding="utf-8", errors="replace").splitlines()
        except Exception:
            continue
        bad = [l.strip()[:180] for l in lines if re.search(r"\b(ERROR|WARNING|WARN|Traceback|error)\b", l)][:3]
        rows.append([f.stem, str(len(lines)), "<br/>".join(escape(_c(b)) for b in bad) or "-"])
    return [_p("Audit trail", S["h1"]), _table(rows, S, widths=[AVAIL * 0.22, AVAIL * 0.08, AVAIL * 0.70])]


def build_report(job_dir, out_path, meta: dict | None = None) -> Path:
    _setup_fonts()
    jd, out, meta = Path(job_dir), Path(out_path), meta or {}
    S = _styles()
    swot_f = _latest(jd, "swot/*/swot_final.json")
    sw = _load(swot_f) if swot_f else None
    company = meta.get("company") or (next(iter(sw["companies"])) if sw and sw.get("companies") else "")
    if not company:
        af = _load(_latest(jd, "finance/*/approved_financials.json")) if _latest(jd, "finance/*/approved_financials.json") else None
        company = (af or {}).get("company", "")
    story = _cover(jd, meta, S, company)
    story += _financials(jd, S)
    story += _peers(jd, S)
    story += _news(jd, S)
    story += _md_section("Filing explanations", [jd / "filings" / "filing_explanations.md"], S)
    story += _md_section("Regulatory documents", [jd / "regulatory" / "federal_register.md"], S)
    sm = _latest(jd, "swot/*/swot_final.md")
    story += _md_section("SWOT (reviewed)", [sm] if sm else [], S)
    story += _md_section("Root-cause analyses", sorted((jd / "rca").glob("*.md")) if (jd / "rca").is_dir() else [], S)
    story += _audit(jd, S)

    def foot(canvas, doc):
        canvas.saveState()
        canvas.setFont(FONT, 7.5)
        canvas.setFillColor(colors.HexColor("#777777"))
        canvas.drawString(20 * mm, 10 * mm, _c(f"FinAnalyst | {company} | job {jd.name}"))
        canvas.drawRightString(A4[0] - 20 * mm, 10 * mm, f"Page {doc.page}")
        canvas.restoreState()

    out.parent.mkdir(parents=True, exist_ok=True)
    doc = SimpleDocTemplate(str(out), pagesize=A4, leftMargin=20 * mm, rightMargin=20 * mm,
                            topMargin=18 * mm, bottomMargin=18 * mm, title=_c(f"{company} analysis report"))
    doc.build(story, onFirstPage=foot, onLaterPages=foot)
    return out


if __name__ == "__main__":
    jid = sys.argv[1]
    root = Path(__file__).resolve().parent.parent / "jobs" / jid
    print(build_report(root, Path(f"/tmp/report_{jid}.pdf")))
