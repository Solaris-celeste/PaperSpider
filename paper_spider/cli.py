from __future__ import annotations

import argparse
from datetime import date, datetime, time, timezone
from pathlib import Path
import sys

from .config import load_settings
from .pipeline import (
    classify, enrich_semantic_scholar, enrich_social_signals, fetch_arxiv,
    load_seen, rank, render_report, report_window, save_seen, summarize_with_llm,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate a twice-weekly AI paper Markdown brief.")
    parser.add_argument("--date", type=date.fromisoformat, default=date.today(), help="Scheduled issue date (Monday or Thursday, YYYY-MM-DD)")
    parser.add_argument("--start", type=date.fromisoformat, help="One-off report start date (YYYY-MM-DD; requires --end)")
    parser.add_argument("--end", type=date.fromisoformat, help="One-off report end date (YYYY-MM-DD; requires --start)")
    parser.add_argument("--config", default="config.toml")
    parser.add_argument("--output-dir", type=Path, default=Path("reports"))
    parser.add_argument("--state-file", type=Path, default=Path("data/seen.json"))
    parser.add_argument("--include-seen", action="store_true", help="Allow papers included in earlier issues")
    parser.add_argument("--no-ai", action="store_true", help="Use extractive fallback instead of calling an LLM")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if bool(args.start) != bool(args.end):
        print("error: --start and --end must be used together", file=sys.stderr)
        return 2
    if args.start and args.end:
        if args.start > args.end:
            print("error: --start cannot be after --end", file=sys.stderr)
            return 2
        start = datetime.combine(args.start, time.min, timezone.utc)
        end = datetime.combine(args.end, time.max, timezone.utc)
    else:
        try:
            start, end = report_window(args.date)
        except ValueError as error:
            print(f"error: {error}", file=sys.stderr)
            return 2
    settings = load_settings(args.config)
    try:
        papers = fetch_arxiv(settings, start, end)
    except Exception as error:  # A report should fail loudly when its primary source is unavailable.
        print(f"error: arXiv collection failed: {error}", file=sys.stderr)
        return 1
    for paper in papers:
        classify(paper, settings)
    seen = load_seen(args.state_file)
    candidates = [paper for paper in papers if args.include_seen or paper.arxiv_id not in seen]
    candidates = sorted(candidates, key=lambda paper: (paper.relevance, paper.published), reverse=True)[:30]
    enrich_semantic_scholar(candidates)
    enrich_social_signals(candidates)
    selected = rank(candidates, settings.target_papers)
    if args.no_ai:
        for paper in selected:
            from .pipeline import fallback_summary
            paper.summary = fallback_summary(paper)
    else:
        summarize_with_llm(selected)
    report = render_report(selected, start, end)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    destination = args.output_dir / f"{end:%Y-%m-%d}.md"
    destination.write_text(report, encoding="utf-8")
    save_seen(args.state_file, selected)
    print(f"Wrote {len(selected)} papers to {destination}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
