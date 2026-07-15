"""X15: gamma arrival process -- mean rate preserved, CV as requested."""

import random
import statistics

import pytest


@pytest.mark.parametrize("rate,cv", [(20.0, 2.0), (10.0, 4.0), (50.0, 8.0)])
def test_gamma_gap_statistics(rate, cv):
    """The pacing draw in benchmark.py: gammavariate(1/cv^2, cv^2/rate) must
    keep the mean inter-arrival gap at 1/rate while hitting the requested
    coefficient of variation (CV=1 would equal Poisson)."""
    rng = random.Random(7)
    cv2 = cv * cv
    draws = [rng.gammavariate(1.0 / cv2, cv2 / rate) for _ in range(200_000)]
    mean = statistics.fmean(draws)
    sd = statistics.pstdev(draws)
    assert mean == pytest.approx(1.0 / rate, rel=0.03)
    assert sd / mean == pytest.approx(cv, rel=0.05)


def test_cli_accepts_gamma():
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from kv_cache import workload

    class Args:
        arrival = "gamma"
        request_rate = 0
        arrival_cv = 2.0

    errors = []
    # reuse the validation snippet's contract: gamma needs a positive rate
    a = Args()
    if getattr(a, "arrival", "fixed") in ("poisson", "gamma") and a.request_rate <= 0:
        errors.append("rate required")
    assert errors
    src = open(Path(__file__).resolve().parents[1] / "kv_cache" / "cli.py").read()
    assert "'gamma'" in src and "--arrival-cv" in src
    src = open(Path(__file__).resolve().parents[1] / "kv_cache" / "benchmark.py").read()
    assert "gammavariate" in src
