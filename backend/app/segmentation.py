from __future__ import annotations

import re
from abc import ABC, abstractmethod

from .domain import Segment, Sentence


_PROTECTED_PERIOD_PATTERNS = (
    r"(?i)\b(?:e\.g\.|i\.e\.|a\.m\.|p\.m\.|ph\.d\.|mr\.|mrs\.|ms\.|dr\.|prof\.|sr\.|jr\.|vs\.|cf\.)",
    r"\b(?:[A-Za-z]\.){2,}",
    r"\b[A-Z]\.(?=\s+[A-Z][a-z])",
    r"(?<!\w)\d+(?:\.\d+)+(?!\w)",
    r"(?i)\b(?:no|fig|eq|sec|ch|vol|pp?|jan|feb|mar|apr|jun|jul|aug|sep|sept|oct|nov|dec)\.(?=\s*\d)",
)


def split_sentences(text: str) -> list[Sentence]:
    text = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    text = re.sub(r"\s*\n+\s*", " ", text)
    text = re.sub(r"[ \t]+", " ", text)
    protected: dict[str, str] = {}

    def protect(match: re.Match[str]) -> str:
        token = f"__ABBR_{len(protected)}__"
        protected[token] = match.group(0)
        return token

    for pattern in _PROTECTED_PERIOD_PATTERNS:
        text = re.sub(pattern, protect, text)
    parts = re.split(r"(?<=[。！？])\s*|(?<=[.!?])(?:\s+|$)", text)
    cleaned = []
    for part in parts:
        for token, value in protected.items():
            part = part.replace(token, value)
        if part.strip():
            cleaned.append(part.strip())
    return [Sentence(id=f"S{i}", text=value) for i, value in enumerate(cleaned, 1)]


class SegmentationStrategy(ABC):
    name: str

    @abstractmethod
    def segment(self, sentences: list[Sentence]) -> list[Segment]: ...


class FixedLengthStrategy(SegmentationStrategy):
    name = "fixed_length"

    def __init__(self, size: int = 3):
        self.size = max(1, size)

    def segment(self, sentences: list[Sentence]) -> list[Segment]:
        result = []
        for index, offset in enumerate(range(0, len(sentences), self.size), 1):
            group = sentences[offset:offset + self.size]
            result.append(Segment(
                id=f"P{index}", start=group[0].id, end=group[-1].id,
                title=f"固定分段 P{index} {group[0].id}-{group[-1].id}",
                sentenceIds=[item.id for item in group],
            ))
        return result


class SingleSegmentStrategy(SegmentationStrategy):
    name = "single_segment"

    def segment(self, sentences: list[Sentence]) -> list[Segment]:
        if not sentences:
            return []
        return [Segment(id="P1", start=sentences[0].id, end=sentences[-1].id,
                        title="完整流程", sentenceIds=[item.id for item in sentences])]
