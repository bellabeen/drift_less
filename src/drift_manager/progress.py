"""Progress_Tracker: observes in-flight operations and reports progress snapshots.

Implements the Progress_Tracker interface from the design (task 4.1):

    observe(operation) -> ProgressSnapshot = { total, completed, remaining, failures }
    # invariant: completed + remaining == total

The tracker backs both bulk operations in the pipeline — detection (items are
stacks) and reconciliation (items are resources) — so it is item-agnostic: an
operation is created with a fixed set of item identifiers, and callers report
each item's terminal outcome (success, failure, or skip) as it happens. Every
``observe`` returns an immutable :class:`~drift_manager.models.ProgressSnapshot`
whose counts satisfy ``completed + remaining == total`` by construction
(Requirements 5.1, 5.2).

Per-item failures are recorded and surfaced in each snapshot while the
operation keeps running, so one failed item never stops the rest
(Requirement 5.4). On completion the tracker reports the operation as complete
and produces an :class:`~drift_manager.models.OperationSummary` of
succeeded / failed / skipped counts (Requirement 5.5).

Timing is driven by an injected clock (the ``FakeClock`` in tests, a real
monotonic clock in production) rather than wall-clock sleeping. Each recorded
status change stamps the operation's last-update time, so a caller can assert
that a change is reflected within the 2-second window (Requirement 5.3). The
tracker never sleeps; it only reads the clock to timestamp updates.
"""

from __future__ import annotations

import threading
from typing import Optional, Protocol

from .models import (
    Failure,
    ItemOutcome,
    OperationKind,
    OperationSummary,
    ProgressSnapshot,
    SummaryItem,
)

#: The window, in seconds, within which a recorded status change must be
#: reflected in an observed snapshot (Requirement 5.3).
UPDATE_WINDOW_SECONDS: float = 2.0


class Clock(Protocol):
    """Minimal clock surface the tracker needs to timestamp updates.

    Satisfied by ``tests.fakes.FakeClock`` and by any real monotonic clock
    wrapper exposing a ``monotonic()`` reading in seconds.
    """

    def monotonic(self) -> float:
        """Return a monotonically increasing time in seconds."""
        ...


class Operation:
    """Mutable tracking state for a single in-flight bulk operation.

    An operation is created with the complete set of item identifiers it will
    process (stacks for detection, resources for reconciliation). Callers then
    record each item's terminal outcome exactly once via :meth:`record_success`,
    :meth:`record_failure`, or :meth:`record_skip`. The object is never exposed
    directly to report consumers — :class:`ProgressTracker` reads it to build
    immutable :class:`ProgressSnapshot` and :class:`OperationSummary` artifacts.

    All mutation is guarded by a lock so the tracker can be shared across the
    detector's bounded-concurrency worker pool.
    """

    def __init__(
        self,
        kind: OperationKind,
        item_ids: tuple[str, ...],
        clock: Clock,
    ) -> None:
        self.kind = kind
        self._clock = clock
        self._lock = threading.Lock()

        #: The fixed universe of item identifiers, in submission order.
        self._item_ids: tuple[str, ...] = tuple(item_ids)
        #: Terminal outcome recorded per item id (absent until recorded).
        self._outcomes: dict[str, ItemOutcome] = {}
        #: Recorded failure reasons, keyed by item id.
        self._failure_reasons: dict[str, str] = {}
        #: Recorded skip reasons, keyed by item id.
        self._skip_reasons: dict[str, str] = {}
        #: Order in which failures were recorded (stable snapshot ordering).
        self._failure_order: list[str] = []

        #: Clock reading of the most recent recorded status change.
        self._last_update: float = clock.monotonic()

    # -- Recording status changes -------------------------------------------

    def _record(
        self,
        item_id: str,
        outcome: ItemOutcome,
        reason: Optional[str],
    ) -> None:
        """Record a terminal outcome for ``item_id`` and stamp the update time.

        Recording is idempotent per item: the first terminal outcome for an
        item wins and later records for the same id are ignored, which keeps
        ``completed`` from exceeding ``total`` even if a caller double-reports.
        """
        with self._lock:
            if item_id not in self._item_ids:
                raise KeyError(
                    f"item {item_id!r} is not part of this {self.kind.value} operation"
                )
            if item_id in self._outcomes:
                return
            self._outcomes[item_id] = outcome
            # Blank (empty or whitespace-only) reasons are not a recorded
            # reason; substitute a default so failed/skipped items always
            # carry a non-blank reason (Requirement 6.4).
            has_reason = isinstance(reason, str) and bool(reason.strip())
            if outcome is ItemOutcome.FAILED:
                self._failure_reasons[item_id] = reason if has_reason else "unspecified failure"
                self._failure_order.append(item_id)
            elif outcome is ItemOutcome.SKIPPED:
                self._skip_reasons[item_id] = reason if has_reason else "unspecified skip"
            self._last_update = self._clock.monotonic()

    def record_success(self, item_id: str) -> None:
        """Record that ``item_id`` completed successfully."""
        self._record(item_id, ItemOutcome.SUCCEEDED, None)

    def record_failure(self, item_id: str, reason: str) -> None:
        """Record that ``item_id`` failed, retaining ``reason`` for the summary.

        The failure is surfaced in subsequent snapshots and the operation keeps
        running for the remaining items (Requirement 5.4).
        """
        self._record(item_id, ItemOutcome.FAILED, reason)

    def record_skip(self, item_id: str, reason: str) -> None:
        """Record that ``item_id`` was skipped, retaining ``reason``."""
        self._record(item_id, ItemOutcome.SKIPPED, reason)

    # -- Derived views (lock-guarded) ---------------------------------------

    @property
    def total(self) -> int:
        """The total number of items in the operation."""
        return len(self._item_ids)

    @property
    def last_update(self) -> float:
        """Clock reading of the most recent recorded status change."""
        with self._lock:
            return self._last_update

    def is_complete(self) -> bool:
        """Return ``True`` once every item has a recorded terminal outcome."""
        with self._lock:
            return len(self._outcomes) == len(self._item_ids)

    def _failures_locked(self) -> tuple[Failure, ...]:
        return tuple(
            Failure(id=item_id, reason=self._failure_reasons[item_id])
            for item_id in self._failure_order
        )


