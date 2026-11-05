from __future__ import annotations

import re
from abc import ABC, abstractmethod

import pysbd

from .domain import Segment, Sentence


_ENGLISH_SENTENCE_SEGMENTER = pysbd.Segmenter(language="en", clean=False)


def split_sentences(text: str) -> list[Sentence]:
    text = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    text = re.sub(r"\s*\n+\s*", " ", text)
    text = re.sub(r"[ \t]+", " ", text)
    cleaned = []
    for english_part in _ENGLISH_SENTENCE_SEGMENTER.segment(text):
        for part in re.split(r"(?<=[。！？])\s*", english_part):
            part = re.sub(r"[ \t]+", " ", part).strip()
            if part:
                cleaned.append(part)
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
