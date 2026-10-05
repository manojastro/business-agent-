"""HTML and PDF exports of approved report versions, with evidence references and version info."""

from __future__ import annotations

from typing import Any

from fpdf import FPDF
from jinja2 import Environment, select_autoescape

_env = Environment(autoescape=select_autoescape(default=True, default_for_string=True))

HTML_TEMPLATE = _env.from_string(
    """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>{{ c.title }}</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
 body{font:15px/1.5 system-ui,-apple-system,Segoe UI,Roboto,sans-serif;color:#1d2433;max-width:900px;margin:32px auto;padding:0 16px}
 h1{font-size:22px;margin:0 0 4px} h2{font-size:17px;margin-top:28px;border-bottom:1px solid #d9dee8;padding-bottom:4px}
 .meta{color:#5b6475;font-size:13px} .badge{display:inline-block;padding:2px 8px;border-radius:10px;font-size:12px;font-weight:600}
 .sim{background:#fff3cd;color:#7a5600} .ok{background:#e3f4ea;color:#17663a} .rej{background:#fde7e7;color:#9b1c1c}
 table{border-collapse:collapse;width:100%;font-size:13px} td,th{border:1px solid #d9dee8;padding:4px 8px;text-align:left}
 .ev{font-family:ui-monospace,Consolas,monospace;font-size:11px;color:#5b6475}
 li{margin:6px 0}
</style></head><body>
<h1>{{ c.title }}</h1>
<div class="meta">{{ c.tenant.name }} &middot; report version {{ version }} &middot; status {{ status }}
 &middot; content hash <span class="ev">{{ content_hash[:16] }}</span></div>
<div class="meta">Approved by {{ approver or "-" }} on {{ approved_at or "-" }} &middot; generated {{ c.generated_at }}</div>
{% if c.simulation_label %}<p><span class="badge sim">{{ c.simulation_label }}</span> Produced with the deterministic fixture provider, not a language model.</p>{% endif %}
<p><b>Question:</b> {{ c.question }}</p>
<p class="meta">Metric {{ c.scope.metric.name }} v{{ c.scope.metric.version }} &middot; current [{{ c.scope.current_window.start }}, {{ c.scope.current_window.end }})
 vs baseline [{{ c.scope.baseline_window.start }}, {{ c.scope.baseline_window.end }}) &middot; {{ c.scope.timezone }} &middot; {{ c.scope.currency }}
 &middot; source watermark {{ c.scope.source_watermark }}</p>
<h2>Summary</h2><p>{{ c.summary }}</p>
<p><b>Primary driver:</b> {{ c.primary_driver.label }} ({{ c.primary_driver.confidence }} confidence). {{ c.primary_driver.explanation }}</p>
{% if c.revision_note %}<p class="meta">Revision note: {{ c.revision_note }}</p>{% endif %}
{% for title, key in [("Findings","findings"),("Hypotheses","hypotheses"),("Limitations","limitations"),("Recommended next investigations","recommendations")] %}
<h2>{{ title }}</h2>{% if c[key] %}<ul>{% for f in c[key] %}<li>{{ f.wording }}{% if f.evidence_ids %} <span class="ev">[evidence {% for e in f.evidence_ids %}{{ e[:8] }}{% if not loop.last %}, {% endif %}{% endfor %}]</span>{% endif %}
{% if f.verification %}<span class="badge ok">verified</span>{% endif %}</li>{% endfor %}</ul>{% else %}<p class="meta">None.</p>{% endif %}
{% endfor %}
{% if c.charts.components %}<h2>Component contributions</h2><table><tr><th>Component</th><th>Change (INR)</th></tr>
{% for p in c.charts.components.points %}<tr><td>{{ p.label }}</td><td>{{ p.value }}</td></tr>{% endfor %}
<tr><th>Total</th><th>{{ c.charts.components.total }}</th></tr></table><p class="ev">evidence {{ c.charts.components.evidence_id }}</p>{% endif %}
{% if c.charts.dimension %}<h2>Contribution by {{ c.charts.dimension.dimension }}</h2><table><tr><th>Segment</th><th>Contribution</th></tr>
{% for p in c.charts.dimension.points %}<tr><td>{{ p.label }}{% if p.small %} (small){% endif %}</td><td>{{ p.value }}</td></tr>{% endfor %}</table>
<p class="ev">evidence {{ c.charts.dimension.evidence_id }}</p>{% endif %}
<h2>Hypotheses tested</h2><table><tr><th>Hypothesis</th><th>Status</th><th>Rationale</th></tr>
{% for h in c.hypothesis_table %}<tr><td>{{ h.statement }}</td><td>{{ h.status }}</td><td>{{ h.rationale }}</td></tr>{% endfor %}</table>
{% if c.rejected_claims %}<h2>Rejected claims (not part of the findings)</h2><ul>{% for r in c.rejected_claims %}
<li><span class="badge rej">rejected</span> {{ r.wording }}<br><span class="meta">{% for x in r.reasons %}{{ x.code }}{% if not loop.last %}, {% endif %}{% endfor %}</span></li>{% endfor %}</ul>{% endif %}
<h2>Evidence index</h2><table><tr><th>ID</th><th>Label</th><th>Kind</th><th>Rows</th><th>Result hash</th></tr>
{% for e in c.evidence_index %}<tr><td class="ev">{{ e.id }}</td><td>{{ e.label }}</td><td>{{ e.kind }}</td><td>{{ e.row_count }}</td><td class="ev">{{ e.result_hash[:16] }}</td></tr>{% endfor %}</table>
<p class="meta">Model: {{ c.model.label }}. Numbers are recomputed deterministically from stored evidence before publication; causal statements are not made without a causal design.</p>
</body></html>"""
)


