from __future__ import annotations

from scripts.recompute_depth31_metrics import (
    MEAN_TOKEN_METRICS,
    QUALITY_METRICS,
    TOKEN_METRICS,
    _flow_means,
    _paired_deltas,
)
from scripts.build_idealized_visualization_dataset import (
    select_semantic_unsound_samples,
)


def _record(
    sample: str,
    strategy: str,
    repetition: int,
    *,
    success: bool,
    sound: bool,
) -> dict[str, object]:
    row: dict[str, object] = {
        "sample": sample,
        "strategy": strategy,
        "repetition": repetition,
        "success": success,
        "sound": sound,
        "qualityEvaluable": success,
        "totalTokens": 100.0,
        "tasks": 12,
        "taskMaxNestingDepth": 2,
        "depthTier": "medium",
    }
    for metric in TOKEN_METRICS:
        row[metric] = 10.0
    for metric in QUALITY_METRICS:
        row[metric] = 0.5 if success else None
    return row


def _three_runs(
    sample: str,
    strategy: str,
    states: list[tuple[bool, bool]],
) -> list[dict[str, object]]:
    return [
        _record(sample, strategy, index, success=success, sound=sound)
        for index, (success, sound) in enumerate(states, start=1)
    ]


def test_completion_and_soundness_require_all_three_runs() -> None:
    rows = _flow_means(
        _three_runs("01", "semantic", [(True, True), (True, True), (False, False)])
        + _three_runs("02", "semantic", [(True, True), (True, True), (True, False)])
        + _three_runs("03", "semantic", [(True, True), (True, True), (True, True)])
    )
    by_sample = {row["sample"]: row for row in rows}

    assert by_sample["01"]["flowCompletionAllThree"] == 0.0
    assert by_sample["01"]["flowSoundnessAllThree"] == 0.0
    assert by_sample["02"]["flowCompletionAllThree"] == 1.0
    assert by_sample["02"]["flowSoundnessAllThree"] == 0.0
    assert by_sample["03"]["flowCompletionAllThree"] == 1.0
    assert by_sample["03"]["flowSoundnessAllThree"] == 1.0


def test_paired_reliability_deltas_are_only_minus_one_zero_or_one() -> None:
    records: list[dict[str, object]] = []
    records += _three_runs("01", "single_segment", [(True, True), (True, True), (True, False)])
    records += _three_runs("01", "semantic", [(True, True), (True, True), (True, True)])
    records += _three_runs("02", "single_segment", [(True, True), (True, True), (True, True)])
    records += _three_runs("02", "semantic", [(True, True), (True, True), (False, False)])
    records += _three_runs("03", "single_segment", [(True, True), (True, True), (True, True)])
    records += _three_runs("03", "semantic", [(True, True), (True, True), (True, True)])

    paired = _paired_deltas(_flow_means(records))

    assert {row["flowCompletionAllThree"] for row in paired} <= {-1.0, 0.0, 1.0}
    assert {row["flowSoundnessAllThree"] for row in paired} == {-1.0, 0.0, 1.0}
    assert all("soundnessRateCompleted" not in row for row in paired)
    assert all(metric in _flow_means(records)[0] for metric in MEAN_TOKEN_METRICS)


def test_idealized_soundness_selection_uses_worst_observed_candidates() -> None:
    rows = [
        {
            "sample": sample,
            "strategy": "semantic",
            "depthTier": tier,
            "flowSoundnessAllThree": sound,
            "soundnessRatePlanned": planned,
            "soundnessRateCompleted": completed,
        }
        for sample, tier, sound, planned, completed in (
            ("01", "low", 1.0, 1.0, 1.0),
            ("41", "medium", 0.0, 1 / 3, 1.0),
            ("48", "medium", 0.0, 2 / 3, 1.0),
            ("25", "high", 0.0, 1 / 3, 0.5),
            ("16", "high", 0.0, 2 / 3, 2 / 3),
            ("18", "high", 0.0, 2 / 3, 2 / 3),
            ("45", "high", 0.0, 2 / 3, 1.0),
        )
    ]

    assert select_semantic_unsound_samples(rows) == {
        "low": [],
        "medium": ["41"],
        "high": ["25", "16"],
    }
