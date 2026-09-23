"""Failure-log throttle regression tests."""

from polycopy.failure_throttle import FailureLogThrottle


def test_repeated_failure_logging_is_bounded():
    throttle = FailureLogThrottle(trace_interval_seconds=300, summary_every=20)
    signature = ("wallet", "ValueError", "same bad input")

    first = throttle.record(signature, now=0)
    assert first.full_trace is True

    decisions = [throttle.record(signature, now=float(i)) for i in range(1, 40)]
    assert sum(d.full_trace for d in decisions) == 0
    assert sum(d.emit_summary for d in decisions) == 1

    later = throttle.record(signature, now=301)
    assert later.full_trace is True
