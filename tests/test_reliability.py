from datetime import date, datetime, timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from paper_spider.cli import main
from paper_spider.config import Settings
from paper_spider.models import Paper
from paper_spider.pipeline import collect_papers, fetch_huggingface, latest_issue_day, load_seen, rank

START = datetime(2026, 9, 1, tzinfo=timezone.utc)
END = datetime(2026, 9, 3, 23, 59, tzinfo=timezone.utc)
SETTINGS = Settings(10, 60, ['cs.AI'], [], ['language model'])


def paper(identifier='2609.00001', title='A language model'):
    return Paper(identifier, title, 'We introduce a language model. It improves reasoning.',
                 'https://arxiv.org/abs/' + identifier, START, START, [], [])


class ReliabilityTest(unittest.TestCase):
    def test_secondary_source_survives_arxiv_outage(self):
        with patch('paper_spider.pipeline.fetch_arxiv', side_effect=TimeoutError), patch(
                'paper_spider.pipeline.fetch_huggingface', return_value=[paper()]):
            papers, warnings = collect_papers(SETTINGS, START, END)
        self.assertEqual(len(papers), 1)
        self.assertIn('arXiv', warnings[0])

    def test_merge_versions_and_signals(self):
        a, b = paper('2609.00001v2'), paper()
        b.sources = ['Hugging Face Daily Papers']
        b.community_votes = 20
        with patch('paper_spider.pipeline.fetch_arxiv', return_value=[a]), patch(
                'paper_spider.pipeline.fetch_huggingface', return_value=[b]):
            papers, _ = collect_papers(SETTINGS, START, END)
        self.assertEqual(len(papers), 1)
        self.assertEqual(papers[0].arxiv_id, '2609.00001')
        self.assertEqual(papers[0].community_votes, 20)
        self.assertEqual(len(papers[0].sources), 2)

    def test_hf_rejects_old_promoted_papers_and_malformed_entries(self):
        def entry(identifier, published):
            return {'paper': {'id': identifier, 'title': 'New model', 'summary': 'Abstract.', 'publishedAt': published}}
        items = [entry('2609.00001', '2026-09-01T00:00:00Z'),
                 entry('2401.00001', '2024-01-01T00:00:00Z'), {}]
        with patch('paper_spider.pipeline._request_json', return_value=items):
            papers = fetch_huggingface(SETTINGS, START, START)
        self.assertEqual([p.arxiv_id for p in papers], ['2609.00001'])

    def test_rank_includes_other_fields(self):
        papers = [paper(str(i), str(i)) for i in range(6)]
        for item in papers:
            item.topic = '大模型'
            item.relevance = 4
        papers[-1].topic = '强化学习'
        papers[-1].relevance = 1
        self.assertIn(papers[-1], rank(papers, 3))

    def test_seen_migrates_versioned_identifiers(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'seen.json'
            path.write_text(json.dumps(['2609.00001v1']))
            self.assertEqual(load_seen(path), {'2609.00001'})

    def test_retry_dates_map_to_same_issue(self):
        self.assertEqual(latest_issue_day(date(2026, 9, 4)), date(2026, 9, 3))
        self.assertEqual(latest_issue_day(date(2026, 9, 8)), date(2026, 9, 7))

    def test_empty_collection_fails_without_writing(self):
        with tempfile.TemporaryDirectory() as directory:
            output, state = Path(directory) / 'reports', Path(directory) / 'seen.json'
            state.write_text('["2608.00001"]')
            with patch('sys.argv', ['paper_spider', '--date', '2026-09-03', '--output-dir', str(output),
                                    '--state-file', str(state), '--no-ai']), patch(
                    'paper_spider.cli.collect_papers', return_value=([], ['outage'])):
                self.assertEqual(main(), 1)
            self.assertFalse(output.exists())
            self.assertEqual(json.loads(state.read_text()), ['2608.00001'])

    def test_successful_issue_is_idempotent(self):
        with tempfile.TemporaryDirectory() as directory:
            output, state = Path(directory) / 'reports', Path(directory) / 'seen.json'
            argv = ['paper_spider', '--date', '2026-09-03', '--scheduled', '--output-dir', str(output),
                    '--state-file', str(state), '--no-ai']
            with patch('sys.argv', argv), patch('paper_spider.cli.collect_papers', return_value=([paper()], [])) as collect, patch(
                    'paper_spider.cli.enrich_semantic_scholar'), patch('paper_spider.cli.enrich_social_signals'):
                self.assertEqual(main(), 0)
                self.assertEqual(main(), 0)
                self.assertEqual(collect.call_count, 1)
            report = (output / '2026-09-03.md').read_text()
            self.assertIn('**领域**', report)
            self.assertIn('**摘要**', report)
            self.assertEqual(load_seen(state), {'2609.00001'})
