"""Live repository discovery + coverage gate, with generated report files.

Guarantees the migration covers every LOCAL (IDM-owned) repository that
Artifactory holds *right now* — not just what a point-in-time summary listed.
REMOTE repos (caches of public registries) and VIRTUAL repos (aggregate views)
are not IDM-owned data and are intentionally excluded.

`discover` writes a live discovery report (report/discovery-report.{md,html,
body.html}) — the fresh, authoritative replacement for the stale
artifactory-package-summary files — and FAILS if any LOCAL repo isn't handled by
the migration config, so a newly added repo can never be silently dropped.
"""
from __future__ import annotations

import datetime as _dt
import html

from .context import Context
from .util import get_logger

log = get_logger()

# Artifactory packageType -> our handler/type name.
_HANDLER = {"Pypi": "pypi", "Docker": "docker", "Npm": "npm", "Generic": "data"}
_CLASSES = ["LOCAL", "REMOTE", "VIRTUAL"]


def _all_repos(ctx: Context) -> list[dict]:
    return ctx.art.get_json(f"{ctx.art.base}/api/repositories")


def list_local_repos(ctx: Context) -> list[dict]:
    """All LOCAL repositories (key + packageType), sorted by key."""
    return sorted((r for r in _all_repos(ctx) if r.get("type") == "LOCAL"),
                  key=lambda r: r.get("key", ""))


def _covered_repos(ctx: Context) -> dict[str, str]:
    """repo key -> handler type, as declared in migration.yaml."""
    out: dict[str, str] = {}
    types = ctx.cfg.types
    for name in ("pypi", "npm", "data"):
        repo = types.get(name, {}).get("repo")
        if repo:
            out[repo] = name
    for repo in types.get("docker", {}).get("repos", []):
        out[repo] = "docker"
    return out


def coverage(ctx: Context, local: list[dict] | None = None) -> dict:
    """Cross-reference live LOCAL repos against the config. Returns rows + gaps."""
    local = local if local is not None else list_local_repos(ctx)
    covered = _covered_repos(ctx)
    rows, uncovered = [], []
    for r in local:
        key, ptype = r["key"], r.get("packageType", "?")
        handler = _HANDLER.get(ptype)
        if key in covered:
            status = "covered"
        elif handler is None:
            status = f"UNCOVERED — unsupported package type ({ptype})"
            uncovered.append(key)
        else:
            status = f"UNCOVERED — add to types.{handler} in migration.yaml"
            uncovered.append(key)
        rows.append({"repo": key, "type": ptype, "handler": handler, "status": status})
    return {"local": rows, "uncovered": uncovered, "covered_count": len(covered)}


# ---- report data + renderers -------------------------------------------------
def build_discovery_data(ctx: Context) -> dict:
    repos = _all_repos(ctx)
    by_class: dict[str, list[dict]] = {c: [] for c in _CLASSES}
    for r in repos:
        by_class.setdefault(r.get("type", "?"), []).append(
            {"key": r.get("key", "?"), "package_type": r.get("packageType", "?")})
    for v in by_class.values():
        v.sort(key=lambda x: x["key"])
    local = [{"key": r["key"], "packageType": r["package_type"]} for r in by_class["LOCAL"]]
    return {
        "generated": _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "source": ctx.art.base,
        "github": ctx.cfg.github_slug,
        "total": len(repos),
        "by_class": by_class,
        "coverage": coverage(ctx, local),
    }


