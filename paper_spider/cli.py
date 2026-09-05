from __future__ import annotations

import argparse
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
import sys

from .config import load_settings
from .pipeline import (
    classify, enrich_semantic_scholar, enrich_social_signals, fallback_summary,
    collect_papers, latest_issue_day, load_seen, rank, render_report, report_window, save_seen, summarize_with_llm,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate a twice-weekly AI paper Markdown brief.")
    parser.add_argument("--date", type=date.fromisoformat, default=datetime.now(timezone(timedelta(hours=8))).date(), help="Issue date (YYYY-MM-DD); defaults to the latest Monday/Thursday")
    parser.add_argument("--start", type=date.fromisoformat, help="One-off report start date (YYYY-MM-DD; requires --end)")
    parser.add_argument("--end", type=date.fromisoformat, help="One-off report end date (YYYY-MM-DD; requires --start)")
    parser.add_argument("--config", default="config.toml")
    parser.add_argument("--output-dir", type=Path, default=Path("reports"))
    parser.add_argument("--state-file", type=Path, default=Path("data/seen.json"))
    parser.add_argument("--include-seen", action="store_true", help="Allow papers included in earlier issues")
    parser.add_argument("--scheduled", action="store_true", help="Use latest Monday/Thursday issue; skip an existing report")
    parser.add_argument("--no-ai", action="store_true", help="Use extractive fallback instead of calling an LLM")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.scheduled and (args.start or args.end):
        print("error: --scheduled cannot be combined with --start/--end", file=sys.stderr)
        return 2
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
            start, end = report_window(latest_issue_day(args.date))
        except ValueError as error:
            print(f"error: {error}", file=sys.stderr)
            return 2
    if args.scheduled:
        issue_day = latest_issue_day(args.date)
        end = min(datetime.combine(issue_day, time.max, timezone.utc), datetime.now(timezone.utc))
        start = datetime.combine(issue_day - timedelta(days=6), time.min, timezone.utc)
    else:
        issue_day = end.date()
    destination = args.output_dir / f"{issue_day:%Y-%m-%d}.md"
    if args.scheduled and destination.exists() and "### 1." in destination.read_text(encoding="utf-8"):
        print(f"Issue already published: {destination}")
        return 0
    settings = load_settings(args.config)
    try:
        papers, warnings = collect_papers(settings, start, end)
    except Exception as error:
        print(f"error: collection failed: {error}", file=sys.stderr)
        return 1
    for paper in papers:
        classify(paper, settings)
    seen = load_seen(args.state_file)
    candidates = [paper for paper in papers if args.include_seen or paper.arxiv_id not in seen]
    candidates = [paper for paper in candidates if paper.relevance > 0 or paper.topic != "其他 AI"]
    candidates = rank(candidates, 40)
    enrich_semantic_scholar(candidates)
    enrich_social_signals(candidates)
    selected = rank(candidates, settings.target_papers)
    if not selected:
        print("error: no unseen recent AI papers; report and state left untouched", file=sys.stderr)
        return 1
    if args.no_ai:
        for paper in selected:
            paper.summary = fallback_summary(paper)
    else:
        summarize_with_llm(selected)
    report = render_report(selected, start, end)
    if warnings:
        report += "\n## 来源状态\n\n" + "\n".join(f"- {warning}" for warning in warnings) + "\n"
    if len(selected) < 5:
        report += "\n> 本期新论文不足 5 篇，仅列出实际获取且未推荐的论文。\n"
    args.output_dir.mkdir(parents=True, exist_ok=True)
    destination.write_text(report, encoding="utf-8")
    save_seen(args.state_file, selected)
    print(f"Wrote {len(selected)} papers to {destination}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
