"""Bench checks. Each result records expected and observed behavior and its evidence."""
import resource
import statistics
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import serial

from ..alarms import Fault, Policy
from ..config import RuntimeSettings
from ..display import Frame, MemoryDisplay
from ..protocol import crc16, encode_state
from ..providers import SimulatedProvider
from ..runtime import Runtime
from ..serial_transport import Opener, Port, SerialTransport
from ..worker import run_inline
from .authorization import AuthorizationError
from .discovery import Candidate
from .probe import BENCH_STATE, Probe, Reply
from .report import Check, Status

DTR_PULSE_S = 0.1
BENCH_POLICY = Policy(urgent_low_mg_dl=55, low_mg_dl=70, high_mg_dl=250, hysteresis_mg_dl=5,
                      rearm_mg_dl=10, max_age_s=600, max_future_skew_s=60,
                      heartbeat_timeout_s=5, glucose_acknowledgement_s=60,
                      fault_acknowledgement_s=60)


@dataclass(frozen=True)
class Profile:
    name: str
    boot_timeout_s: float
    reply_timeout_s: float
    exchange_interval_s: float
    exchanges: int
    silence_window_s: float
    heartbeat_pause_s: float
    resets: int
    burst_exchanges: int
    sustained_s: float
    sustained_tick_s: float
    max_tick_lateness_s: float
    backoff_initial_s: float
    backoff_max_s: float
    unplugged_s: float
    replug_timeout_s: float
    watchdog_cycles: int
    watchdog_cycle_timeout_s: float
    watchdog_gap_min_s: float
    watchdog_gap_max_s: float


HARDWARE = Profile(
    name="hardware", boot_timeout_s=5, reply_timeout_s=0.5, exchange_interval_s=0.2,
    exchanges=50, silence_window_s=0.6, heartbeat_pause_s=7, resets=5, burst_exchanges=500,
    sustained_s=120, sustained_tick_s=0.2, max_tick_lateness_s=0.1, backoff_initial_s=1,
    backoff_max_s=8, unplugged_s=20, replug_timeout_s=60, watchdog_cycles=2,
    watchdog_cycle_timeout_s=40, watchdog_gap_min_s=1.5, watchdog_gap_max_s=4.0)


@dataclass(frozen=True)
class Bench:
    device: str
    physical: bool
    profile: Profile
    session: int
    clock: Callable[[], float]
    sleep: Callable[[float], None]
    open_port: Callable[[str], serial.Serial]
    reset: Callable[[serial.Serial], None]
    transport_opener: Opener
    present: Callable[[str], bool]
    prompt: Callable[[str], None]
    interactive: bool

    @property
    def evidence(self) -> str:
        return "physical-usb" if self.physical else "simulated"


def dtr_reset(port: serial.Serial, sleep: Callable[[float], None]) -> None:
    port.dtr = False
    sleep(DTR_PULSE_S)
    port.dtr = True


class _Timer:
    def __init__(self, bench: Bench) -> None:
        self.bench = bench
        self.started = bench.clock()

    def check(self, spec: tuple[str, str, str], passed: bool, observed: str,
              details: dict[str, Any]) -> Check:
        return Check(spec[0], spec[1], spec[2], Status.PASS if passed else Status.FAIL,
                     observed, self.bench.evidence,
                     round(self.bench.clock() - self.started, 3), details)


def not_tested(spec: tuple[str, str, str], reason: str) -> Check:
    return Check(spec[0], spec[1], spec[2], Status.NOT_TESTED, reason, "none", 0.0, {})


def latency_ms(replies: list[Reply]) -> dict[str, Any]:
    values = sorted(reply.latency_s * 1000 for reply in replies)
    if not values:
        return {"count": 0}
    return {"count": len(values), "min": round(values[0], 2),
            "median": round(statistics.median(values), 2),
            "p95": round(values[int(0.95 * (len(values) - 1))], 2), "max": round(values[-1], 2)}


USB = ("usb.enumeration", "USB enumeration",
       "selected device exists and enumerates as a USB serial device")
UPLOAD = ("firmware.upload", "Firmware upload",
          "flash record shows the production image written and verified (avrdude exit 0)")
IDENTITY = ("firmware.identity", "Production firmware identity",
            "GG1 reports received and no watchdog-test banner seen")
HANDSHAKE = ("gg1.handshake", "GG1 handshake",
             "first valid report within boot_timeout_s of opening the port")
