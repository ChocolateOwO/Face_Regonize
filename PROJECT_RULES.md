# Reconize Project Rules

## 1. Golden Rule

DO THE CURRENT TASK ONLY.

Do not suggest, implement, or start the next feature until the user explicitly says the current task is finished or asks for the next task.

Do not branch into unrelated work.

When the requested task is complete:
- STOP
- REPORT
- WAIT for explicit user approval or the next instruction

---

## 2. Mandatory Development Workflow

For ANY project or code change, always follow this order:

DUMMY
→ AUTOMATED TEST
→ USER MANUAL / VISUAL TEST WHEN APPLICABLE
→ REPORT
→ EXPLICIT USER APPROVAL
→ MAIN
→ USER VERIFIES MAIN

Never modify Main first.

Passing automated tests is NOT approval.

A successful Dummy test is NOT permission to modify Main.

Claude, Codex, or any other agent recommendation is NOT approval.

Only the user can explicitly authorize moving the approved change from Dummy to Main.

---

## 3. Main and Dummy Paths

MAIN:

E:\งาน\491\Face_Reconize\Reconize

DUMMY:

E:\งาน\491\Face_Reconize\Reconize_Dummy

Main contains the real project and real project data.

Dummy is the implementation and testing environment.

Do not confuse these two folders.

---

## 4. Main Protection

Never use Main as the first implementation environment.

Never perform destructive testing on Main.

Never:

- replace the Main database
- copy Dummy database into Main
- delete Main participant data
- delete Main attendance data
- delete Main consent data
- delete Main recognition history
- delete Main activities
- delete Main event-photo data
- overwrite Main storage with Dummy storage
- copy Dummy test data into Main
- reset Main tables
- seed synthetic test data into Main
- blindly replace Main files with Dummy files
- remove unrelated Main functionality
- change unrelated Main behavior
- refactor unrelated Main code while applying another feature

Main-specific unrelated code always wins over Dummy when merging.

Apply only the user-approved feature or fix.

---

## 5. Backup Before Main Changes

Before applying any approved change to Main:

1. Identify every Main file that will be modified.
2. Create a patch backup of those files.
3. Verify the backup exists and is readable.
4. Compare Main and Dummy carefully.
5. Apply only the approved change surgically.

Do not blindly copy a whole Dummy file over Main if Main contains unrelated changes.

---

## 6. No Automatic Git or Release Actions

Passing tests or finishing code does NOT authorize:

- git commit
- git push
- git tag
- GitHub Release
- publishing
- deployment
- changing VERSION
- packaging a release

These actions require separate explicit user instruction.

Never assume permission.

---

## 7. Testing Rules

Use isolated Dummy/test data.

Do not test destructive behavior against real Main participants or real Main data.

If the task affects visible, clickable, interactive, or UI behavior:

1. Implement in Dummy.
2. Run automated tests.
3. Start Dummy.
4. Give the user the exact Dummy URL.
5. Give the user exact manual test steps.
6. STOP.
7. Wait for the user to personally test it.
8. Wait for explicit approval before touching Main.

Automated testing alone is not sufficient for UI changes.

If a task is backend-only and has no meaningful visual/manual test, automated Dummy testing and a report are still required before Main approval.

---

## 8. Dummy Handoff Protection

Once Dummy has been handed to the user for manual testing:

Do NOT mutate the Dummy test data or environment used by the user until the user says testing is finished.

Do not run destructive automated tests against the same participants or data currently being used for the user's visual test.

If destructive testing is genuinely necessary, use separate throwaway test participants/data and complete that testing BEFORE handing Dummy to the user.

---

## 9. Scope Control

Do not change anything outside the requested task.

Do not:

- redesign unrelated pages
- clean up unrelated code
- rename unrelated variables
- reorganize unrelated files
- rewrite working architecture
- change existing behavior because it seems cleaner
- fix unrelated bugs without permission
- add features not explicitly requested

If an unrelated issue is discovered, report it only.

Do not fix it unless the user explicitly requests it.

---

## 10. Recognition Engine Protection

Do not change the face-recognition engine unless explicitly requested.

Current recognition behavior that must be preserved includes:

- InsightFace
- model: buffalo_l
- detection + recognition modules
- embedding dimension: 512
- float32 embeddings
- L2-normalized embeddings
- recognition threshold: 0.45
- detection threshold: 0.5
- det_size: 320x320
- in-memory vectorized recognition index
- multi-face recognition
- existing attendance semantics
- existing participant matching behavior

