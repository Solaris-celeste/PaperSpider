"""Collection and editorial pipeline. Every network integration is optional."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, time, timedelta, timezone
import json
import math
import os
from pathlib import Path
import re
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
            with urlopen(request, timeout=90) as response:  # nosec B310: fixed endpoint
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
    # arXiv's documented submittedDate filter intermittently returns HTTP 500. Fetch a
    # bounded newest slice and apply the exact time window locally instead.
    params = urlencode({"search_query": f"({categories})", "start": 0, "max_results": settings.candidate_limit, "sortBy": "submittedDate", "sortOrder": "descending"})
    root = ET.fromstring(_request_arxiv(f"{ARXIV_API}?{params}"))

    papers: list[Paper] = []
    for entry in root.findall(f"{ATOM}entry"):
        published = datetime.fromisoformat(entry.findtext(f"{ATOM}published", "").replace("Z", "+00:00"))
        updated = datetime.fromisoformat(entry.findtext(f"{ATOM}updated", "").replace("Z", "+00:00"))
        url = entry.findtext(f"{ATOM}id", "")
        paper_id = url.rsplit("/", 1)[-1]
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
    paper.topic = " / ".join(matching_topics[:2]) or "其他 AI"
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
    except (HTTPError, URLError, TimeoutError, json.JSONDecodeError):
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
    except (HTTPError, URLError, TimeoutError, json.JSONDecodeError):
        return


def enrich_hacker_news(paper: Paper) -> None:
    try:
        data = _request_json(f"{HN_SEARCH_API}?{urlencode({'query': paper.title, 'tags': 'story', 'hitsPerPage': 5})}")
        matches = [hit for hit in data.get("hits", []) if _token_overlap(paper.title, hit.get("title") or "") >= 0.55]
        paper.hn_points = max((int(hit.get("points") or 0) for hit in matches), default=0)
        if paper.hn_points:
            paper.signals.append(f"Hacker News {paper.hn_points} points")
    except (HTTPError, URLError, TimeoutError, json.JSONDecodeError):
        return


def enrich_social_signals(papers: list[Paper]) -> None:
    # Bound calls: enrichment is applied only to candidates likely to reach the report.
    with ThreadPoolExecutor(max_workers=5) as executor:
        futures = [executor.submit(func, paper) for paper in papers for func in (enrich_github, enrich_hacker_news)]
        for future in as_completed(futures):
            future.result()


def rank(papers: list[Paper], target: int) -> list[Paper]:
    for paper in papers:
        paper.score = (
            paper.relevance * 8
            + (15 if paper.conference else 0)
            + min(30, math.log1p(paper.github_stars) * 4)
            + min(20, math.log1p(paper.citations) * 4)
            + min(15, math.log1p(paper.hn_points) * 3)
        )
    selected = sorted(papers, key=lambda item: (item.score, item.published), reverse=True)[:target]
    if not selected:
        return []
    high, low = selected[0].score, selected[-1].score
    for paper in selected:
        # Relative stars make the new-paper ranking legible even when absolute attention is low.
        paper.stars = 3 if high == low else max(1, min(5, round(1 + 4 * (paper.score - low) / (high - low))))
    return selected


def fallback_summary(paper: Paper) -> str:
    sentences = re.split(r"(?<=[.!?])\s+", paper.abstract)
    excerpt = " ".join(sentences[:2]).strip()
    return f"这项工作属于{paper.topic}方向。{excerpt}" if excerpt else f"这项工作属于{paper.topic}方向，建议结合原文摘要进一步评估。"


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
            paper.summary = _clean(response["choices"][0]["message"]["content"])
        except (HTTPError, URLError, TimeoutError, ValueError, KeyError, IndexError, json.JSONDecodeError):
            paper.summary = fallback_summary(paper)


def load_seen(path: Path) -> set[str]:
    try:
        return set(json.loads(path.read_text(encoding="utf-8")))
    except (FileNotFoundError, json.JSONDecodeError):
        return set()


def save_seen(path: Path, papers: list[Paper]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    seen = load_seen(path) | {paper.arxiv_id for paper in papers}
    path.write_text(json.dumps(sorted(seen), ensure_ascii=False, indent=2), encoding="utf-8")


def render_report(papers: list[Paper], start: datetime, end: datetime) -> str:
    lines = [
        f"# AI 论文双报 | {end:%Y-%m-%d}",
        "",
        f"> 覆盖时间：{start:%Y-%m-%d} 至 {end:%Y-%m-%d}（UTC）。从 arXiv 新投稿中筛选，并以代码热度、引用和公开社区讨论信号辅助排序。",
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
            f"- **信号**：{'；'.join(paper.signals) if paper.signals else '新近投稿，暂未观测到外部热度信号'}",
            f"- **代码**：[{paper.github_url}]({paper.github_url})" if paper.github_url else "- **代码**：未检索到高置信度公开仓库",
            f"- **摘要**：{paper.summary}",
            "",
        ])
    lines.extend([
        "## 方法说明",
        "",
        "候选来自 `cs.AI`、`cs.CL`、`cs.CV`、`cs.LG`、`stat.ML` 的 arXiv 新投稿。排序将主题相关性、顶会备注、GitHub stars、Semantic Scholar 引用和 Hacker News 讨论度合并为可解释分数；服务不可用时对应信号为零，不会阻断出报。",
        "",
    ])
    return "\n".join(lines)
