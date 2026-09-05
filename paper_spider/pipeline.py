"""Collection and editorial pipeline. Every network integration is optional."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, time, timedelta, timezone
import json
import math
import os
from pathlib import Path
import re
import sys
import time as clock
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
import xml.etree.ElementTree as ET

from .config import Settings
from .models import Paper

ARXIV_API = "https://export.arxiv.org/api/query"
S2_BATCH_API = "https://api.semanticscholar.org/graph/v1/paper/batch"
GITHUB_SEARCH_API = "https://api.github.com/search/repositories"
GITHUB_REPOSITORY_API = "https://api.github.com/repos"
HN_SEARCH_API = "https://hn.algolia.com/api/v1/search"
ATOM = "{http://www.w3.org/2005/Atom}"
ARXIV = "{http://arxiv.org/schemas/atom}"

TOPIC_RULES = (
    ("大模型 / 智能体", ("large language", "language model", "llm", "reasoning", "agent", "foundation model")),
    ("生成模型", ("generative", "diffusion", "flow matching", "image generation", "video generation")),
    ("强化学习", ("reinforcement learning", "rlhf", "policy optimization", "offline rl")),
    ("世界模型 / 具身智能", ("world model", "model-based", "robot", "embodied", "planning")),
    ("多模态", ("multimodal", "vision-language", "visual language", "audio-language")),
    ("计算机视觉", ("computer vision", "segmentation", "detection", "3d vision")),
    ("自然语言处理", ("natural language", "machine translation", "retrieval", "speech")),
)


def report_window(report_day: date) -> tuple[datetime, datetime]:
    """Return the inclusive period since the preceding Monday/Thursday issue."""
    weekday = report_day.weekday()
    if weekday not in (0, 3):
        raise ValueError("report date must be a Monday (0) or Thursday (3)")
    days_since_previous_issue = 4 if weekday == 0 else 3
    start = datetime.combine(report_day - timedelta(days=days_since_previous_issue), time.min, timezone.utc)
    end = datetime.combine(report_day, time.max, timezone.utc)
    return start, end


def _request_json(url: str, *, headers: dict[str, str] | None = None, body: Any = None) -> Any:
    payload = json.dumps(body).encode() if body is not None else None
    request = Request(url, data=payload, headers=headers or {}, method="POST" if body is not None else "GET")
    with urlopen(request, timeout=25) as response:  # nosec B310: URLs are fixed API endpoints
        return json.loads(response.read().decode())


def _request_arxiv(url: str) -> bytes:
    """Use a small backoff budget for arXiv's occasionally busy public API."""
    request = Request(url, headers={"User-Agent": "PaperSpider/0.1 (research brief)"})
    for attempt, delay in enumerate((5, 15, 0)):
        try:
            with urlopen(request, timeout=30) as response:  # nosec B310: fixed endpoint
                return response.read()
        except HTTPError as error:
            if error.code not in (429, 500, 502, 503, 504) or attempt == 2:
                raise
        except (TimeoutError, URLError):
            if attempt == 2:
                raise
        clock.sleep(delay)
    raise RuntimeError("unreachable")


def _clean(value: str | None) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def fetch_arxiv(settings: Settings, start: datetime, end: datetime) -> list[Paper]:
    categories = " OR ".join(f"cat:{category}" for category in settings.arxiv_categories)
    date_range = f"submittedDate:[{start:%Y%m%d%H%M} TO {end:%Y%m%d%H%M}]"
    params = urlencode({"search_query": f"({categories}) AND {date_range}", "start": 0, "max_results": settings.candidate_limit, "sortBy": "submittedDate", "sortOrder": "descending"})
    try:
        response = _request_arxiv(f"{ARXIV_API}?{params}")
    except HTTPError:
        # The documented date filter can intermittently return HTTP 500. Retain a
        # bounded newest-slice fallback so a scheduled issue can still be produced.
        fallback = urlencode({"search_query": f"({categories})", "start": 0, "max_results": settings.candidate_limit, "sortBy": "submittedDate", "sortOrder": "descending"})
        response = _request_arxiv(f"{ARXIV_API}?{fallback}")
    root = ET.fromstring(response)

    papers: list[Paper] = []
    for entry in root.findall(f"{ATOM}entry"):
        published = datetime.fromisoformat(entry.findtext(f"{ATOM}published", "").replace("Z", "+00:00"))
        updated = datetime.fromisoformat(entry.findtext(f"{ATOM}updated", "").replace("Z", "+00:00"))
        url = entry.findtext(f"{ATOM}id", "")
        paper_id = canonical_id(url.rsplit("/", 1)[-1])
        papers.append(Paper(
            arxiv_id=paper_id,
            title=_clean(entry.findtext(f"{ATOM}title")),
            abstract=_clean(entry.findtext(f"{ATOM}summary")),
            url=url.replace("http://", "https://"),
            published=published,
            updated=updated,
            authors=[_clean(author.findtext(f"{ATOM}name")) for author in entry.findall(f"{ATOM}author")],
            categories=[node.attrib["term"] for node in entry.findall(f"{ATOM}category")],
            comment=_clean(entry.findtext(f"{ARXIV}comment")),
        ))
    return [paper for paper in papers if start <= paper.published <= end]