Do not silently change model, threshold, embedding format, index logic, recognition logic, or attendance behavior.

---

## 11. GPU Protection

The current Main environment has a working CUDA/GPU setup for InsightFace.

Do not change, remove, or bypass the existing CUDA/GPU initialization unless explicitly requested.

Do not silently fall back to a different recognition architecture.

---

## 12. Enrollment Protection

Participant enrollment currently supports:

- HEIC / HEIF profile images
- normal image formats
- primary enrollment face selection

Enrollment rule:

- 0 faces → reject
- 1 face → use that face
- 2 or more faces:
  - calculate largest detected face bbox area divided by the sum of all detected face bbox areas
  - if largest ratio >= 0.60 → use only the largest face
  - if largest ratio < 0.60 → reject as multiple prominent faces

This rule applies to enrollment/profile photos only.

Do not apply this rule to kiosk recognition or event photos.

---

## 13. PDPA / Consent Protection

Consent does NOT prevent recognition or attendance.

A participant may still be recognized and checked in even if they declined consent.

Consent history is append-only.

Current consent can come from sources such as:

- kiosk
- registration
- admin

Registration sync must compare against the latest registration-source record.

An unchanged registration answer must not repeatedly append duplicate consent records.

A newer kiosk answer must not be overwritten by an unchanged registration answer.

If the latest overall consent source is admin, registration sync must not overwrite it.

Do not change these semantics unless explicitly requested.

---

## 14. Event Photo Processing Protection

Existing event-photo processing behavior must be preserved unless explicitly requested.

The processing pipeline has separate local-processing and Drive-sync phases.

Do not silently rewrite this pipeline.

Do not change:

- participant matching
- consent snapshot semantics
- SORTED folder behavior
- MEDIA blur behavior
- Drive output behavior

without explicit user instruction.

---

## 15. CCTV Is the Main Camera Workflow

The current user-facing camera workflow is CCTV Live View.

Do not reintroduce a separate redundant Camera page unless explicitly requested.

Current CCTV behavior includes:

- multiple physical cameras on one Windows PC
- support for approximately 7 cameras
- checkbox camera selection
- persistent camera selection
- stable deviceId to cameraId mapping
- automatic restore after navigation or refresh
- smooth live MediaStream preview
- no recognition-triggered preview flicker
- one Local Recognition Agent
- face count per camera
- all detected faces count, including unknown faces
- recognition only when Detection Session is active
- Activity selection
- Start Detection
- Stop Detection
- camera preview remains live when detection is stopped
- integrated per-camera recording
- multiple cameras may record simultaneously
- recording reuses the existing MediaStream
- recording must not open the same camera again
- stopping recording must not stop the live camera
- Live Recognition Feed
- Global Session History
- Per-Camera Session History

Do not rebuild or replace this architecture unless explicitly requested.

---

## 16. CCTV Live Recognition Feed Rules

Live Recognition Feed uses the existing recognition result.

Never run a second AI inference just for the feed.

Rules:

- unknown faces do not appear in the feed
- recognized participants appear by participant_id
- deduplicate by participant_id, not by name
- each visible participant stays for at least 5 seconds after the latest sighting
- another detection refreshes the 5-second timer
- if the same participant is seen by multiple cameras, show one row and combine camera sources
- after the row expires, the same participant may appear again on a future detection

Do not alter Central attendance semantics to implement UI deduplication.

---

## 17. Detection Session History Rules

A Detection Session begins when the user presses START DETECTION.

Starting a new Detection Session resets:

- Live Recognition Feed
- Global Session History
- Per-Camera Session History

Global Session History:

- one participant once per Detection Session
- deduplicate by participant_id

Per-Camera Session History:

- one participant once per camera per Detection Session
- deduplicate by participant_id

STOP DETECTION prevents new recognition additions.

Existing session history remains viewable after STOP.

---

## 18. Camera Stream Rules

Each physical camera must have its own MediaStream.

Do not repeatedly call getUserMedia during recognition ticks.

Recognition scanning must never reopen or remount the camera stream.

Recording and recognition must reuse the same already-open camera stream.

Stopping detection must not stop the camera preview.

Stopping recording must not stop the camera preview.

Unchecking or intentionally shutting down a camera may release that camera's stream.

---

## 19. Central Server Is Authoritative

Central remains the source of truth for:

- participants
- embeddings/index master
- attendance
- consent
- activities
- recognition history
- event-photo processing
- project data

Do not create a second competing source of truth.

Local camera nodes may perform recognition inference, but they do not own the authoritative project database.

---

