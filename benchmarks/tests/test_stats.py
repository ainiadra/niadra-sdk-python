from niadra_bench.stats import across, distribution, percentile, rate


def test_percentile_is_nearest_rank() -> None:
    values = list(range(1, 101))
    assert percentile(values, 50) == 51
    assert percentile(values, 95) == 95
    assert percentile(values, 99) == 99
    assert percentile([], 50) is None


def test_distribution_and_rate() -> None:
    d = distribution([10.0, 20.0, 30.0])
    assert d["n"] == 3 and d["p50"] == 20.0 and d["max"] == 30.0
    assert rate(1, 3) == 0.3333
    assert rate(1, 0) is None


def test_across_repetitions_keeps_median_and_range() -> None:
    assert across([45.2, 50.0, 47.1], 1) == {
        "median": 47.1,
        "min": 45.2,
        "max": 50.0,
        "runs": [45.2, 50.0, 47.1],
    }
    assert across([None, None])["median"] is None
