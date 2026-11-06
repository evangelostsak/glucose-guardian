@AGENTS.md

# Claude Code conventions

AGENTS.md above is the single source for engineering rules, commands and the handoff
protocol. This file adds only what is specific to Claude Code sessions; do not copy
AGENTS.md content here.

## Documents

Keep these current after every substantial task:

- `docs/ARCHITECTURE.md`: current architecture and technical decisions
- `docs/SAFETY.md`: failure modes, alarm behavior, safety limitations
- `docs/PROJECT_STATUS.md`: completed work, current status, next steps
- `docs/DEVELOPMENT_LOG.md`: per-task entry required by the handoff protocol
- `docs/HARDWARE.md`: electrical interface assumptions
- `docs/audits/`: dated audits; never rewrite a past audit's findings (link fixes only), add a new one

Rename files with `git mv`; never create files that differ from an existing one only in
capitalization.

## Collaboration

- ChatGPT is used separately for architecture decisions, hardware planning and
  independent code reviews. Write PR descriptions and documents so an outside reviewer
  can verify every claim.
- Present findings and a plan, then wait for approval, before significant architectural
  changes or starting a new roadmap milestone.
- Work on feature branches and open pull requests; never push to `main`.

## Git

- No AI attribution anywhere: no `Co-Authored-By` trailers, no "Generated with" footers.
- Signed commits, single-line subjects, one commit per file.
