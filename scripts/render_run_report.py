from __future__ import annotations

import argparse
import html
import json
from pathlib import Path
from typing import Any


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def read_text(path: Path) -> str:
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8").strip()


def pct(value: float) -> str:
    return f"{value * 100:.1f}%"


def badge_class(decision: str) -> str:
    return "keep" if decision == "keep" else "discard"


def build_markdown(compare: dict[str, Any], baseline_text: str, challenger_text: str) -> str:
    baseline = compare["baseline"]
    challenger = compare["challenger"]
    baseline_breakdown = baseline.get("rule_breakdown") or {}
    challenger_breakdown = challenger.get("rule_breakdown") or {}
    lines: list[str] = []
    lines.append(f"# Optimization Report")
    lines.append("")
    lines.append(f"- Decision: `{compare['decision']}`")
    lines.append(f"- Winner: `{compare['winner']}`")
    lines.append(f"- Reason: {compare['reason']}")
    lines.append(f"- Delta: `{compare['delta']}`")
    lines.append(f"- Margin: `{compare['margin']}`")
    lines.append("")
    lines.append("## Score Summary")
    lines.append("")
    lines.append(f"- Baseline final score: `{baseline['final_score']}`")
    lines.append(f"- Challenger final score: `{challenger['final_score']}`")
    lines.append(f"- Baseline model score: `{baseline['model_score']}`")
    lines.append(f"- Challenger model score: `{challenger['model_score']}`")
    lines.append(f"- Baseline rule score: `{baseline['rule_score']}`")
    lines.append(f"- Challenger rule score: `{challenger['rule_score']}`")
    lines.append("")
    lines.append("## Baseline")
    lines.append("")
    lines.append("```text")
    lines.append(baseline_text)
    lines.append("```")
    lines.append("")
    lines.append("## Challenger")
    lines.append("")
    lines.append("```text")
    lines.append(challenger_text)
    lines.append("```")
    lines.append("")
    lines.append("## Baseline Notes")
    lines.append("")
    if baseline.get("notes"):
        lines.extend(f"- {note}" for note in baseline["notes"])
    else:
        lines.append("- None")
    lines.append("")
    lines.append("## Challenger Notes")
    lines.append("")
    if challenger.get("notes"):
        lines.extend(f"- {note}" for note in challenger["notes"])
    else:
        lines.append("- None")
    lines.append("")
    lines.append("## Rule Breakdown")
    lines.append("")
    lines.append("| Metric | Baseline | Challenger |")
    lines.append("| --- | ---: | ---: |")
    keys = list(dict.fromkeys(list(baseline_breakdown.keys()) + list(challenger_breakdown.keys())))
    for key in keys:
        lines.append(
            f"| {key} | {float(baseline_breakdown.get(key, 0.0)):.6f} | {float(challenger_breakdown.get(key, 0.0)):.6f} |",
        )
    lines.append("")
    return "\n".join(lines)


def build_score_bar(value: float, color: str) -> str:
    width = max(2, min(100, int(round(value * 100))))
    return (
        '<div class="bar-track">'
        f'<div class="bar-fill {color}" style="width:{width}%"></div>'
        "</div>"
    )


