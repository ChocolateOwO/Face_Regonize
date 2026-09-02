# Project rules — Reconize

These rules apply to every future session working in this repository, until the user changes them.

## 1. Scope discipline

Only touch what the current request is actually about. Do not modify, refactor, "clean up," or improve files/features
that are not relevant to the specific order being worked on, even if you notice something else that could be
improved. If something outside scope looks broken or worth changing, mention it — don't fix it unless asked.

## 2. Surface real choices — don't decide them silently

When implementing a request involves a genuine choice with more than one reasonable answer — a visual/color choice,
which of several valid logic/behavior options to use, a data-retention or business-logic decision, anything where a
different reasonable person could pick differently — **stop and ask the user to decide**, e.g. via option A/B with
tradeoffs. Do not silently pick one and move on.

This does not apply to routine implementation details that don't have real competing alternatives (e.g. naming a
variable, which existing UI component to reuse, standard CRUD wiring) — those can be decided and implemented
directly. The bar is: if a reasonable person could disagree about the *right* answer (not just style), ask first.

## 3. Terse output

Minimum words. No filler, no praise, no restating the request, no long summaries. State the fact or the result.
Bullets over paragraphs. Skip preambles like "I'll now..." — just do it and report the outcome in one or two lines.

## 4. Pre-authorized commands

Bash and PowerShell commands in this repo are pre-approved — run them without asking, including `rm` of files
this project created (scratchpad files, generated output, temp scripts, batch storage dirs).

Still confirm first for: deleting or overwriting `database/app.db` or `storage/people/`, `git reset --hard`,
`git push --force`, and anything that destroys user data with no copy left.

## 5. Test on dummy data first, never on the real files

Never verify a change by running it against the real database, the real `storage/people/` photos, or the user's real
Google Drive folder. Test on dummy/throwaway copies first — a scratch DB, dummy participants, a dummy Drive folder,
sample images. Only after it demonstrably works on the dummy does it get applied to, or run against, the real data.

This applies to schema migrations, batch/bulk operations, delete or cleanup logic, retention expiry, imports, and
anything that writes or deletes files. Report the dummy-run result first, then ask before touching the real data.