EXCHANGE = ("gg1.exchange", "GG1 exchanges",
            "every state frame answered within reply_timeout_s; valid CRC, matching session "
            "and sequence, constant boot, report sequence +1 per reply")
SESSION = ("gg1.session_change", "Pi session change",
           "a new Pi session is accepted immediately (Pi restart resynchronization)")
REJECTION = ("gg1.rejection", "Rejected frames",
             "duplicate, older, bad-checksum, wrong-version and oversize frames get no reply; "
             "the next valid frame does")
PAUSE = ("link.heartbeat_pause", "Pi heartbeat loss and recovery",
         "after a pause longer than the Uno heartbeat timeout, replies resume without a reset")
UNO_ALARM = ("link.uno_alarm", "Uno link-lost alarm",
             "Uno demands its link-lost alarm while the Pi is silent")
RESET = ("reset.unexpected", "Unexpected Uno reset",
         "after a DTR reset the boot counter increments by 1 and the Pi link flags an "
         "unexpected reset")
RESETS = ("reset.repeated", "Repeated resets",
          "every reset increments the boot counter by exactly 1 and recovers within "
          "boot_timeout_s")
BURST = ("traffic.burst", "Back-to-back traffic",
         "every frame of a back-to-back burst answered within reply_timeout_s")
SUSTAINED = ("traffic.sustained", "Sustained runtime traffic",
             "production Runtime over the real link: no overruns, tick lateness within "
             "max_tick_lateness_s, no communication fault after warm-up, no write failures")
DISCONNECT = ("usb.disconnect", "USB disconnect",
              "unplugging the cable is detected as a transport disconnect")
RECONNECT = ("usb.reconnect", "USB reconnect",
             "after replugging, the transport reconnects as an initial connection")
BACKOFF = ("usb.backoff", "Reconnect backoff",
           "open attempts while unplugged follow initial, doubling, capped intervals")
WATCHDOG_FLASH = ("watchdog.flash_record", "Watchdog-test flash evidence",
                  "last flash record: watchdog-test image matching this build's SHA256SUMS and "
                  "the file on disk, avrdude exit 0, this board's current USB connection, this "
                  "host boot and build, at most 900 s old, integrity intact")
WATCHDOG_IDENTITY = ("watchdog.identity", "Watchdog-test firmware identity",
                     "watchdog-test banner seen and GG1 reports received")
WATCHDOG_RESET = ("watchdog.reset", "Hardware watchdog reset",
                  "each deliberate hang ends in a reset: boot counter +1, reply gap within "
                  "the window, Pi link flags an unexpected reset")
WATCHDOG_CAUSE = ("watchdog.cause", "Watchdog reset cause",
                  "reports after a watchdog reset carry reset cause W")
LIMITATIONS = (
    (("hardware.outputs", "Siren and bed shaker outputs", "outputs driven per alarm demand"),
     "no output pins or drivers exist; requires electrical integration (M7)"),
    (("hardware.button", "Physical button", "debounce, short and long press, stuck detection"),
     "no button pin is assigned; logic is covered only by native unit tests"),
    (("hardware.power", "Power loss and backup power", "alarms survive Pi power loss"),
     "power topology and backup power are undecided (audit H5)"),
    (("hardware.display", "HDMI display", "frames rendered on the 800x480 LCD"),
     "GUI not implemented (M5)"),
)
UNO_ALARM_REASON = ("not observable: the Uno reports nothing while the Pi is silent and has no "
                    "outputs; needs outputs or a diagnostic protocol field")


def check_enumeration(bench: Bench, candidate: Candidate | None) -> Check:
    timer = _Timer(bench)
    present = bench.present(bench.device)
    if not present:
        return timer.check(USB, False, "device not found", {"present": False})
    if candidate is None or candidate.usb_id is None:
        return not_tested(USB, "simulated target: not a USB device")
    return timer.check(USB, True, f"{candidate.description} ({candidate.usb_id})",
                       {"recognized_uno_id": candidate.recognized, "by_id": candidate.by_id})


