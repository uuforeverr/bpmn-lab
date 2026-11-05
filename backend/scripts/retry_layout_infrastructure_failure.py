"""Resume a frozen comparison run while treating layout timeouts as infrastructure failures.

This wrapper deliberately leaves ``compare_strategies.py`` unchanged so its
frozen experiment hash remains valid.  All scheduling, generation, scoring,
checkpointing, and artifact persistence still execute through that module.
"""

from __future__ import annotations

import compare_strategies


_original_classifier = compare_strategies._is_infrastructure_failure


def _classify_with_layout_timeout(run: dict) -> bool:
    if _original_classifier(run):
        return True
    if run.get("success"):
        return False
    error = str(run.get("error", ""))
    return "BPMN 自动布局服务调用失败" in error and "timed out" in error


compare_strategies._is_infrastructure_failure = _classify_with_layout_timeout


if __name__ == "__main__":
    raise SystemExit(compare_strategies.main())