def classify(paper: Paper, settings: Settings) -> None:
    text = f"{paper.title} {paper.abstract}".lower()
    matching_topics = [label for label, terms in TOPIC_RULES if any(term in text for term in terms)]
    paper.topic = max(TOPIC_RULES, key=lambda rule: sum(term in text for term in rule[1]))[0] if matching_topics else "其他 AI"
    paper.relevance = sum(term in text for term in settings.topics)
    comment = paper.comment.upper()
    paper.conference = next((name for name in settings.conferences if name in comment), None)
    if paper.conference:
        paper.signals.append(f"arXiv 备注提及 {paper.conference}")


def _token_overlap(left: str, right: str) -> float:
    a = set(re.findall(r"[a-z0-9]+", left.lower()))
    b = set(re.findall(r"[a-z0-9]+", right.lower()))
    return len(a & b) / max(1, len(a | b))


def enrich_semantic_scholar(papers: list[Paper]) -> None:
    if not papers:
        return
    try:
        data = _request_json(
            f"{S2_BATCH_API}?fields=title,citationCount,externalIds",
            headers={"Content-Type": "application/json", "User-Agent": "PaperSpider/0.1"},
            body={"ids": [f"ARXIV:{paper.arxiv_id}" for paper in papers]},
        )
        for paper, item in zip(papers, data):
            if item:
                paper.citations = int(item.get("citationCount") or 0)
                if paper.citations:
                    paper.signals.append(f"Semantic Scholar 引用 {paper.citations}")
    except (HTTPError, URLError, TimeoutError, ValueError, TypeError, KeyError):
        return


def enrich_github(paper: Paper) -> None:
    token = os.getenv("GITHUB_TOKEN")
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "PaperSpider/0.1"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        declared = re.search(r"https?://github\.com/([\w.-]+/[\w.-]+)", f"{paper.abstract} {paper.comment}", re.IGNORECASE)
        if declared:
            repository = declared.group(1).rstrip(".,:;)")
            data = _request_json(f"{GITHUB_REPOSITORY_API}/{repository}", headers=headers)
            paper.github_stars = int(data.get("stargazers_count") or 0)
            paper.github_url = data.get("html_url")
            paper.signals.append(f"GitHub {paper.github_stars:,} stars（作者提供代码）")
            return
        data = _request_json(f"{GITHUB_SEARCH_API}?{urlencode({'q': paper.title, 'sort': 'stars', 'order': 'desc', 'per_page': 5})}", headers=headers)
        candidates = data.get("items", [])
        best = max(candidates, key=lambda item: _token_overlap(paper.title, f"{item['name']} {item.get('description') or ''}"), default=None)
        if best and _token_overlap(paper.title, f"{best['name']} {best.get('description') or ''}") >= 0.24:
            paper.github_stars = int(best.get("stargazers_count") or 0)
            paper.github_url = best.get("html_url")
            paper.signals.append(f"GitHub {paper.github_stars:,} stars")
    except (HTTPError, URLError, TimeoutError, ValueError, TypeError, KeyError):
        return


def enrich_hacker_news(paper: Paper) -> None:
    try:
        data = _request_json(f"{HN_SEARCH_API}?{urlencode({'query': paper.title, 'tags': 'story', 'hitsPerPage': 5})}")
        matches = [hit for hit in data.get("hits", []) if _token_overlap(paper.title, hit.get("title") or "") >= 0.55]
        paper.hn_points = max((int(hit.get("points") or 0) for hit in matches), default=0)
        if paper.hn_points:
            paper.signals.append(f"Hacker News {paper.hn_points} points")
    except (HTTPError, URLError, TimeoutError, ValueError, TypeError, KeyError):
        return


def enrich_social_signals(papers: list[Paper]) -> None:
    # Bound calls: enrichment is applied only to candidates likely to reach the report.
    with ThreadPoolExecutor(max_workers=5) as executor:
        futures = [executor.submit(func, paper) for paper in papers for func in (enrich_github, enrich_hacker_news)]
        for future in as_completed(futures):
            try:
                future.result()
            except Exception as error:
                print(f"warning: optional enrichment failed: {type(error).__name__}", file=sys.stderr)