## 20. Future Architecture Is Not Current Scope

Long-term Reconize may eventually support:

- multiple Windows recognition nodes
- other PCs using their own cameras
- phone/iPad cameras sending frames to Central
- real-time multi-device CCTV
- real-time People updates
- real-time PDPA updates

These are future goals.

Do NOT implement them just because they appear in project context.

Only implement them when explicitly requested.

---

## 21. No Assumptions About Approval

Statements such as:

- "tests passed"
- "looks good"
- "the fix is safe"
- "Dummy works"
- "recommended to merge"

are NOT approval.

Only explicit user authorization such as:

- "ขึ้น Main"
- "เอาขึ้น Main"
- "ผ่านแล้ว"
- "อนุมัติให้ขึ้น Main"

authorizes the appropriate next action.

If uncertain, STOP and ask.

---

## 22. Final Rule

When unsure whether a change can go directly to Main:

IT CANNOT.

Use Dummy first.

When unsure whether unrelated code may be changed:

DO NOT CHANGE IT.

When the requested task is finished:

STOP AND WAIT.

---

## 23. Project Documentation Must Stay Synchronized

The project instruction files are part of the project state and must stay synchronized with the actual implementation.

The authoritative project files are:

- `AGENTS.md` — agent startup and operating instructions
- `PROJECT_RULES.md` — permanent development and safety rules
- `PROJECT_CONTEXT.md` — current architecture, implemented capabilities, important historical decisions, and long-term direction
- `CURRENT_TASK.md` — the currently active task and its exact workflow state
- `CLAUDE.md` — Claude-specific instructions

Do not allow these files to silently become outdated after project changes.

However, do NOT casually rewrite these files during implementation.

Documentation updates must reflect verified reality, not assumptions or planned behavior.

---

## 24. CURRENT_TASK.md Lifecycle

`CURRENT_TASK.md` must represent the actual current task state.

For an active development task, it should clearly identify:

- requested task
- scope
- protected / out-of-scope areas
- current environment
- current workflow stage
- files or components involved when known
- tests already performed
- manual verification status
- approval status
- Main application status
- Main verification status

Typical workflow states include:

- PLANNING
- DUMMY IMPLEMENTATION
- DUMMY AUTOMATED TESTING
- WAITING FOR USER DUMMY TEST
- WAITING FOR USER APPROVAL
- APPROVED FOR MAIN
- APPLYING TO MAIN
- WAITING FOR MAIN VERIFICATION
- COMPLETED

Do not mark a task COMPLETED merely because code or automated tests passed.

The task becomes COMPLETED only when the completion conditions defined for that task have been satisfied, including user verification when required.

---

## 25. Updating PROJECT_CONTEXT.md

`PROJECT_CONTEXT.md` describes the actual project, not speculative plans.

Update it when an approved change materially changes:

- architecture
- implemented features
- important data semantics
- recognition behavior
- camera/CCTV behavior
- enrollment behavior
- consent behavior
- event-photo processing
- integrations
- environment structure
- important technical decisions
- long-term architecture decisions explicitly approved by the user

Do NOT document an unapproved Dummy experiment as an implemented Main capability.

A feature should not be described as a Main capability until it has actually been applied to Main and reached the appropriate verification stage.

Preserve useful historical design decisions when they explain why the current architecture exists.

Do not turn PROJECT_CONTEXT.md into a raw chronological log.

---

## 26. Updating PROJECT_RULES.md

`PROJECT_RULES.md` contains permanent or reusable rules.

If the user establishes a new permanent development rule, safety rule, approval rule, data-protection rule, or workflow rule:

- identify that it is a new permanent rule
- add it to PROJECT_RULES.md when appropriate
- do not weaken or remove an existing rule unless the user explicitly requests it

Task-specific instructions do not automatically become permanent rules.

When uncertain whether an instruction is permanent, ask the user before changing PROJECT_RULES.md.

---

## 27. AGENTS.md Maintenance

`AGENTS.md` should remain a stable entry point for Codex and other compatible coding agents.

Do not fill AGENTS.md with temporary task details.

Temporary work belongs in CURRENT_TASK.md.

Architecture belongs in PROJECT_CONTEXT.md.

Permanent rules belong in PROJECT_RULES.md.

Only update AGENTS.md when the agent startup procedure or instruction hierarchy itself changes.

---

## 28. Patch Backup Is Mandatory Before Main Modification

Before modifying ANY approved Main file, create a patch backup.

The patch backup must contain the original Main version of every file that is about to be modified.

Create the backup BEFORE editing Main.

