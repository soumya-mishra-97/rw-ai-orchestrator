from __future__ import annotations

from evals.agreement import cohens_kappa
from evals.metrics import percentile


def test_percentile_nearest_rank() -> None:
    assert percentile([], 50) is None
    assert percentile([10.0], 95) == 10.0
    values = [float(v) for v in range(1, 21)]
    assert percentile(values, 50) == 10.0
    assert percentile(values, 95) == 19.0


def test_cohens_kappa() -> None:
    assert cohens_kappa(["pass", "fail"], ["pass", "fail"]) == 1.0
    # agreement no better than chance -> 0
    assert cohens_kappa(["pass", "pass", "fail", "fail"], ["pass", "fail", "pass", "fail"]) == 0.0
    assert cohens_kappa([], []) is None
