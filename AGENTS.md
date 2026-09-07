# Reconize — Codex Agent Instructions

## Mandatory Startup Procedure

`AGENTS.md` is the entry-point instruction file for this repository.

After loading `AGENTS.md`, before doing ANY project work, read these files in this exact order:

1. `PROJECT_RULES.md`
2. `PROJECT_CONTEXT.md`
3. `CURRENT_TASK.md`
4. `CLAUDE.md` when relevant for historical Claude-specific project guidance

Do not modify code before reading the required project instruction files.

The effective startup order is:

`AGENTS.md`
→ `PROJECT_RULES.md`
→ `PROJECT_CONTEXT.md`
→ `CURRENT_TASK.md`
→ `CLAUDE.md` when relevant

These files are the authoritative project instructions and handoff context.

Do not rely on chat history alone.

Do not modify code before reading all three files.

---

## Instruction Priority

When working in this repository, follow this priority:

1. The user's explicit instruction in the current conversation
2. `PROJECT_RULES.md`
3. `CURRENT_TASK.md`
4. `PROJECT_CONTEXT.md`
5. Existing implementation and project documentation

If instructions appear to conflict, STOP and report the conflict instead of guessing.

---

## Main Safety

This repository is MAIN:

`E:\งาน\491\Face_Reconize\Reconize`

MAIN contains the real project and real project data.

The isolated development environment is:

`E:\งาน\491\Face_Reconize\Reconize_Dummy`

Never modify MAIN first for a new project/code change.

Follow the mandatory workflow defined in `PROJECT_RULES.md`:

DUMMY
→ AUTOMATED TEST
→ USER MANUAL / VISUAL TEST WHEN APPLICABLE
→ REPORT
→ EXPLICIT USER APPROVAL
→ MAIN
→ USER VERIFIES MAIN

Do not interpret test success as permission to modify MAIN.

---

## Existing System

Reconize is an existing working system.

Do not treat this repository as a new project.

Before modifying an existing feature:

- inspect the current implementation
- understand existing behavior
- preserve unrelated functionality
- preserve real data
- make the smallest change necessary for the requested task

Do not rewrite working architecture simply because another implementation appears cleaner.

---

## Scope Discipline

Work only on the user's current task.

Do not automatically:

- start another feature
- refactor unrelated code
- fix unrelated issues
- redesign unrelated UI
- change architecture
- modify recognition settings
- change database semantics
- commit
- push
- tag
- release
- deploy
- change VERSION

unless explicitly authorized.

If you discover an unrelated issue, report it without fixing it.

---

## Current Task

Always read `CURRENT_TASK.md` before acting.

The current task file tells you what work is currently active and what has not yet been verified.

Do not assume an active task is complete until the user explicitly confirms completion.

Do not start future goals listed in `PROJECT_CONTEXT.md` unless the user explicitly requests them.

---

## Before Editing

Before making any code change, report:

1. what you understand the requested task to be
2. which environment should be modified
3. which files you expect may need modification
4. what existing behavior must be preserved

Then follow `PROJECT_RULES.md`.

For a new task, implementation normally begins in Dummy, not Main.

---

## Data Protection

Never use real MAIN data for destructive testing.

Do not delete, reset, replace, migrate, or seed MAIN data unless the user explicitly authorizes that exact operation.

Never copy Dummy databases, storage, test participants, or test records into MAIN.

---

## Recognition Protection

The existing recognition architecture is intentional.

Do not change the InsightFace model, embeddings, thresholds, recognition index, CUDA/GPU setup, multi-face behavior, attendance semantics, enrollment behavior, or consent semantics unless explicitly requested.

Refer to `PROJECT_RULES.md` and `PROJECT_CONTEXT.md` for the authoritative details.

---

## CCTV Protection

The existing CCTV Live View is an established workflow.

Do not recreate a separate camera workflow or rewrite the CCTV architecture unless explicitly requested.

Preserve the existing:

- multi-camera streams
- camera persistence
- stable camera identity
- no-flicker preview behavior
- activity-controlled detection
- integrated recording
- face count
- Live Recognition Feed
- Global Session History
- Per-Camera Session History

Refer to `PROJECT_CONTEXT.md` for details.

---

## Testing and Approval

For visible or interactive changes:

- implement in Dummy
- run automated tests
- start Dummy
- provide the exact Dummy URL
- provide exact manual test instructions
- STOP and wait

The user must personally test and explicitly approve the change before it can be applied to Main.

---

## Completion Behavior

When the requested task is finished:

STOP.

Report:

- what changed
- files changed
- tests performed
- test results
- anything the user must manually verify

Do not continue to another task until instructed.

---

## Core Rule

When uncertain:

DO NOT MODIFY MAIN.

DO NOT MODIFY UNRELATED CODE.

DO NOT ASSUME APPROVAL.

STOP AND ASK.