Never create the backup after the file has already been changed.

The purpose of the patch is to make the exact pre-change Main state recoverable.

---

## 29. Patch Backup Location and Naming

Unless the existing project already has a compatible patch convention, use:

`patch/YYYY-MM-DD_HHMM_<short-task-name>/`

Example:

`patch/2026-09-06_1430_cctv-feed-fix/`

The patch directory should contain:

- original copies of every Main file that will be modified
- a `NOTES.md` describing the patch

Preserve relative paths inside the patch when practical so the original file location is clear.

Do not include:

- database copies unless the task explicitly requires and safely authorizes a database backup
- participant photos
- event photos
- secrets
- credentials
- Dummy data
- unrelated files

---

## 30. Patch NOTES.md

Each Main patch backup should document at minimum:

- creation date/time
- task name
- reason for change
- Main files backed up
- expected Main files to be modified
- whether a database/schema change is expected
- whether real data is expected to be modified
- Dummy verification status
- user approval status

After the Main change, the notes may additionally record:

- files actually modified
- tests performed
- Main verification status

Patch notes must describe reality and must not claim user verification before it happens.

---

## 31. Verify Patch Before Editing Main

After creating the patch and BEFORE modifying Main:

verify that:

- every intended Main file has a backup
- every backup is readable
- backup content matches the current pre-change Main file
- no intended file is missing

Use a checksum such as SHA-256 when practical.

If patch verification fails:

STOP.

Do not modify Main.

Report the failure.

---

## 32. Unexpected Main Differences

Before applying a Dummy-approved change to Main, compare the relevant Main and Dummy implementations.

If Main contains unrelated changes that Dummy does not contain:

preserve Main.

Do not overwrite those differences.

Merge only the approved change.

If the merge cannot be performed safely without affecting unrelated Main behavior:

STOP and report the conflict.

---

## 33. Unexpected Schema or Data Changes

If an approved task was expected to require no database/schema/data change, but Main application reveals that one is required:

STOP BEFORE performing that change.

Report:

- why it is required
- affected tables/data
- migration implications
- rollback implications

Wait for explicit user authorization.

Do not silently introduce a migration or modify real data.

---

## 34. Post-Main Documentation Update

After an approved change has been applied to Main:

do not immediately declare the task complete.

Update the task state to:

`WAITING FOR MAIN VERIFICATION`

when user verification is required.

Only after the user verifies Main successfully should the project documentation be finalized to reflect the verified state.

At that point:

- update CURRENT_TASK.md appropriately
- update PROJECT_CONTEXT.md if architecture/capabilities materially changed
- update PROJECT_RULES.md only if a new permanent rule was established
- update patch NOTES.md with final verification status when appropriate

Do not change documentation merely to make the project appear complete.

---

## 35. Failed Main Verification

If Main verification fails:

- keep the task ACTIVE
- record the failure in CURRENT_TASK.md when appropriate
- diagnose only the current failure
- preserve the patch backup
- do not start another feature
- do not claim the approved feature is complete

Any corrective code change must follow the safest applicable workflow.

Do not use the existence of previous approval as unlimited permission for unrelated fixes.

---

## 36. Project State Handoff

The purpose of the project documentation is to allow another coding agent or a future session to understand the project without relying on previous chat memory.

At any handoff point, these files together should answer:

`PROJECT_RULES.md`
→ What rules must I obey?

`PROJECT_CONTEXT.md`
→ What system am I working on and why is it designed this way?

`CURRENT_TASK.md`
→ What exactly is happening right now?

`AGENTS.md`
→ What must I read and how must I behave before acting?

The actual source code remains the final technical truth for implementation details.

If documentation and code disagree:

DO NOT silently choose one.

Inspect the discrepancy, report it, and avoid changing behavior until the conflict is understood.

---

## 37. Documentation Changes Are Still Changes

Do not silently modify project instruction/documentation files.

Whenever you modify:

- AGENTS.md
- PROJECT_RULES.md
- PROJECT_CONTEXT.md
- CURRENT_TASK.md
- CLAUDE.md
- patch NOTES.md

include those modifications in your final change report.

Documentation maintenance does NOT grant permission to modify unrelated code.

---

## 38. Continuity Rule

Every completed project change should leave Reconize understandable to the next agent.

Do not rely on statements such as:

- "we discussed this earlier"
- "Claude already knows"
- "Codex remembers"
- "it was in the previous chat"

Important project knowledge must live in the appropriate project documentation or in the source code itself.

Chat memory is supplementary, not authoritative.