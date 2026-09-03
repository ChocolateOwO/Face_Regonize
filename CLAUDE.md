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

## 5. The permanent development workflow — Dummy first, always

Every code change follows this sequence, in order:

```
DUMMY
  → AUTOMATED TEST
  → USER MANUAL/VISUAL TEST (when applicable)
  → REPORT
  → EXPLICIT USER APPROVAL
  → MAIN
  → USER VERIFIES MAIN
```

```
Main:   E:\งาน\491\Face_Reconize\Reconize
Dummy:  E:\งาน\491\Face_Reconize\Reconize_Dummy
```

**5.1 — All code changes go to Dummy first.** Never develop a change by editing Main. Copy what you need into the
Dummy, build and test it there, and report. Main stays untouched until the user has seen the result and explicitly
approved applying it.

**5.2 — The Dummy must be isolated from Main** wherever it matters: separate frontend port, separate backend port,
separate database, separate storage directory, separate test data. Never verify a change by running it against the
real database, the real `storage/people/` photos, or the user's real Google Drive folder. This applies in particular
to schema migrations, batch/bulk operations, delete or cleanup logic, retention expiry, imports, and anything that
writes or deletes files.

**5.3 — Automated test success is NOT permission to modify Main.** Neither is a passing build, an obvious bug, a
one-line change, a previous approval on a related task, or your own confidence.

**5.4 — For anything visible, clickable or interactive** (UI, photo previews, participant pages, import, kiosk,
camera, recognition screens, PDPA, Settings, photo processing, navigation, buttons, forms): finish the automated
tests first, then start the Dummy site, give the user the Dummy URL (and the backend URL when relevant), give exact
manual test steps, and **STOP**. Wait for the user's manual testing and explicit approval.

**5.5 — Once the Dummy is handed over for manual testing, do not mutate Dummy data** until the user says testing is
finished. Do not keep running tests against it in the background.

**5.6 — Never run destructive photo replacement/swap tests against participants the user is visually testing.**
Photo-replacement visual testing is performed manually by the user. If automated destructive testing is genuinely
required, use dedicated throwaway test participants/data and finish it *before* handing the Dummy over.

**5.7 — Backend-only changes with no visible behavior** may skip the user visual test, but still require:
`DUMMY → AUTOMATED TEST → REPORT → EXPLICIT USER APPROVAL → MAIN`.

**5.8 — Before applying an approved change to Main, create and verify a patch backup** of every affected Main file
(rule 6).

**5.9 — After applying a visible change to Main,** make Main available and tell the user exactly what to verify. The
change is not complete until the user has had that opportunity.

**5.10 — Never commit, push, tag, release, publish or deploy** merely because testing passed. Each of those needs its
own explicit instruction.

**5.11 — When uncertain whether a change may go directly to Main: it may not.** Use the Dummy and ask for approval.

Only the user can authorize moving a tested change from Dummy to Main — for example "Apply to Main",
"Approved for Main", "เอาเข้า Main", "ผ่าน เอาเข้าเว็บจริง". A recommendation is not approval, and the user testing
the Dummy successfully is not approval either unless they explicitly authorize applying it.

The Dummy is where mistakes are allowed to happen. Main is not.

## 6. Patch backups before overwriting live files

Before a live file is modified or replaced, save its current version under `patch/`, in a folder named with a
timestamp and the key thing being changed:

```
patch/YYYY-MM-DD_HHMM_<key-change>/
    <original relative path of each file replaced>
    NOTES.md      what changed and why, plus how to restore
```

Example: `patch/2026-09-02_1730_sidebar-scroll-fix/frontend/src/components/Layout.tsx`

Keep the original directory structure inside the patch folder so a restore is a straight copy back. Verify the backup
copies are byte-identical to the originals before editing anything. Never delete a patch folder as part of another
task.
