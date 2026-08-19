from dataclasses import dataclass
from pathlib import Path
import re

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10: keep the project dependency-free.
    tomllib = None


@dataclass(frozen=True)
class Settings:
    target_papers: int
    candidate_limit: int
    arxiv_categories: list[str]
    conferences: list[str]
    topics: list[str]


def load_settings(path: str | Path = "config.toml") -> Settings:
    content = Path(path).read_text(encoding="utf-8")
    if tomllib:
        data = tomllib.loads(content)
    else:
        data = _parse_simple_toml(content)
    target = int(data.get("target_papers", 10))
    return Settings(
        target_papers=max(5, min(15, target)),
        candidate_limit=max(20, int(data.get("candidate_limit", 60))),
        arxiv_categories=list(data.get("arxiv_categories", [])),
        conferences=[item.upper() for item in data.get("conferences", [])],
        topics=[item.lower() for item in data.get("topics", [])],
    )


def _parse_simple_toml(content: str) -> dict[str, object]:
    """Parse this project's flat scalar/string-array TOML on Python 3.10."""
    without_comments = re.sub(r"#.*$", "", content, flags=re.MULTILINE)
    values: dict[str, object] = {}
    for key, raw in re.findall(r"^(\w+)\s*=\s*(\d+|\[[\s\S]*?\]|\"[^\"]*\")\s*$", without_comments, re.MULTILINE):
        if raw.isdigit():
            values[key] = int(raw)
        elif raw.startswith("["):
            values[key] = re.findall(r'"([^\"]*)"', raw)
        else:
            values[key] = raw.strip('"')
    return values