def check_upload(record: dict[str, Any] | None) -> Check:
    if record is None:
        return not_tested(UPLOAD, "no flash record supplied")
    passed = record.get("variant") == "production" and record.get("avrdude_exit_code") == 0
    observed = (f"variant {record.get('variant')}, avrdude exit {record.get('avrdude_exit_code')}"
                f", sha256 {str(record.get('sha256'))[:12]}")
    status = Status.PASS if passed else Status.FAIL
    return Check(UPLOAD[0], UPLOAD[1], UPLOAD[2], status, observed, "flash-record", 0.0, record)


def check_handshake(bench: Bench, probe: Probe) -> tuple[Check, Reply | None]:
    timer = _Timer(bench)
    reply = probe.first_reply(bench.profile.boot_timeout_s, bench.profile.exchange_interval_s)
    if reply is None:
        return timer.check(HANDSHAKE, False, "no valid report", dict(probe.rejects)), None
    details = {"boot": f"{reply.report.boot:04X}", "cause": reply.report.cause.value,
               "seconds_to_first_reply": round(reply.received_at - timer.started, 3),
               "rejected_before_first_reply": dict(probe.rejects)}
    return timer.check(HANDSHAKE, True, f"boot {reply.report.boot:04X}, cause "
                       f"{reply.report.cause.value}", details), reply


def check_identity(bench: Bench, probe: Probe, first: Reply | None) -> Check:
    timer = _Timer(bench)
    if probe.banner_seen:
        return timer.check(IDENTITY, False, "WATCHDOG-TEST firmware detected: restore "
                           "production firmware before any other use", {})
    if first is None:
        return not_tested(IDENTITY, "no GG1 report received")
    return timer.check(IDENTITY, True, "production GG1 firmware responding", {})


def _exchanges(bench: Bench, probe: Probe, count: int, interval_s: float,
               reference: Reply) -> tuple[list[Reply], int, list[str]]:
    replies: list[Reply] = []
    missing = 0
    anomalies: list[str] = []
    previous = reference
    for _ in range(count):
        reply = probe.exchange(bench.profile.reply_timeout_s)
        if reply is None:
            missing += 1
        else:
            report = reply.report
            expected_seq = (previous.report.seq + 1) % 0x10000
            problems = [
                name for name, bad in (
                    ("not accepted", not reply.accepted),
                    ("boot changed", report.boot != reference.report.boot),
                    (f"seq {report.seq:04X} != {expected_seq:04X}", report.seq != expected_seq),
                    ("cause changed", report.cause != reference.report.cause),
                    ("button stuck", report.button_stuck),
                    ("press counters", report.short_presses or report.long_presses),
                ) if bad]
            anomalies += [f"seq {reply.seq}: {problem}" for problem in problems]
            replies.append(reply)
            previous = reply
        if interval_s:
            bench.sleep(interval_s)
    return replies, missing, anomalies


def check_exchange(bench: Bench, probe: Probe, first: Reply) -> tuple[Check, Reply]:
    timer = _Timer(bench)
    rejects_before = sum(probe.rejects.values())
    replies, missing, anomalies = _exchanges(bench, probe, bench.profile.exchanges,
                                             bench.profile.exchange_interval_s, first)
    new_rejects = sum(probe.rejects.values()) - rejects_before
    passed = not missing and not anomalies and not new_rejects
    latency = latency_ms(replies)
    details = {"latency_ms": latency, "missing": missing, "anomalies": anomalies[:10],
               "rejected_lines": new_rejects}
    observed = (f"{len(replies)}/{bench.profile.exchanges} answered, {len(anomalies)} "
                f"anomalies, p95 {latency.get('p95')} ms")
    return timer.check(EXCHANGE, passed, observed, details), replies[-1] if replies else first


def check_session(bench: Bench, probe: Probe, last: Reply) -> tuple[Check, Reply]:
    timer = _Timer(bench)
    session = probe.link.session ^ 0xA5A5
    probe.use_session(session)
    reply = probe.exchange(bench.profile.reply_timeout_s)
    passed = (reply is not None and reply.report.ack_session == session
              and reply.report.boot == last.report.boot)
    observed = "new session answered" if passed else "new session not answered as expected"
    return timer.check(SESSION, passed, observed, {"session": f"{session:04X}"}), reply or last


