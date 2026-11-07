"""Bench report model with JSON and Markdown writers."""
import json
from dataclasses import asdict, dataclass
from enum import Enum
from pathlib import Path


class Status(str, Enum):
    PASS = "PASS"
    FAIL = "FAIL"
    NOT_TESTED = "NOT TESTED"


@dataclass(frozen=True)
class Check:
    id: str
    title: str
    expected: str
    status: Status
    observed: str
    evidence: str
    duration_s: float
    details: dict[str, object]


@dataclass(frozen=True)
class Target:
    device: str
    physical: bool
    usb_id: str | None
    description: str | None


@dataclass(frozen=True)
class Report:
    command: str
    started_utc: str
    finished_utc: str
    target: Target
    environment: dict[str, str]
    checks: tuple[Check, ...]

    @property
    def verdict(self) -> Status:
        statuses = {check.status for check in self.checks}
        if Status.FAIL in statuses:
            return Status.FAIL
        return Status.PASS if Status.PASS in statuses else Status.NOT_TESTED


def to_json(report: Report) -> str:
    data = asdict(report)
    data["verdict"] = report.verdict.value
    return json.dumps(data, indent=2, default=str) + "\n"


def to_markdown(report: Report) -> str:
    lines = [f"# Bench report: {report.command}", ""]
    if not report.target.physical:
        lines += ["> **SIMULATED TARGET — NOT HARDWARE VALIDATION.** No physical Uno was "
                  "tested in this run.", ""]
    lines += [
        f"- Verdict: **{report.verdict.value}**",
        f"- Started: {report.started_utc}",
        f"- Finished: {report.finished_utc}",
        f"- Device: `{report.target.device}`",
        f"- USB ID: {report.target.usb_id or 'none'} ({report.target.description or 'unknown'})",
    ]
    lines += [f"- {key}: {value}" for key, value in sorted(report.environment.items())]
    lines += ["", "| Status | Check | Expected | Observed | Evidence |", "|---|---|---|---|---|"]
    for check in report.checks:
        lines.append(f"| {check.status.value} | `{check.id}` {check.title} | {check.expected} | "
                     f"{check.observed} | {check.evidence} |")
    return "\n".join(lines) + "\n"


def write(report: Report, directory: Path) -> tuple[Path, Path]:
    directory.mkdir(parents=True, exist_ok=True)
    json_path = directory / "report.json"
    markdown_path = directory / "report.md"
    json_path.write_text(to_json(report))
    markdown_path.write_text(to_markdown(report))
    return json_path, markdown_path