def render_html(content: dict[str, Any], meta: dict[str, Any]) -> str:
    return HTML_TEMPLATE.render(c=content, **meta)


def _latin(text: Any) -> str:
    s = str(text if text is not None else "")
    s = s.replace("₹", "INR ").replace("±", "+/-").replace("—", "-").replace("–", "-").replace("’", "'").replace("“", '"').replace("”", '"')
    return s.encode("latin-1", "replace").decode("latin-1")


def render_pdf(content: dict[str, Any], meta: dict[str, Any]) -> bytes:
    pdf = FPDF()
    pdf.set_auto_page_break(auto=True, margin=15)
    pdf.add_page()
    w = pdf.w - pdf.l_margin - pdf.r_margin

    def para(text: str, size: int = 10, style: str = "") -> None:
        pdf.set_font("Helvetica", style, size)
        pdf.multi_cell(w, 5, _latin(text))
        pdf.ln(1)

    def head(text: str) -> None:
        pdf.ln(3)
        para(text, 12, "B")

    para(content["title"], 15, "B")
    para(f"{content['tenant']['name']} | version {meta['version']} | status {meta['status']} | "
         f"content hash {meta['content_hash'][:16]}", 8)
    para(f"Approved by {meta.get('approver') or '-'} on {meta.get('approved_at') or '-'}", 8)
    if content.get("simulation_label"):
        para(f"[{content['simulation_label']}] Produced with the deterministic fixture provider, not a language model.", 9, "B")
    sc = content["scope"]
    para(f"Question: {content['question']}")
    para(f"Metric {sc['metric']['name']} v{sc['metric']['version']}; current [{sc['current_window']['start']}, "
         f"{sc['current_window']['end']}) vs baseline [{sc['baseline_window']['start']}, {sc['baseline_window']['end']}); "
         f"{sc['timezone']}; {sc['currency']}; watermark {sc['source_watermark']}", 8)
    head("Summary")
    para(content["summary"])
    d = content["primary_driver"]
    para(f"Primary driver: {d['label']} ({d['confidence']}). {d['explanation']}")
    for title, key in (("Findings", "findings"), ("Hypotheses", "hypotheses"), ("Limitations", "limitations"),
                       ("Recommended next investigations", "recommendations")):
        head(title)
        items = content.get(key) or []
        if not items:
            para("None.", 9)
        for f in items:
            refs = ", ".join(e[:8] for e in f.get("evidence_ids", []))
            para(f"- {f['wording']}" + (f"  [evidence {refs}]" if refs else ""))
    if content.get("rejected_claims"):
        head("Rejected claims (not part of the findings)")
        for r in content["rejected_claims"]:
            para(f"- {r['wording']}  ({', '.join(x['code'] for x in r['reasons'])})", 9)
    head("Evidence index")
    for e in content.get("evidence_index", []):
        para(f"{e['id']}  {e['label']}  ({e['kind']}, {e['row_count']} rows, hash {e['result_hash'][:12]})", 7)
    para(f"Model: {content['model']['label']}. Numbers are recomputed deterministically from stored evidence before "
         "publication.", 8)
    return bytes(pdf.output())
