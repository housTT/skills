"""Reconcile serving expectations and choose useful secondary parallelism."""
from benchmark_stage.evidence import finite_number

PROFILES = (1, 8, 16)


def review_performance(performance, expectations):
    if not isinstance(expectations, dict) or set(expectations) != {'1', '8', '16'}:
        raise ValueError('performance_expectations must cover profiles 1, 8 and 16')
    review = {}
    for capacity in PROFILES:
        key = str(capacity)
        expected = expectations[key]
        if not isinstance(expected, dict):
            raise ValueError(f'profile {key}: invalid performance expectation')
        for field in ('max_ttft_ms', 'min_decode_tokens_per_second_per_user'):
            if not finite_number(expected.get(field)) or expected[field] <= 0:
                raise ValueError(f'profile {key}: invalid expectation {field}')
        if not isinstance(expected.get('source'), str) or not expected['source'].strip():
            raise ValueError(f'profile {key}: expectation needs prior measurement sources')
        row = performance[key]
        ttft, tpot = row['mean_ttft_ms'], row['mean_tpot_ms']
        if not finite_number(ttft) or ttft <= 0 or not finite_number(tpot) or tpot <= 0:
            raise ValueError(f'profile {key}: invalid observed TTFT/TPOT')
        tsu = 1000 / tpot
        review[key] = {'ttft_ms': ttft, 'decode_tokens_per_second_per_user': tsu,
                       'ttft_limit_ratio': ttft / expected['max_ttft_ms'],
                       'decode_floor_ratio': tsu / expected['min_decode_tokens_per_second_per_user'],
                       'within_expectation': ttft <= expected['max_ttft_ms'] and
                           tsu >= expected['min_decode_tokens_per_second_per_user'],
                       'source': expected['source']}
    return review


def select_accuracy_concurrency(performance, requested=None):
    """Compare equal useful work including the measured request-pool drain."""
    costs = {}
    for capacity in PROFILES:
        row = performance[str(capacity)]
        duration, completed = row.get('duration'), row.get('completed')
        if not finite_number(duration) or duration <= 0 or type(completed) is not int or completed <= 0:
            raise ValueError('parallelism comparison needs positive duration and completion count')
        costs[capacity] = duration / completed
    speedups = {str(n): costs[1] / costs[n] for n in PROFILES}
    best = min(PROFILES, key=lambda n: (costs[n], n))
    if requested is not None:
        if type(requested) is not int or requested not in PROFILES:
            raise ValueError('accuracy_concurrency must be 1, 8 or 16')
        if requested != 1 and speedups[str(requested)] <= 1:
            raise ValueError('secondary accuracy concurrency must beat serial batch-1 wall time')
        best = requested
    return best, speedups