def _invalid_frames(session: int, last_seq: int) -> dict[str, bytes]:
    current = encode_state(session, last_seq, BENCH_STATE)
    older = encode_state(session, (last_seq - 1) % 0x10000, BENCH_STATE)
    newer = encode_state(session, (last_seq + 1) % 0x10000, BENCH_STATE)
    body = newer[:newer.rindex(b"*")]
    checksum = crc16(body)
    bad = body + b"*%04X\n" % (checksum ^ 0xFFFF)
    version_body = b"GG2" + body[3:]
    return {"duplicate": current, "older": older, "bad_checksum": bad,
            "wrong_version": version_body + b"*%04X\n" % crc16(version_body),
            "oversize": b"G" * 100 + b"\n"}


def check_rejection(bench: Bench, probe: Probe, last: Reply) -> tuple[Check, Reply]:
    timer = _Timer(bench)
    answered = []
    for name, frame in _invalid_frames(probe.link.session, last.seq).items():
        probe.send_raw(frame)
        if probe.listen(bench.profile.silence_window_s):
            answered.append(name)
    recovery = probe.exchange(bench.profile.reply_timeout_s)
    passed = not answered and recovery is not None and recovery.seq != last.seq
    observed = ("all invalid frames ignored, next valid frame answered" if passed else
                f"answered invalid frames: {answered or 'none'}; recovery "
                f"{'ok' if recovery else 'missing'}")
    return timer.check(REJECTION, passed, observed, {"answered": answered}), recovery or last


def check_pause(bench: Bench, probe: Probe, last: Reply) -> tuple[Check, Reply]:
    timer = _Timer(bench)
    bench.sleep(bench.profile.heartbeat_pause_s)
    reply = probe.first_reply(bench.profile.boot_timeout_s, bench.profile.exchange_interval_s)
    passed = reply is not None and reply.report.boot == last.report.boot and reply.accepted
    observed = (f"replies resumed after {bench.profile.heartbeat_pause_s} s without a reset"
                if passed else "no clean resumption after the pause")
    return timer.check(PAUSE, passed, observed,
                       {"pause_s": bench.profile.heartbeat_pause_s}), reply or last


def _reset_once(bench: Bench, probe: Probe, last: Reply) -> tuple[Reply | None, float]:
    started = bench.clock()
    bench.reset(probe.port)
    deadline = started + bench.profile.boot_timeout_s
    while bench.clock() < deadline:
        reply = probe.exchange(bench.profile.exchange_interval_s)
        if reply is not None and reply.report.boot != last.report.boot:
            return reply, bench.clock() - started
    return None, bench.clock() - started


def _reset_ok(last: Reply, reply: Reply | None) -> bool:
    return (reply is not None and reply.report.boot == (last.report.boot + 1) % 0x10000
            and reply.events.unexpected_reset)


def check_reset(bench: Bench, probe: Probe, last: Reply) -> tuple[Check, Reply]:
    timer = _Timer(bench)
    reply, recovery_s = _reset_once(bench, probe, last)
    passed = _reset_ok(last, reply)
    details: dict[str, Any] = {"old_boot": f"{last.report.boot:04X}",
                               "recovery_s": round(recovery_s, 3)}
    if reply is not None:
        details |= {"new_boot": f"{reply.report.boot:04X}", "cause": reply.report.cause.value,
                    "flagged_unexpected": reply.events.unexpected_reset}
    observed = (f"boot {details['old_boot']} -> {details.get('new_boot', 'none')}, cause "
                f"{details.get('cause', '-')}, recovered in {details['recovery_s']} s")
    return timer.check(RESET, passed, observed, details), reply or last


def check_resets(bench: Bench, probe: Probe, last: Reply) -> tuple[Check, Reply]:
    timer = _Timer(bench)
    recoveries: list[float] = []
    failures = 0
    for _ in range(bench.profile.resets):
        reply, recovery_s = _reset_once(bench, probe, last)
        failures += not _reset_ok(last, reply)
        recoveries.append(round(recovery_s, 3))
        if reply is None:
            break
        last = reply
    passed = not failures and len(recoveries) == bench.profile.resets
    observed = (f"{len(recoveries) - failures}/{bench.profile.resets} resets clean, max "
                f"recovery {max(recoveries) if recoveries else '-'} s")
    return timer.check(RESETS, passed, observed, {"recovery_s": recoveries}), last