def build_html(compare: dict[str, Any], baseline_text: str, challenger_text: str) -> str:
    baseline = compare["baseline"]
    challenger = compare["challenger"]
    decision = compare["decision"]
    baseline_breakdown = baseline.get("rule_breakdown") or {}
    challenger_breakdown = challenger.get("rule_breakdown") or {}
    rows = []
    keys = list(dict.fromkeys(list(baseline_breakdown.keys()) + list(challenger_breakdown.keys())))
    for key in keys:
        rows.append(
            "<tr>"
            f"<td>{html.escape(str(key))}</td>"
            f"<td>{float(baseline_breakdown.get(key, 0.0)):.6f}</td>"
            f"<td>{float(challenger_breakdown.get(key, 0.0)):.6f}</td>"
            "</tr>",
        )
    baseline_notes = "".join(
        f"<li>{html.escape(note)}</li>" for note in baseline["notes"]
    ) or "<li>None</li>"
    challenger_notes = "".join(
        f"<li>{html.escape(note)}</li>" for note in challenger["notes"]
    ) or "<li>None</li>"
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Optimization Report</title>
  <style>
    :root {{
      --bg: #f5f1e8;
      --panel: #fffaf0;
      --ink: #1a1a17;
      --muted: #5c584f;
      --line: #d9d1c0;
      --accent: #183153;
      --green: #275d38;
      --red: #903030;
      --gold: #b2862f;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      padding: 32px;
      background:
        radial-gradient(circle at top left, rgba(24,49,83,0.08), transparent 32%),
        radial-gradient(circle at bottom right, rgba(178,134,47,0.10), transparent 28%),
        var(--bg);
      color: var(--ink);
      font: 15px/1.6 "Iowan Old Style", "Palatino Linotype", serif;
    }}
    .shell {{
      max-width: 1160px;
      margin: 0 auto;
      display: grid;
      gap: 20px;
    }}
    .hero, .panel {{
      background: rgba(255,250,240,0.92);
      border: 1px solid var(--line);
      border-radius: 18px;
      box-shadow: 0 14px 34px rgba(24, 28, 34, 0.08);
      backdrop-filter: blur(10px);
    }}
    .hero {{
      padding: 28px;
      display: grid;
      gap: 12px;
    }}
    .eyebrow {{
      color: var(--muted);
      text-transform: uppercase;
      letter-spacing: 0.12em;
      font-size: 12px;
    }}
    h1 {{
      margin: 0;
      font-size: clamp(30px, 5vw, 54px);
      line-height: 1.05;
    }}
    .meta {{
      display: flex;
      flex-wrap: wrap;
      gap: 12px;
      color: var(--muted);
    }}
    .badge {{
      display: inline-flex;
      align-items: center;
      gap: 8px;
      width: fit-content;
      padding: 7px 12px;
      border-radius: 999px;
      font-weight: 700;
      font-size: 13px;
    }}
    .badge.keep {{
      background: rgba(39,93,56,0.12);
      color: var(--green);
      border: 1px solid rgba(39,93,56,0.25);
    }}
    .badge.discard {{
      background: rgba(144,48,48,0.10);
      color: var(--red);
      border: 1px solid rgba(144,48,48,0.22);
    }}
    .grid {{
      display: grid;
      gap: 20px;
    }}
    .grid.metrics {{
      grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
    }}
    .metric {{
      padding: 18px;
    }}
    .metric .label {{
      color: var(--muted);
      font-size: 12px;
      text-transform: uppercase;
      letter-spacing: 0.08em;
    }}
    .metric .value {{
      margin-top: 8px;
      font-size: 34px;
      line-height: 1;
      font-weight: 700;
    }}
    .comparison {{
      grid-template-columns: repeat(auto-fit, minmax(320px, 1fr));
    }}
    .panel {{
      padding: 22px;
    }}
    .panel h2, .panel h3 {{
      margin-top: 0;
      margin-bottom: 10px;
    }}
    .copy {{
      white-space: pre-wrap;
      background: rgba(24,49,83,0.05);
      border: 1px solid rgba(24,49,83,0.08);
      border-radius: 12px;
      padding: 16px;
      min-height: 140px;
    }}
    .score-line {{
      display: grid;
      gap: 6px;
      margin-bottom: 14px;
    }}
    .score-head {{
      display: flex;
      justify-content: space-between;
      gap: 12px;
      font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
      font-size: 13px;
    }}
    .bar-track {{
      height: 10px;
      border-radius: 999px;
      background: rgba(24,49,83,0.08);
      overflow: hidden;
    }}
    .bar-fill {{
      height: 100%;
      border-radius: 999px;
    }}
    .bar-fill.blue {{ background: linear-gradient(90deg, #204c8c, #3e74c5); }}
    .bar-fill.gold {{ background: linear-gradient(90deg, #9c7425, #c9a84d); }}
    .notes {{
      margin: 0;
      padding-left: 18px;
    }}
    table {{
      width: 100%;
      border-collapse: collapse;
      font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
      font-size: 13px;
    }}
    th, td {{
      border-top: 1px solid var(--line);
      padding: 10px 8px;
      text-align: left;
    }}
    th {{
      color: var(--muted);
      font-weight: 700;
      font-size: 12px;
      text-transform: uppercase;
      letter-spacing: 0.08em;
    }}
    .footer {{
      color: var(--muted);
      font-size: 12px;
    }}
  </style>
</head>
<body>
  <main class="shell">
    <section class="hero">
      <div class="eyebrow">ZH Communication Evolver</div>
      <h1>Optimization Report</h1>
      <div class="badge {badge_class(decision)}">{html.escape(decision.upper())} · {html.escape(compare['winner'])}</div>
      <div class="meta">
        <span>Reason: {html.escape(compare['reason'])}</span>
        <span>Delta: {compare['delta']}</span>
        <span>Margin: {compare['margin']}</span>
      </div>
    </section>

    <section class="grid metrics">
      <div class="panel metric">
        <div class="label">Baseline Score</div>
        <div class="value">{baseline['final_score']:.6f}</div>
      </div>
      <div class="panel metric">
        <div class="label">Challenger Score</div>
        <div class="value">{challenger['final_score']:.6f}</div>
      </div>
      <div class="panel metric">
        <div class="label">Score Delta</div>
        <div class="value">{compare['delta']:.6f}</div>
      </div>
      <div class="panel metric">
        <div class="label">Decision</div>
        <div class="value">{html.escape(compare['decision'])}</div>
      </div>
    </section>

    <section class="grid comparison">
      <article class="panel">
        <h2>Baseline</h2>
        <div class="score-line">
          <div class="score-head"><span>Final</span><span>{pct(baseline['final_score'])}</span></div>
          {build_score_bar(baseline['final_score'], 'blue')}
        </div>
        <div class="score-line">
          <div class="score-head"><span>Model</span><span>{pct(baseline['model_score'])}</span></div>
          {build_score_bar(baseline['model_score'], 'gold')}
        </div>
        <div class="score-line">
          <div class="score-head"><span>Rules</span><span>{pct(baseline['rule_score'])}</span></div>
          {build_score_bar(baseline['rule_score'], 'gold')}
        </div>
        <div class="copy">{html.escape(baseline_text)}</div>
        <h3>Notes</h3>
        <ul class="notes">{baseline_notes}</ul>
      </article>

      <article class="panel">
        <h2>Challenger</h2>
        <div class="score-line">
          <div class="score-head"><span>Final</span><span>{pct(challenger['final_score'])}</span></div>
          {build_score_bar(challenger['final_score'], 'blue')}
        </div>
        <div class="score-line">
          <div class="score-head"><span>Model</span><span>{pct(challenger['model_score'])}</span></div>
          {build_score_bar(challenger['model_score'], 'gold')}
        </div>
        <div class="score-line">
          <div class="score-head"><span>Rules</span><span>{pct(challenger['rule_score'])}</span></div>
          {build_score_bar(challenger['rule_score'], 'gold')}
        </div>
        <div class="copy">{html.escape(challenger_text)}</div>
        <h3>Notes</h3>
        <ul class="notes">{challenger_notes}</ul>
      </article>
    </section>

    <section class="panel">
      <h2>Rule Breakdown</h2>
      <table>
        <thead>
          <tr><th>Metric</th><th>Baseline</th><th>Challenger</th></tr>
        </thead>
        <tbody>
          {''.join(rows)}
        </tbody>
      </table>
    </section>

    <section class="panel footer">
      <div>Spec: {html.escape(compare['spec_path'])}</div>
      <div>Baseline file: {html.escape(compare['baseline']['path'])}</div>
      <div>Challenger file: {html.escape(compare['challenger']['path'])}</div>
    </section>
  </main>
</body>
</html>
"""


def main() -> None:
    parser = argparse.ArgumentParser(description="Render markdown and HTML for one optimization run.")
    parser.add_argument("--run-dir", required=True, type=Path)
    args = parser.parse_args()

    run_dir = args.run_dir.resolve()
    compare_path = run_dir / "compare-result.json"
    baseline_path = run_dir / "baseline.txt"
    challenger_path = run_dir / "challenger.txt"
    if not compare_path.exists():
        raise FileNotFoundError(f"Missing compare-result.json in {run_dir}")

    compare = load_json(compare_path)
    baseline_text = read_text(baseline_path)
    challenger_text = read_text(challenger_path)

    md = build_markdown(compare, baseline_text, challenger_text)
    html_doc = build_html(compare, baseline_text, challenger_text)

    md_path = run_dir / "report.md"
    html_path = run_dir / "report.html"
    md_path.write_text(md, encoding="utf-8")
    html_path.write_text(html_doc, encoding="utf-8")

    print(
        json.dumps(
            {
                "run_dir": str(run_dir),
                "markdown_report": str(md_path),
                "html_report": str(html_path),
            },
            ensure_ascii=False,
            indent=2,
        ),
    )


if __name__ == "__main__":
    main()
