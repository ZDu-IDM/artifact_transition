"""Generate a migration report (HTML + Markdown) from the inventories + state.

Read-only: it summarises what has been inventoried, published, and verified per
type using manifests/<type>-inventory.json and manifests/state.json. Safe to run
at any point — before a migration it simply shows 0 published.
"""
from __future__ import annotations

import datetime as _dt
import html
import json

from .context import Context
from .util import get_logger

log = get_logger()

_TYPES = ["pypi", "docker", "npm", "data"]
_TARGETS = {
    "pypi": "GitHub Releases + PEP 503 index (Pages)",
    "docker": "GitHub Container Registry (ghcr.io)",
    "npm": "GitHub Packages (npm)",
    "data": "GitHub Releases (one per folder)",
}


def _human_bytes(n: int) -> str:
    step = 1024.0
    val = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if val < step:
            return f"{val:.0f} {unit}" if unit == "B" else f"{val:.1f} {unit}"
        val /= step
    return f"{val:.1f} PB"


def build_report_data(ctx: Context) -> dict:
    """Collect per-type stats into a plain dict used by both renderers."""
    cap = ctx.cfg.options.get("max_asset_bytes", 2 << 30)
    out: dict = {
        "generated": _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "source": ctx.cfg.artifactory.base_url,
        "github": ctx.cfg.github_slug,
        "pages_url": ctx.cfg.pages_url,
        "types": {},
    }

    for t in _TYPES:
        path = ctx.inventory_path(t)
        entry: dict = {"target": _TARGETS[t], "progress": ctx.state.status_counts(t)}
        if not path.exists():
            entry["status"] = "not inventoried"
            out["types"][t] = entry
            continue
        inv = json.loads(path.read_text(encoding="utf-8"))
        entry["status"] = "inventoried"
        entry["source_repo"] = inv.get("source_repo") or ", ".join(inv.get("source_repos", []))
        if t == "pypi":
            entry.update(packages=inv["package_count"], files=inv["file_count"])
        elif t == "docker":
            imgs = inv["images"]
            entry.update(images=inv["image_count"],
                         tags=sum(len(m["tags"]) for m in imgs.values()))
        elif t == "npm":
            entry.update(packages=inv["package_count"],
                         renames={p: m["new_name"] for p, m in inv["packages"].items()})
        elif t == "data":
            files = inv["files"]
            total = sum(f["size"] for f in files)
            oversize = [f["path"] for f in files if f["size"] > cap]
            entry.update(files=inv["file_count"], folders=inv["folders"],
                         total_bytes=total, total_human=_human_bytes(total),
                         oversize=oversize)
        out["types"][t] = entry
    return out


# ---- Markdown ----------------------------------------------------------------
def render_markdown(d: dict) -> str:
    lines = [
        "# Migration report",
        "",
        f"*Generated: {d['generated']} · {d['source']} → {d['github']}*",
        "",
        "| Type | Source | Target | Inventory | Published |",
        "|---|---|---|---|---|",
    ]
    for t in _TYPES:
        e = d["types"][t]
        if e["status"] != "inventoried":
            lines.append(f"| **{t}** | — | {e['target']} | _{e['status']}_ | — |")
            continue
        inv_desc = _inv_desc(t, e)
        pub = e["progress"].get("published", 0) + e["progress"].get("verified", 0)
        lines.append(f"| **{t}** | `{e['source_repo']}` | {e['target']} | {inv_desc} | {pub} |")

    for t in _TYPES:
        e = d["types"][t]
        if e["status"] != "inventoried":
            continue
        lines += ["", f"## {t}", "", f"- Target: {e['target']}",
                  f"- Progress (state.json): {e['progress'] or 'nothing yet'}"]
        if t == "npm" and e.get("renames"):
            lines.append("- Renames (scope must match owner):")
            lines += [f"  - `{old}` → `{new}`" for old, new in e["renames"].items()]
        if t == "data":
            lines.append(f"- Total size: {e['total_human']} across {e['files']} files")
            if e["oversize"]:
                lines.append(f"- Split (>2 GiB): {', '.join(e['oversize'])}")
    return "\n".join(lines) + "\n"


