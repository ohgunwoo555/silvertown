"""파이프라인 공용 유틸: 경로, .env 로드, topics.csv 읽기.

키는 환경변수에서만 읽는다. .env 파일이 있으면 os.environ 에 올리되
이미 설정된 값은 덮어쓰지 않는다. 파일에 키를 쓰는 함수는 두지 않는다.
"""
from __future__ import annotations

import csv
import os
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TOPICS_CSV = ROOT / "topics.csv"
SOURCES_DIR = ROOT / "sources"
TEMPLATES_DIR = ROOT / "templates"
ASSETS_DIR = ROOT / "assets"
OUTPUT_DIR = ROOT / "output"


def load_dotenv(path: Path = ROOT / ".env") -> None:
    """.env 의 KEY=VALUE 를 환경변수로 올린다. 기존 값은 유지."""
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


@dataclass
class Topic:
    id: str
    topic: str
    key_message: str
    sources: list[str] = field(default_factory=list)
    status: str = ""

    @property
    def work_dir(self) -> Path:
        """중간 산출물 폴더: output/{id}/ (최종 영상은 output/{id}.mp4)."""
        return OUTPUT_DIR / self.id

    @property
    def script_path(self) -> Path:
        return self.work_dir / "script.json"


def load_topics(path: Path = TOPICS_CSV) -> list[Topic]:
    if not path.exists():
        raise FileNotFoundError(f"topics.csv 가 없습니다: {path}")
    topics: list[Topic] = []
    with path.open(encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            if not (row.get("id") or "").strip():
                continue
            sources = [s.strip() for s in (row.get("sources") or "").split(";") if s.strip()]
            topics.append(
                Topic(
                    id=row["id"].strip(),
                    topic=(row.get("topic") or "").strip(),
                    key_message=(row.get("key_message") or "").strip(),
                    sources=sources,
                    status=(row.get("status") or "").strip(),
                )
            )
    return topics


def pick_topic(topic_id: str | None = None, row: int | None = None) -> Topic:
    """--id 우선, 없으면 --row(1부터), 둘 다 없으면 첫 행."""
    topics = load_topics()
    if not topics:
        raise ValueError("topics.csv 에 행이 없습니다.")
    if topic_id:
        for t in topics:
            if t.id == topic_id:
                return t
        raise ValueError(f"id={topic_id} 행이 topics.csv 에 없습니다.")
    idx = (row or 1) - 1
    if idx < 0 or idx >= len(topics):
        raise ValueError(f"row={row} 는 범위 밖입니다 (1~{len(topics)}).")
    return topics[idx]


def read_sources(topic: Topic) -> str:
    """sources/ 의 근거 문서를 합쳐서 반환. 하나라도 없으면 실패."""
    if not topic.sources:
        raise ValueError(f"[{topic.id}] sources 열이 비어 있습니다. 근거 없이는 대본을 쓰지 않습니다.")
    parts: list[str] = []
    for name in topic.sources:
        p = SOURCES_DIR / name
        if not p.exists():
            raise FileNotFoundError(f"[{topic.id}] 근거 문서가 없습니다: {p}")
        text = p.read_text(encoding="utf-8").strip()
        if "출처:" not in text:
            raise ValueError(f"[{topic.id}] '출처:' 줄이 없는 근거 문서는 쓸 수 없습니다: {name}")
        parts.append(f"<source file=\"{name}\">\n{text}\n</source>")
    return "\n\n".join(parts)
