from niadra_bench.stats import across, distribution, percentile, rate, share_difference, wilson


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


def test_wilson_interval_holds_at_the_edges() -> None:
    assert wilson(0, 0) is None
    low, high = wilson(0, 12) or (None, None)
    assert low == 0.0 and high is not None and 0.24 < high < 0.25
    low, high = wilson(12, 12) or (None, None)
    assert high == 1.0 and low is not None and 0.75 < low < 0.76
    low, high = wilson(10, 20) or (None, None)
    assert low is not None and high is not None and abs((low + high) / 2 - 0.5) < 1e-9


def test_the_difference_interval_is_newcombes() -> None:
    # Newcombe (1998), table II, example (a): 56/70 against 48/80 gives 0.0524 to 0.3339.
    low, high = share_difference(56, 70, 48, 80) or (0.0, 0.0)
    assert round(low, 4) == 0.0524 and round(high, 4) == 0.3339
    assert share_difference(1, 2, 0, 0) is None