def _inv_desc(t: str, e: dict) -> str:
    if t == "pypi":
        return f"{e['packages']} pkgs / {e['files']} files"
    if t == "docker":
        return f"{e['images']} images / {e['tags']} tags"
    if t == "npm":
        return f"{e['packages']} pkgs"
    if t == "data":
        return f"{e['files']} files / {e['total_human']}"
    return ""


# ---- HTML --------------------------------------------------------------------
def _render_body(d: dict) -> str:
    rows = []
    for t in _TYPES:
        e = d["types"][t]
        if e["status"] != "inventoried":
            rows.append(f"<tr><td><b>{t}</b></td><td>—</td><td>{html.escape(e['target'])}</td>"
                        f"<td><i>{e['status']}</i></td><td>—</td></tr>")
            continue
        pub = e["progress"].get("published", 0) + e["progress"].get("verified", 0)
        rows.append(
            f"<tr><td><b>{t}</b></td><td><code>{html.escape(e['source_repo'])}</code></td>"
            f"<td>{html.escape(e['target'])}</td><td>{html.escape(_inv_desc(t, e))}</td>"
            f"<td>{pub}</td></tr>")

    sections = []
    for t in _TYPES:
        e = d["types"][t]
        if e["status"] != "inventoried":
            continue
        prog = html.escape(str(e["progress"] or "nothing published yet"))
        body = [f"<p><b>Target:</b> {html.escape(e['target'])}<br><b>Progress:</b> {prog}</p>"]
        if t == "npm" and e.get("renames"):
            items = "".join(
                f"<li><code>{html.escape(o)}</code> → <code>{html.escape(n)}</code></li>"
                for o, n in e["renames"].items())
            body.append(f"<p>Renames:</p><ul>{items}</ul>")
        if t == "data":
            body.append(f"<p>Total size: <b>{e['total_human']}</b> across {e['files']} files.</p>")
            if e["oversize"]:
                lis = "".join(f"<li><code>{html.escape(p)}</code> (auto-split)</li>"
                              for p in e["oversize"])
                body.append(f"<p>Over the 2 GiB cap:</p><ul>{lis}</ul>")
        sections.append(f"<section><h2>{t}</h2>{''.join(body)}</section>")

    gh = html.escape(d["github"])
    pages = html.escape(d["pages_url"])
    head = (f'<p class="meta">Generated {html.escape(d["generated"])} · '
            f'<code>{html.escape(d["source"])}</code> → '
            f'<a href="https://github.com/{gh}">{gh}</a></p>')
    thead = ("<thead><tr><th>Type</th><th>Source</th><th>Target</th>"
             "<th>Inventory</th><th>Published</th></tr></thead>")
    return (f"<h1>Migration report</h1>\n{head}\n"
            f"<table>\n{thead}\n<tbody>\n" + "\n".join(rows) + "\n</tbody>\n</table>\n"
            f'<p>PyPI index: <a href="{pages}/simple/">{pages}/simple/</a></p>\n'
            + "\n".join(sections))


def render_html_body(d: dict) -> str:
    """Style block + body content only (for embedding, e.g. a claude.ai Artifact)."""
    return _STYLE + "\n" + _render_body(d)


def render_html(d: dict) -> str:
    """A complete, standalone HTML document."""
    return (f'<!DOCTYPE html>\n<html lang="en"><head><meta charset="utf-8">\n'
            f'<meta name="viewport" content="width=device-width, initial-scale=1">\n'
            f'<title>Migration report — {html.escape(d["github"])}</title>\n'
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
  section { border: 1px solid #d0d7de; border-radius: 8px;
            padding: .3rem 1rem 1rem; margin: 1rem 0; }
  section h2 { text-transform: capitalize; }
  a { color: #0969da; }
</style>"""


def generate(ctx: Context) -> dict:
    """Write report/migration-report.{html,md}; return the report data."""
    data = build_report_data(ctx)
    out_dir = ctx.cfg.root / "report"
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "migration-report.html").write_text(render_html(data), encoding="utf-8")
    (out_dir / "migration-report.body.html").write_text(render_html_body(data), encoding="utf-8")
    (out_dir / "migration-report.md").write_text(render_markdown(data), encoding="utf-8")
    log.info("report: wrote migration-report.{html,body.html,md} to %s", out_dir)
    return data
