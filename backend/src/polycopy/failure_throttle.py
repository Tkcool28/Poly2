"""Bound repeated deterministic failure logging without hiding failures."""

from __future__ import annotations

from collections.abc import Hashable
from dataclasses import dataclass
from time import monotonic


@dataclass(frozen=True)
class FailureLogDecision:
    full_trace: bool
    emit_summary: bool
    repeat_count: int


@dataclass
class _State:
    last_trace_at: float
    repeats_since_trace: int = 0


class FailureLogThrottle:
    """Emit first/full trace, then sparse summaries until trace interval elapses."""

    def __init__(self, *, trace_interval_seconds: float, summary_every: int) -> None:
        if trace_interval_seconds <= 0:
            raise ValueError("trace_interval_seconds must be positive")
        if summary_every <= 0:
            raise ValueError("summary_every must be positive")
        self.trace_interval_seconds = trace_interval_seconds
        self.summary_every = summary_every
        self._state: dict[Hashable, _State] = {}

    def record(
        self,
        signature: Hashable,
        *,
        now: float | None = None,
    ) -> FailureLogDecision:
        current = monotonic() if now is None else now
        state = self._state.get(signature)
        if state is None:
            self._state[signature] = _State(last_trace_at=current)
            return FailureLogDecision(True, False, 1)

        state.repeats_since_trace += 1
        repeat_count = state.repeats_since_trace + 1
        if current - state.last_trace_at >= self.trace_interval_seconds:
            state.last_trace_at = current
            state.repeats_since_trace = 0
            return FailureLogDecision(True, False, repeat_count)

        return FailureLogDecision(
            False,
            state.repeats_since_trace % self.summary_every == 0,
            repeat_count,
        )

    def clear_prefix(self, prefix: object) -> None:
        doomed = [
            key
            for key in self._state
            if isinstance(key, tuple) and key and key[0] == prefix
        ]
        for key in doomed:
            self._state.pop(key, None)