class ProgressTracker:
    """Observes in-flight bulk operations and reports progress and summaries.

    The tracker is the single object the orchestrator hands to the detector and
    the reconciler. It owns operation lifecycle (:meth:`start`), exposes a
    point-in-time :meth:`observe` returning an immutable snapshot, answers
    :meth:`is_complete`, and produces the end-of-run :meth:`summary`.
    """

    def __init__(self, clock: Clock) -> None:
        self._clock = clock

    def start(
        self,
        kind: OperationKind,
        item_ids: tuple[str, ...],
    ) -> Operation:
        """Begin tracking an operation over ``item_ids`` and return its handle.

        ``kind`` distinguishes detection (items are stacks) from reconciliation
        (items are resources); the returned :class:`Operation` is used both to
        record status changes and to observe progress.
        """
        return Operation(kind, tuple(item_ids), self._clock)

    def observe(self, operation: Operation) -> ProgressSnapshot:
        """Return a point-in-time snapshot of ``operation``.

        The snapshot reports ``total``, ``completed``, ``remaining``, and the
        recorded ``failures``. By construction ``remaining = total - completed``
        and therefore ``completed + remaining == total`` (Requirements 5.1,
        5.2). Completed counts every item with a terminal outcome — success,
        failure, or skip — so a failed item advances progress rather than
        stalling it (Requirement 5.4).
        """
        with operation._lock:  # noqa: SLF001 - trusted collaborator
            total = len(operation._item_ids)
            completed = len(operation._outcomes)
            remaining = total - completed
            failures = operation._failures_locked()
        return ProgressSnapshot(
            total=total,
            completed=completed,
            remaining=remaining,
            failures=failures,
        )

    def is_complete(self, operation: Operation) -> bool:
        """Return ``True`` once the operation has reported every item complete.

        Equivalent to observing a snapshot whose ``remaining`` is zero.
        """
        return operation.is_complete()

    def reflects_within_window(
        self,
        operation: Operation,
        *,
        window: float = UPDATE_WINDOW_SECONDS,
    ) -> bool:
        """Report whether the latest status change is within the update window.

        A recorded status change stamps the operation's last-update time from
        the injected clock. This returns ``True`` while the elapsed time since
        that stamp is at most ``window`` seconds (default 2s), letting callers
        assert the 2-second reflection guarantee deterministically against the
        fake clock (Requirement 5.3). The tracker itself never sleeps.
        """
        elapsed = self._clock.monotonic() - operation.last_update
        return elapsed <= window

    def summary(self, operation: Operation) -> OperationSummary:
        """Produce the end-of-run operation summary for ``operation``.

        Lists every item with its outcome (succeeded / failed / skipped) and the
        aggregate counts, where failed and skipped items carry their recorded
        reason. The three counts sum to the number of items listed, so
        ``succeeded + failed + skipped == len(items)`` (Requirement 5.5).
        """
        with operation._lock:  # noqa: SLF001 - trusted collaborator
            item_ids = operation._item_ids
            outcomes = dict(operation._outcomes)
            failure_reasons = dict(operation._failure_reasons)
            skip_reasons = dict(operation._skip_reasons)

        items: list[SummaryItem] = []
        succeeded = failed = skipped = 0
        for item_id in item_ids:
            outcome = outcomes.get(item_id)
            if outcome is ItemOutcome.SUCCEEDED:
                succeeded += 1
                items.append(SummaryItem(id=item_id, outcome=ItemOutcome.SUCCEEDED))
            elif outcome is ItemOutcome.FAILED:
                failed += 1
                items.append(
                    SummaryItem(
                        id=item_id,
                        outcome=ItemOutcome.FAILED,
                        reason=failure_reasons.get(item_id),
                    )
                )
            elif outcome is ItemOutcome.SKIPPED:
                skipped += 1
                items.append(
                    SummaryItem(
                        id=item_id,
                        outcome=ItemOutcome.SKIPPED,
                        reason=skip_reasons.get(item_id),
                    )
                )
            else:
                # An item with no recorded terminal outcome is treated as
                # skipped with a reason so the summary still conserves the item
                # total (succeeded + failed + skipped == len(items)).
                skipped += 1
                items.append(
                    SummaryItem(
                        id=item_id,
                        outcome=ItemOutcome.SKIPPED,
                        reason="no outcome recorded before summary",
                    )
                )

        return OperationSummary(
            kind=operation.kind,
            items=tuple(items),
            succeeded=succeeded,
            failed=failed,
            skipped=skipped,
        )