def rank(papers: list[Paper], target: int) -> list[Paper]:
    for paper in papers:
        paper.score = (
            min(paper.relevance, 4) * 8
            + min(20, math.log1p(paper.community_votes) * 5)
            + (15 if paper.conference else 0)
            + min(30, math.log1p(paper.github_stars) * 4)
            + min(20, math.log1p(paper.citations) * 4)
            + min(15, math.log1p(paper.hn_points) * 3)
        )
    ordered = sorted(papers, key=lambda item: (item.score, item.published), reverse=True)
    # Give each available primary field a place before filling by score.
    selected, fields = [], set()
    for paper in ordered:
        if paper.topic not in fields and len(selected) < target:
            selected.append(paper)
            fields.add(paper.topic)
    selected.extend(paper for paper in ordered if paper not in selected)
    selected = sorted(selected[:target], key=lambda item: item.score, reverse=True)
    if not selected:
        return []
    high, low = selected[0].score, selected[-1].score
    for paper in selected:
        # Relative stars make the new-paper ranking legible even when absolute attention is low.
        paper.stars = 3 if high == low else max(1, min(5, round(1 + 4 * (paper.score - low) / (high - low))))
    return selected


def fallback_summary(paper: Paper) -> str:
    sentences = re.split(r"(?<=[.!?])\s+", paper.abstract)
    methods = [sentence for sentence in sentences if re.search(
        r"\b(we (present|propose|introduce|develop|show|find)|our (method|approach|results))\b", sentence, re.I)]
    excerpt = " ".join((methods or sentences)[:2]).strip()
    if len(excerpt) > 650:
        excerpt = excerpt[:647].rsplit(" ", 1)[0] + "…"
    return f"摘要原文节选（中文生成未启用或不可用）：{excerpt}" if excerpt else f"这项工作属于{paper.topic}方向，建议结合原文摘要进一步评估。"


def summarize_with_llm(papers: list[Paper]) -> None:
    api_key = os.getenv("LLM_API_KEY")
    if not api_key:
        for paper in papers:
            paper.summary = fallback_summary(paper)
        return
    base_url = (os.getenv("LLM_BASE_URL") or "https://api.openai.com/v1").rstrip("/")
    model = os.getenv("LLM_MODEL") or "gpt-4o-mini"
    for paper in papers:
        prompt = (
            "用中文写 2-3 句严谨的论文速览。只根据给定摘要，不要臆测结果；说明问题、方法和价值。"
            f"\n标题：{paper.title}\n摘要：{paper.abstract}"
        )
        try:
            response = _request_json(
                f"{base_url}/chat/completions",
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                body={"model": model, "messages": [{"role": "user", "content": prompt}], "temperature": 0.2},
            )
            paper.summary = _clean(response["choices"][0]["message"]["content"]) or fallback_summary(paper)
        except (HTTPError, URLError, TimeoutError, ValueError, TypeError, KeyError, IndexError):
            paper.summary = fallback_summary(paper)


def load_seen(path: Path) -> set[str]:
    try:
        return {canonical_id(item) for item in json.loads(path.read_text(encoding="utf-8"))}
    except (FileNotFoundError, json.JSONDecodeError):
        return set()