def check_burst(bench: Bench, probe: Probe, last: Reply) -> tuple[Check, Reply]:
    timer = _Timer(bench)
    replies, missing, anomalies = _exchanges(bench, probe, bench.profile.burst_exchanges, 0,
                                             last)
    elapsed = bench.clock() - timer.started
    passed = not missing and not anomalies
    details = {"latency_ms": latency_ms(replies), "missing": missing,
               "anomalies": anomalies[:10],
               "exchanges_per_s": round(len(replies) / elapsed, 1) if elapsed else None}
    observed = (f"{len(replies)}/{bench.profile.burst_exchanges} answered, "
                f"{details['exchanges_per_s']} exchanges/s")
    return timer.check(BURST, passed, observed, details), replies[-1] if replies else last


class _TimingDisplay(MemoryDisplay):
    def __init__(self, clock: Callable[[], float]) -> None:
        super().__init__()
        self.clock = clock
        self.ticks: list[tuple[float, Frame]] = []

    def render(self, frame: Frame) -> None:
        self.ticks.append((self.clock(), frame))


def check_sustained(bench: Bench) -> Check:
    timer = _Timer(bench)
    profile = bench.profile
    transport = SerialTransport(device=bench.device, session=bench.session ^ 0x5A5A,
                                reconnect_initial_s=profile.backoff_initial_s,
                                reconnect_max_s=profile.backoff_max_s,
                                open_port=bench.transport_opener)
    display = _TimingDisplay(bench.clock)
    settings = RuntimeSettings(tick_s=profile.sustained_tick_s, fetch_interval_s=60,
                               fetch_timeout_s=10, clock_check_interval_s=60,
                               clock_check_timeout_s=5)
    runtime = Runtime(policy=BENCH_POLICY, settings=settings,
                      provider=SimulatedProvider((110,)), clock_check=lambda: True,
                      transport=transport, display=display, wall_clock=lambda: datetime.now(UTC),
                      monotonic=bench.clock, acquisition_spawn=run_inline,
                      clock_spawn=run_inline)
    usage_before = resource.getrusage(resource.RUSAGE_SELF)
    start = bench.clock()
    end = start + profile.sustained_s

    def wait(timeout_s: float) -> bool:
        if bench.clock() + timeout_s >= end:
            return True
        bench.sleep(timeout_s)
        return False

    try:
        runtime.run(wait)
    finally:
        transport.close()
    usage_after = resource.getrusage(resource.RUSAGE_SELF)
    wall = bench.clock() - start
    lateness = [moment - (start + index * profile.sustained_tick_s)
                for index, (moment, _) in enumerate(display.ticks)]
    warm = start + profile.boot_timeout_s
    late_faults = sum(1 for moment, frame in display.ticks
                      if moment >= warm and Fault.COMMUNICATION in frame.faults)
    write_failures = transport.counts["incomplete_writes"] + transport.counts["failed_writes"]
    max_late = max(lateness) if lateness else float("inf")
    passed = (runtime.counts["overruns"] == 0 and max_late <= profile.max_tick_lateness_s
              and late_faults == 0 and write_failures == 0
              and transport.counts["disconnects"] == 0 and bool(display.ticks))
    cpu = (usage_after.ru_utime + usage_after.ru_stime
           - usage_before.ru_utime - usage_before.ru_stime)
    details = {"ticks": len(display.ticks), "overruns": runtime.counts["overruns"],
               "max_tick_lateness_ms": round(max_late * 1000, 2),
               "communication_fault_ticks_after_warmup": late_faults,
               "frames_sent": transport.counts["frames_sent"], "write_failures": write_failures,
               "disconnects": transport.counts["disconnects"],
               "link_counts": dict(transport.link.counts),
               "cpu_percent": round(100 * cpu / wall, 1) if wall else None,
               "max_rss_kb": usage_after.ru_maxrss}
    observed = (f"{len(display.ticks)} ticks, max lateness {details['max_tick_lateness_ms']} ms, "
                f"{late_faults} fault ticks after warm-up, CPU {details['cpu_percent']}%")
    return timer.check(SUSTAINED, passed, observed, details)


def _expected_delays(profile: Profile, count: int) -> list[float]:
    delays = []
    backoff = profile.backoff_initial_s
    for _ in range(count):
        delays.append(backoff)
        backoff = min(backoff * 2, profile.backoff_max_s)
    return delays


