from datetime import date, datetime, timezone
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import unquote_plus

from paper_spider.config import Settings
from paper_spider.models import Paper
from paper_spider.pipeline import fetch_arxiv, load_seen, rank, report_window, save_seen


def make_paper(title: str, score_hint: int = 0) -> Paper:
    paper = Paper("2601.00001", title, "", "https://arxiv.org/abs/2601.00001", datetime.now(timezone.utc), datetime.now(timezone.utc), [], [])
    paper.relevance = score_hint
    return paper


class PipelineTest(unittest.TestCase):
    def test_monday_window_starts_previous_thursday(self):
        start, end = report_window(date(2026, 8, 17))
        self.assertEqual(start, datetime(2026, 8, 13, tzinfo=timezone.utc))
        self.assertEqual(end.date(), date(2026, 8, 17))

    def test_thursday_window_starts_previous_monday(self):
        start, end = report_window(date(2026, 8, 13))
        self.assertEqual(start.date(), date(2026, 8, 10))
        self.assertEqual(end.date(), date(2026, 8, 13))

    def test_window_rejects_non_issue_days(self):
        with self.assertRaises(ValueError):
            report_window(date(2026, 8, 14))

    def test_rank_assigns_relative_stars(self):
        papers = [make_paper("lower", 1), make_paper("higher", 4)]
        selected = rank(papers, 5)
        self.assertEqual(selected[0].title, "higher")
        self.assertEqual(selected[0].stars, 5)
        self.assertEqual(selected[-1].stars, 1)

    def test_seen_state_is_additive_when_an_issue_is_empty(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "seen.json"
            save_seen(state, [make_paper("first")])
            save_seen(state, [])
            self.assertEqual(load_seen(state), {"2601.00001"})

    def test_arxiv_query_uses_the_report_window(self):
        settings = Settings(10, 60, ["cs.AI"], [], [])
        empty_feed = b'<feed xmlns="http://www.w3.org/2005/Atom" />'
        with patch("paper_spider.pipeline._request_arxiv", return_value=empty_feed) as request:
            fetch_arxiv(settings, datetime(2026, 8, 13, tzinfo=timezone.utc), datetime(2026, 8, 17, 23, 59, tzinfo=timezone.utc))
        self.assertIn("submittedDate:[202608130000 TO 202608172359]", unquote_plus(request.call_args.args[0]))
