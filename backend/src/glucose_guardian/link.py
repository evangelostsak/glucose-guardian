"""Pi-side link supervision for protocol v1. Pure: no I/O."""
import math
from collections import Counter, OrderedDict
from dataclasses import dataclass

from .protocol import (
    COUNTER_MODULUS,
    SEQ_MODULUS,
    Reject,
    Report,
    ResetCause,
    decode_report,
    seq_newer,
)

ACK_WINDOW = 16
ABNORMAL_CAUSES = frozenset({ResetCause.WATCHDOG, ResetCause.BROWN_OUT})


@dataclass(frozen=True)
class LinkEvents:
    short_presses: int
    long_presses: int
    unexpected_reset: bool


NO_EVENTS = LinkEvents(0, 0, False)


class Link:
    def __init__(self, *, session: int) -> None:
        self.session = session
        self.counts: Counter[str] = Counter()
        self.button_stuck = False
        self._next_seq = 0
        self._sent: OrderedDict[int, float] = OrderedDict()
        self._acked_at_s = -math.inf
        self._boot: int | None = None
        self._uno_seq = 0
        self._presses = (0, 0)

    def connection_opened(self) -> None:
        self._boot = None
        self._sent.clear()
        self.button_stuck = False

    def next_seq(self) -> int:
        seq = self._next_seq
        self._next_seq = (seq + 1) % SEQ_MODULUS
        return seq

    def sent(self, seq: int, monotonic_s: float) -> None:
        self._sent.pop(seq, None)
        self._sent[seq] = monotonic_s
        while len(self._sent) > ACK_WINDOW:
            self._sent.popitem(last=False)

    def age_s(self, monotonic_s: float) -> float:
        return monotonic_s - self._acked_at_s

    def accept(self, item: bytes | Reject) -> LinkEvents:
        report = item if isinstance(item, Reject) else decode_report(item)
        if isinstance(report, Reject):
            self.counts[f"rejected_{report.value}"] += 1
            return NO_EVENTS
        sent_at = self._sent.get(report.ack_seq) if report.ack_session == self.session else None
        if sent_at is None:
            self.counts["rejected_outdated"] += 1
            return NO_EVENTS
        if report.boot == self._boot and not seq_newer(report.seq, self._uno_seq):
            self.counts["rejected_duplicate"] += 1
            return NO_EVENTS
        return self._apply(report, sent_at)

    def _apply(self, report: Report, sent_at: float) -> LinkEvents:
        if self._boot is None:
            self.counts["connections"] += 1
            unexpected_reset = report.cause in ABNORMAL_CAUSES
            previous = (report.short_presses, report.long_presses)
        elif report.boot != self._boot:
            self.counts["unexpected_resets"] += 1
            unexpected_reset = True
            previous = (0, 0)
        else:
            unexpected_reset = False
            previous = self._presses
        self._boot = report.boot
        self._uno_seq = report.seq
        self._presses = (report.short_presses, report.long_presses)
        self._acked_at_s = max(self._acked_at_s, sent_at)
        self.button_stuck = report.button_stuck
        self.counts["accepted"] += 1
        return LinkEvents((report.short_presses - previous[0]) % COUNTER_MODULUS,
                          (report.long_presses - previous[1]) % COUNTER_MODULUS,
                          unexpected_reset)