def render_markdown(d: dict) -> str:
    cov = d["coverage"]
    counts = " · ".join(f"{len(d['by_class'][c])} {c}" for c in _CLASSES)
    lines = [
        "# Artifactory live discovery",
        "",
        f"*Generated: {d['generated']} · Source: {d['source']}*",
        "",
        f"**{d['total']} repositories** ({counts}). Only **LOCAL** repos store "
        "IDM-owned artifacts and are migrated; REMOTE are public-registry caches and "
        "VIRTUAL are aggregate views — both excluded.",
        "",
    ]
    if cov["uncovered"]:
        lines.append(f"> ⚠️ **{len(cov['uncovered'])} LOCAL repo(s) UNCOVERED:** "
                     f"{', '.join(cov['uncovered'])}")
    else:
        lines.append(f"> ✅ **All {len(cov['local'])} LOCAL repositories are covered "
                     "by the migration.**")
    lines += ["", "## LOCAL repositories (migrated)", "",
              "| Repository | Package type | Handler | Coverage |",
              "|---|---|---|---|"]
    for row in cov["local"]:
        lines.append(f"| `{row['repo']}` | {row['type']} | {row['handler'] or '—'} "
                     f"| {row['status']} |")
    lines += ["", "## Full instance structure", ""]
    for c in _CLASSES:
        items = d["by_class"][c]
        lines.append(f"### {c} ({len(items)})")
        lines += [f"- `{it['key']}` ({it['package_type']})" for it in items] or ["- _(none)_"]
        lines.append("")
    return "\n".join(lines) + "\n"


def _render_body(d: dict) -> str:
    cov = d["coverage"]
    counts = " · ".join(f"{len(d['by_class'][c])} {c}" for c in _CLASSES)
    if cov["uncovered"]:
        banner = (f'<p class="warn">⚠️ {len(cov["uncovered"])} LOCAL repo(s) UNCOVERED: '
                  f'{html.escape(", ".join(cov["uncovered"]))}</p>')
    else:
        banner = (f'<p class="ok">✅ All {len(cov["local"])} LOCAL repositories are '
                  "covered by the migration.</p>")
    local_rows = "\n".join(
        f"<tr><td><code>{html.escape(r['repo'])}</code></td><td>{html.escape(r['type'])}</td>"
        f"<td>{html.escape(r['handler'] or '—')}</td><td>{html.escape(r['status'])}</td></tr>"
        for r in cov["local"])
    struct = []
    for c in _CLASSES:
        items = d["by_class"][c]
        lis = "".join(f"<li><code>{html.escape(it['key'])}</code> "
                      f"({html.escape(it['package_type'])})</li>" for it in items)
        struct.append(f"<h3>{c} ({len(items)})</h3><ul>{lis or '<li><i>none</i></li>'}</ul>")
    return (
        f"<h1>Artifactory live discovery</h1>\n"
        f'<p class="meta">Generated {html.escape(d["generated"])} · '
        f'<code>{html.escape(d["source"])}</code></p>\n'
        f"<p>{d['total']} repositories ({html.escape(counts)}). Only LOCAL repos store "
        "IDM-owned artifacts and are migrated.</p>\n"
        f"{banner}\n<h2>LOCAL repositories (migrated)</h2>\n"
        "<table><thead><tr><th>Repository</th><th>Package type</th><th>Handler</th>"
        f"<th>Coverage</th></tr></thead>\n<tbody>\n{local_rows}\n</tbody></table>\n"
        f"<h2>Full instance structure</h2>\n{''.join(struct)}")


def render_html_body(d: dict) -> str:
    return _STYLE + "\n" + _render_body(d)


def render_html(d: dict) -> str:
    return (f'<!DOCTYPE html>\n<html lang="en"><head><meta charset="utf-8">\n'
            f'<meta name="viewport" content="width=device-width, initial-scale=1">\n'
            f'<title>Artifactory live discovery — {html.escape(d["github"])}</title>\n'
            f'{_STYLE}</head>\n<body>\n{_render_body(d)}\n</body></html>\n')


_STYLE = """<style>
  body { font-family: system-ui, -apple-system, Segoe UI, Roboto, sans-serif;
         max-width: 54rem; margin: 2.5rem auto; padding: 0 1rem;
         line-height: 1.55; color: #1b1f24; }
  h1 { margin-bottom: .2rem; } .meta { color: #57606a; margin-top: 0; }
  table { border-collapse: collapse; width: 100%; margin: 1rem 0; }
  th, td { text-align: left; padding: .5rem .6rem;
           border-bottom: 1px solid #d0d7de; vertical-align: top; }
  th { background: #f6f8fa; }
  code { background: #f6f8fa; padding: .1em .35em; border-radius: 4px; font-size: .9em; }
  .ok { color: #116329; font-weight: 600; }
  .warn { color: #9a6700; font-weight: 600; }
  ul { columns: 2; }
</style>"""