def check_replug(bench: Bench) -> list[Check]:
    if not bench.interactive:
        reason = "requires --interactive and physical access to the USB cable"
        return [not_tested(spec, reason) for spec in (DISCONNECT, RECONNECT, BACKOFF)]
    profile = bench.profile
    timer = _Timer(bench)
    attempts: list[float] = []

    def opener(device: str) -> Port:
        attempts.append(bench.clock())
        return bench.transport_opener(device)

    transport = SerialTransport(device=bench.device, session=bench.session ^ 0x3C3C,
                                reconnect_initial_s=profile.backoff_initial_s,
                                reconnect_max_s=profile.backoff_max_s, open_port=opener)
    resets_flagged = 0

    def step() -> None:
        nonlocal resets_flagged
        now = bench.clock()
        resets_flagged += transport.receive(now).unexpected_reset
        transport.send(BENCH_STATE, now)
        bench.sleep(profile.sustained_tick_s)

    def until(condition: Callable[[], bool], timeout_s: float) -> bool:
        deadline = bench.clock() + timeout_s
        while bench.clock() < deadline:
            step()
            if condition():
                return True
        return False

    def linked() -> bool:
        return transport.connected and transport.link.age_s(bench.clock()) <= 2 * (
            profile.sustained_tick_s + profile.reply_timeout_s)

    try:
        if not until(linked, profile.boot_timeout_s):
            reason = "link not established before the unplug step"
            return [timer.check(spec, False, reason, {}) for spec in (DISCONNECT, RECONNECT,
                                                                      BACKOFF)]
        bench.prompt("UNPLUG the Uno USB cable now.")
        detected = until(lambda: transport.counts["disconnects"] >= 1,
                         profile.replug_timeout_s)
        lost_at = bench.clock()
        disconnect = timer.check(DISCONNECT, detected and not bench.present(bench.device),
                                 "disconnect detected" if detected else "no disconnect detected",
                                 {"device_present": bench.present(bench.device)})
        if not detected:
            return [disconnect, not_tested(RECONNECT, "no disconnect"),
                    not_tested(BACKOFF, "no disconnect")]
        bench.prompt(f"Keep the cable unplugged for about {profile.unplugged_s:g} s; you will "
                     "be told when to plug it back in.")
        until(lambda: False, profile.unplugged_s)
        window = [moment for moment in attempts if moment > lost_at]
        early = transport.connected
        bench.prompt("PLUG the Uno USB cable back in now.")
        connections_before = transport.link.counts["connections"]
        reconnected = until(lambda: linked() and transport.link.counts["connections"]
                            > connections_before, profile.replug_timeout_s)
    finally:
        transport.close()
    delays = [round(b - a, 3) for a, b in zip([lost_at, *window], window, strict=False)]
    expected = _expected_delays(profile, len(delays))
    tolerance = profile.sustained_tick_s + profile.reply_timeout_s
    backoff_ok = (len(delays) >= 3 and not early
                  and all(abs(d - e) <= tolerance for d, e in zip(delays, expected, strict=True)))
    reconnect = timer.check(RECONNECT, reconnected and not early and resets_flagged == 0,
                            "reconnected as an initial connection" if reconnected
                            else "no reconnection", {"unexpected_resets_flagged": resets_flagged,
                                                     "reconnected_early": early})
    backoff = timer.check(BACKOFF, backoff_ok, f"delays {delays} vs expected {expected}",
                          {"delays_s": delays, "expected_s": expected,
                           "tolerance_s": tolerance})
    return [disconnect, reconnect, backoff]


def limitations() -> list[Check]:
    return [not_tested(spec, reason) for spec, reason in LIMITATIONS]


def run_suite(bench: Bench, candidate: Candidate | None,
              flash_record: dict[str, Any] | None) -> list[Check]:
    checks = [check_enumeration(bench, candidate), check_upload(flash_record)]
    link_checks = (EXCHANGE, SESSION, REJECTION, PAUSE, RESET, RESETS, BURST, SUSTAINED,
                   DISCONNECT, RECONNECT, BACKOFF)
    if not bench.present(bench.device):
        return checks + [not_tested(spec, "device not present")
                         for spec in (HANDSHAKE, IDENTITY, *link_checks)] + limitations()
    port = bench.open_port(bench.device)
    try:
        probe = Probe(port, session=bench.session, clock=bench.clock)
        handshake, last = check_handshake(bench, probe)
        checks += [handshake, check_identity(bench, probe, last)]
        if last is None or probe.banner_seen:
            reason = "no production GG1 link" if last is None else "watchdog-test firmware"
            return checks + [not_tested(spec, reason) for spec in link_checks] + [
                not_tested(UNO_ALARM, UNO_ALARM_REASON)] + limitations()
        for step in (check_exchange, check_session, check_rejection, check_pause):
            check, last = step(bench, probe, last)
            checks.append(check)
        checks.append(not_tested(UNO_ALARM, UNO_ALARM_REASON))
        for step in (check_reset, check_resets, check_burst):
            check, last = step(bench, probe, last)
            checks.append(check)
    finally:
        port.close()
    checks.append(check_sustained(bench))
    checks += check_replug(bench)
    return checks + limitations()