def save_seen(path: Path, papers: list[Paper]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    seen = load_seen(path) | {canonical_id(paper.arxiv_id) for paper in papers}
    path.write_text(json.dumps(sorted(seen), ensure_ascii=False, indent=2), encoding="utf-8")


def render_report(papers: list[Paper], start: datetime, end: datetime) -> str:
    lines = [
        f"# AI 论文双报 | {end:%Y-%m-%d}",
        "",
        f"> 覆盖时间：{start:%Y-%m-%d} 至 {end:%Y-%m-%d}（UTC）。从 arXiv 与 Hugging Face Daily Papers 等入口获取摘要元数据，按首次发布日期筛选；不下载 PDF。",
        "> 星级是本期候选间的相对优先级，不代表论文质量的绝对评价。",
        "",
        "## 本期精选",
        "",
    ]
    if not papers:
        lines.append("本时间窗口未发现符合当前主题配置的新论文。可调整 `config.toml` 的分类或关键词后重试。")
    for index, paper in enumerate(papers, 1):
        lines.extend([
            f"### {index}. {'★' * paper.stars}{'☆' * (5 - paper.stars)} [{paper.title}]({paper.url})",
            "",
            f"- **领域**：{paper.topic}",
            f"- **提交**：{paper.published:%Y-%m-%d} | **作者**：{', '.join(paper.authors[:4])}{' 等' if len(paper.authors) > 4 else ''}",
            f"- **来源**：{'、'.join(paper.sources)}",
            f"- **信号**：{'；'.join(paper.signals) if paper.signals else '新近投稿，暂未观测到外部热度信号'}",
            f"- **代码**：[{paper.github_url}]({paper.github_url})" if paper.github_url else "- **代码**：未检索到高置信度公开仓库",
            f"- **摘要**：{paper.summary}",
            "",
        ])
    lines.extend([
        "## 方法说明",
        "",
        "候选合并 arXiv 新投稿和 Hugging Face 社区精选，按论文 ID 与标题去重，并优先覆盖不同领域。排序将主题相关性、顶会备注、GitHub stars、Semantic Scholar 引用和 Hacker News 讨论度合并为可解释分数；服务不可用时对应信号为零，不会阻断出报。",
        "",
    ])
    return "\n".join(lines)


def canonical_id(value: str) -> str:
    return re.sub(r"v\d+$", "", value)


def fetch_huggingface(settings: Settings, start: datetime, end: datetime) -> list[Paper]:
    papers = []
    successes = 0
    day = start.date()
    while day <= end.date():
        try:
            entries = _request_json(f"https://huggingface.co/api/daily_papers?date={day.isoformat()}")
            if not isinstance(entries, list):
                raise ValueError("expected a paper list")
            successes += 1
            for entry in entries:
                try:
                    item = entry["paper"]
                    published = datetime.fromisoformat(item["publishedAt"].replace("Z", "+00:00"))
                    if not start <= published <= end or not item.get("summary"):
                        continue
                    paper_id = canonical_id(item["id"])
                    if not re.fullmatch(r"\d{4}\.\d{4,5}", paper_id):
                        continue
                    paper = Paper(paper_id, _clean(item["title"]), _clean(item["summary"]),
                                  f"https://arxiv.org/abs/{paper_id}", published, published,
                                  [a["name"] for a in item.get("authors", [])], [],
                                  sources=["Hugging Face Daily Papers"],
                                  community_votes=max(0, int(item.get("upvotes") or 0)))
                    paper.github_url = item.get("githubRepo")
                    paper.github_stars = max(0, int(item.get("githubStars") or 0))
                    paper.signals.append(f"Hugging Face 社区推荐 {paper.community_votes} 票")
                    papers.append(paper)
                except (ValueError, TypeError, KeyError):
                    continue
        except Exception as error:
            print(f"warning: Hugging Face {day}: {type(error).__name__}", file=sys.stderr)
        day += timedelta(days=1)
    if not successes:
        raise RuntimeError("all daily feed requests failed")
    return papers


def collect_papers(settings: Settings, start: datetime, end: datetime) -> tuple[list[Paper], list[str]]:
    merged: dict[str, Paper] = {}
    titles: dict[str, str] = {}
    warnings = []
    with ThreadPoolExecutor(max_workers=2) as executor:
        jobs = {executor.submit(fetcher, settings, start, end): name for name, fetcher in
                (("arXiv", fetch_arxiv), ("Hugging Face", fetch_huggingface))}
        for future in as_completed(jobs):
            name = jobs[future]
            try:
                fetched = future.result()
                print(f"{name}: {len(fetched)} candidates", flush=True)
                if not fetched:
                    warnings.append(f"{name} 本窗口返回 0 篇论文")
                for paper in fetched:
                    if not start <= paper.published <= end:
                        continue
                    key = canonical_id(paper.arxiv_id)
                    title = " ".join(re.findall(r"\w+", paper.title.casefold()))
                    key = titles.get(title, key)
                    if key in merged:
                        previous = merged[key]
                        previous.sources = sorted(set(previous.sources + paper.sources))
                        previous.community_votes = max(previous.community_votes, paper.community_votes)
                        previous.github_stars = max(previous.github_stars, paper.github_stars)
                        previous.github_url = previous.github_url or paper.github_url
                        previous.comment = previous.comment or paper.comment
                        previous.signals = sorted(set(previous.signals + paper.signals))
                    else:
                        paper.arxiv_id = key
                        merged[key] = paper
                        titles[title] = key
            except Exception as error:
                warnings.append(f"{name} 获取失败（{type(error).__name__}），本期使用其余来源")
    for warning in warnings:
        print(f"warning: {warning}", file=sys.stderr)
    return list(merged.values()), warnings


def latest_issue_day(today: date) -> date:
    while today.weekday() not in (0, 3):
        today -= timedelta(days=1)
    return today