def _pdf_safe(s: str) -> str:
    """Core-font PDFs are latin-1 only; replace the few non-latin-1 chars we emit."""
    for bad, good in (("✅", "[OK]"), ("⚠️", "[!]"), ("⚠", "[!]"),
                      ("—", "-"), ("→", "->")):
        s = s.replace(bad, good)
    return s.encode("latin-1", "ignore").decode("latin-1")


def write_pdf(d: dict, path) -> bool:
    """Render a simple PDF of the discovery report. Best-effort (needs fpdf2)."""
    try:
        from fpdf import FPDF
    except ImportError:
        log.warning("discover: fpdf2 not installed — skipping PDF (pip install fpdf2)")
        return False

    cov = d["coverage"]
    counts = " - ".join(f"{len(d['by_class'][c])} {c}" for c in _CLASSES)
    pdf = FPDF()
    pdf.set_auto_page_break(auto=True, margin=15)
    pdf.add_page()

    def line(text, size=10, style="", gap=5):
        pdf.set_font("Helvetica", style, size)
        pdf.set_x(pdf.l_margin)  # multi_cell leaves x at the right edge otherwise
        pdf.multi_cell(0, gap, _pdf_safe(text), new_x="LMARGIN", new_y="NEXT")

    line("Artifactory live discovery", size=16, style="B", gap=9)
    pdf.set_text_color(90)
    line(f"Generated {d['generated']}  -  {d['source']}", size=9)
    pdf.set_text_color(0)
    pdf.ln(1)
    line(f"{d['total']} repositories ({counts}). Only LOCAL repos store IDM-owned "
         "artifacts and are migrated; REMOTE are public-registry caches and VIRTUAL "
         "are aggregate views (both excluded).")
    pdf.ln(1)
    if cov["uncovered"]:
        line(f"[!] {len(cov['uncovered'])} LOCAL repo(s) UNCOVERED: "
             f"{', '.join(cov['uncovered'])}", style="B")
    else:
        line(f"[OK] All {len(cov['local'])} LOCAL repositories are covered.", style="B")

    pdf.ln(2)
    line("LOCAL repositories (migrated)", size=13, style="B", gap=7)
    for r in cov["local"]:
        line(f"  - {r['repo']}  ({r['type']} -> {r['handler'] or '-'})  {r['status']}", size=9)

    pdf.ln(2)
    line("Full instance structure", size=13, style="B", gap=7)
    for c in _CLASSES:
        items = d["by_class"][c]
        line(f"{c} ({len(items)})", size=10, style="B")
        for it in items:
            line(f"  - {it['key']}  ({it['package_type']})", size=9)
    pdf.output(str(path))
    return True


def generate(ctx: Context) -> dict:
    """Write report/discovery-report.{md,html,body.html,pdf}; return the data."""
    data = build_discovery_data(ctx)
    out_dir = ctx.cfg.root / "report"
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "discovery-report.md").write_text(render_markdown(data), encoding="utf-8")
    (out_dir / "discovery-report.html").write_text(render_html(data), encoding="utf-8")
    (out_dir / "discovery-report.body.html").write_text(render_html_body(data), encoding="utf-8")
    pdf_ok = write_pdf(data, out_dir / "discovery-report.pdf")
    exts = "md,html,body.html,pdf" if pdf_ok else "md,html,body.html"
    log.info("discover: wrote discovery-report.{%s} to %s", exts, out_dir)
    return data


def run(ctx: Context) -> None:
    """Generate the discovery report, log the audit, and FAIL if any repo is uncovered."""
    data = generate(ctx)
    cov = data["coverage"]
    log.info("LOCAL (IDM-owned) repositories — coverage audit:")
    for row in cov["local"]:
        mark = "[MISS]" if row["status"].startswith("UNCOVERED") else "[ OK ]"
        log.info("  %s %-26s %-8s %s", mark, row["repo"], row["type"], row["status"])
    if cov["uncovered"]:
        raise RuntimeError(
            f"{len(cov['uncovered'])} LOCAL repo(s) not covered by the migration: "
            f"{', '.join(cov['uncovered'])}. Add them to migration.yaml.")
    log.info("All %d LOCAL repositories are covered.", len(cov["local"]))