def run_watchdog(bench: Bench) -> list[Check]:
    profile = bench.profile
    if not bench.present(bench.device):
        return [not_tested(spec, "device not present")
                for spec in (WATCHDOG_IDENTITY, WATCHDOG_RESET, WATCHDOG_CAUSE)]
    port = bench.open_port(bench.device)
    try:
        probe = Probe(port, session=bench.session, clock=bench.clock)
        timer = _Timer(bench)
        last = probe.first_reply(profile.boot_timeout_s, profile.exchange_interval_s)
        identified = last is not None and probe.banner_seen
        identity = timer.check(WATCHDOG_IDENTITY, identified,
                               "watchdog-test firmware responding" if identified else
                               "watchdog-test banner not seen; is production firmware loaded?",
                               {"banner_seen": probe.banner_seen, "replied": last is not None})
        if last is None or not identified:
            return [identity, not_tested(WATCHDOG_RESET, "watchdog-test firmware not confirmed"),
                    not_tested(WATCHDOG_CAUSE, "watchdog-test firmware not confirmed")]
        timer = _Timer(bench)
        cycles: list[dict[str, Any]] = []
        for _ in range(profile.watchdog_cycles):
            deadline = bench.clock() + profile.watchdog_cycle_timeout_s
            new = None
            while new is None and bench.clock() < deadline:
                reply = probe.exchange(profile.reply_timeout_s)
                if reply is not None and reply.report.boot != last.report.boot:
                    new = reply
                elif reply is not None:
                    last = reply
                    bench.sleep(profile.exchange_interval_s)
            if new is None:
                break
            cycles.append({"old_boot": f"{last.report.boot:04X}",
                           "new_boot": f"{new.report.boot:04X}",
                           "increment_ok": new.report.boot == (last.report.boot + 1) % 0x10000,
                           "gap_s": round(new.received_at - last.received_at, 3),
                           "cause": new.report.cause.value,
                           "flagged_unexpected": new.events.unexpected_reset})
            last = new
    finally:
        port.close()
    reset_ok = len(cycles) == profile.watchdog_cycles and all(
        cycle["increment_ok"] and cycle["flagged_unexpected"]
        and profile.watchdog_gap_min_s <= cycle["gap_s"] <= profile.watchdog_gap_max_s
        for cycle in cycles)
    causes = [cycle["cause"] for cycle in cycles]
    reset = timer.check(WATCHDOG_RESET, reset_ok,
                        f"{len(cycles)}/{profile.watchdog_cycles} watchdog resets observed, gaps "
                        f"{[cycle['gap_s'] for cycle in cycles]} s", {"cycles": cycles})
    cause_ok = bool(causes) and all(cause == "W" for cause in causes)
    cause = timer.check(WATCHDOG_CAUSE, cause_ok, f"reported causes {causes}",
                        {"note": "U means the bootloader cleared MCUSR before the sketch "
                                 "read it (see docs/HARDWARE.md)"})
    return [identity, reset, cause]


def run_watchdog_verified(bench: Bench, verify: Callable[[], dict[str, Any]]) -> list[Check]:
    try:
        record = verify()
    except AuthorizationError as error:
        return [Check(*WATCHDOG_FLASH, Status.FAIL, str(error), "flash-record", 0.0, {}),
                *(not_tested(spec, "flash record not verified; the port was not opened")
                  for spec in (WATCHDOG_IDENTITY, WATCHDOG_RESET, WATCHDOG_CAUSE))]
    evidence = Check(*WATCHDOG_FLASH, Status.PASS,
                     f"watchdog-test image sha256 {record['sha256'][:12]} flashed at "
                     f"{record['utc']}, avrdude exit 0", "flash-record", 0.0, record)
    return [evidence, *run_watchdog(bench)]
