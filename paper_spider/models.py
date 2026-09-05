from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class Paper:
    arxiv_id: str
    title: str
    abstract: str
    url: str
    published: datetime
    updated: datetime
    authors: list[str]
    categories: list[str]
    comment: str = ""
    topic: str = "其他 AI"
    relevance: int = 0
    conference: str | None = None
    github_url: str | None = None
    github_stars: int = 0
    citations: int = 0
    hn_points: int = 0
    score: float = 0.0
    stars: int = 1
    summary: str = ""
    sources: list[str] = field(default_factory=lambda: ["arXiv"])
    community_votes: int = 0
    signals: list[str] = field(default_factory=list)
