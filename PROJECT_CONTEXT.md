# Reconize Project Context

## 1. Project Overview

Reconize is a local-first event management and face-recognition system.

The system is designed for real event usage and currently includes:

- participant registration
- participant import from CSV / Excel / Google Sheet
- face enrollment
- face-recognition check-in
- attendance tracking
- PDPA / consent tracking
- multi-camera CCTV recognition
- activity-based recognition
- post-event photo processing
- participant photo sorting
- privacy-aware MEDIA output
- Google Drive integration

The project must remain production-usable and must preserve existing working behavior.

---

## 2. Main and Dummy Environments

MAIN:

E:\งาน\491\Face_Reconize\Reconize

DUMMY:

E:\งาน\491\Face_Reconize\Reconize_Dummy

Main is the real working project and contains real project data.

Dummy is used for implementation, automated tests, and user manual testing before any approved change is moved to Main.

Never treat Main as a test environment.

---

## 3. Current Face Recognition Architecture

Reconize currently uses InsightFace.

Recognition configuration:

- model: buffalo_l
- allowed modules: detection + recognition
- embedding dimension: 512
- float32 embeddings
- L2-normalized embeddings
- recognition threshold: 0.45
- detection threshold: 0.5
- detection size: 320x320

Participant embeddings are stored and used to build an in-memory recognition index.

Matching is vectorized using NumPy rather than querying the database for every scan.

The current recognition algorithm, model, embeddings, index, and thresholds must not be changed unless explicitly requested.

---

## 4. Recognition Flow

Typical recognition flow:

1. Browser captures a frame from an already-open camera MediaStream.
2. Frame is converted to JPEG.
3. Image is sent to the recognition backend or Local Recognition Agent.
4. InsightFace detects faces.
5. Recognition embeddings are calculated.
6. Embeddings are compared against the in-memory participant index.
7. Recognized participants are returned.
8. Recognition / attendance data is sent to Central.
9. UI updates using the same recognition result.

Multi-face recognition is supported.

Unknown faces may still count toward face detection totals but do not appear as recognized participants.

---

## 5. Enrollment Behavior

Enrollment accepts standard images and HEIC / HEIF images.

Enrollment photos use a primary-face rule.

Rules:

- 0 detected faces:
  reject

- 1 detected face:
  use that face

- 2 or more detected faces:
  calculate:

  largest detected face bounding-box area
  divided by
  sum of all detected face bounding-box areas

  If ratio >= 0.60:
  use only the largest face

  If ratio < 0.60:
  reject as multiple prominent faces

This rule applies only to participant enrollment/profile photos.

It must not be applied to normal kiosk recognition, CCTV recognition, or event-photo processing.

---

## 6. Participant Import

Reconize supports participant import from:

- CSV
- Excel
- Google Sheet

Input column names may vary.

Only the participant name and photo reference are fundamentally required.

Other columns may be ignored unless used for supported functions such as consent.

The system can assign internal sequential participant IDs when an external ID is not provided.

Duplicate participant handling is based on normalized participant name according to the existing implementation.

Existing participants must not be duplicated when syncing/importing the same person.

---

## 7. Google Sheet Sync

Google Sheet registration sync currently supports participant and consent updates.

Important behavior:

- existing participant name should not create a duplicate Person
- explicit registration consent may create a ConsentRecord
- blank / unknown consent does not clear an existing value
- unchanged registration consent must be idempotent
- registration sync compares against the latest registration-source record
- unchanged registration data must not overwrite a newer kiosk decision
- if a genuinely changed registration response arrives later, it may become the latest record
- if the latest overall consent source is admin, registration sync must not overwrite it

Do not change this behavior unless explicitly requested.

---

## 8. PDPA / Consent Semantics

Consent does not block face recognition or attendance.

A participant may still be recognized and checked in even when their consent status is NOT CONSENTED or PENDING.

Consent history is append-only.

Possible sources include:

- kiosk
- registration
- admin

The current status is determined according to the existing latest-record logic.

Event-photo processing may use the consent state captured at processing time.

Do not reinterpret PDPA behavior without explicit user instruction.

---

## 9. Kiosk Recognition

The guest-facing kiosk has a session-based scanning flow.

General behavior:

IDLE
→ user starts scan
→ consent choice
→ camera opens
→ scan until registered participant is recognized
→ process recognized participant(s)
→ camera stops
→ result shown
→ return to IDLE

The camera should not remain open continuously for the normal tap-to-scan kiosk flow.

Multi-person recognition is supported.

Existing attendance data determines whether a participant is new or already checked in.

Do not replace this behavior with frontend-only attendance tracking.

---

## 10. CCTV Live View

CCTV Live View is now the main user-facing multi-camera workflow.

The current implementation is designed for multiple physical cameras connected to one Windows PC.

It currently supports approximately 7 cameras.

Important CCTV behavior includes:

- multiple physical cameras on one Windows PC
- checkbox-based camera selection
- persistent selection in localStorage
- stable physical deviceId to cameraId mapping
- restored cameras after navigation / refresh
- one independent MediaStream per physical camera
- live video preview
- no recognition-driven video flicker
- face count per camera
- one Local Recognition Agent
- activity-controlled recognition
- Start Detection
- Stop Detection
- camera previews remain live when detection is stopped
- recognition stops when detection is stopped
- integrated video recording
- multiple cameras may record simultaneously
- recording reuses the existing camera stream
- stopping recording must not stop live preview

Do not reintroduce a separate redundant Camera workflow unless explicitly requested.

---

## 11. CCTV Face Count

Face Count means:

ALL faces detected by the detector.

This includes:

- recognized participants
- unknown/unrecognized faces

A face does not need to match a registered participant to count toward the Face Count.

---

## 12. CCTV Live Recognition Feed

CCTV includes a right-side Live Recognition Feed.

The feed reuses the exact same recognition result already generated by the camera scan.

It must not trigger another recognition inference.

Rules:

- only recognized participants appear
- unknown faces do not appear
- participant identity is based on participant_id
- duplicate rows must not be created for the same participant
- a participant remains visible for at least 5 seconds after their latest detection
- detecting them again refreshes the timer
- the same participant detected by multiple cameras appears as one feed row
- camera sources may be combined
- after the feed row expires, the same participant may appear again later

This feed is a UI behavior and must not alter Central attendance semantics.

---

## 13. Detection Session

A Detection Session begins when the user presses START DETECTION.

At the beginning of a new Detection Session, the system resets:

- Live Recognition Feed
- Global Session History
- Per-Camera Session History

While Detection is STOPPED:

- live camera preview remains available
- recording may continue
- no recognition frames should be processed
- no new recognition feed entries should be created
- no new session-history entries should be created

---

## 14. Global Session History

Global Session History stores recognized participants for the current Detection Session.

Rules:

- one participant appears once per Detection Session
- uniqueness is based on participant_id
- repeated recognition does not create duplicate global entries

History remains viewable after STOP DETECTION.

A new START DETECTION creates a fresh session history.

---

## 15. Per-Camera Session History

Each camera also has its own Session History.

Rules:

- one participant appears once for that camera during the current Detection Session
- uniqueness is based on participant_id
- the same person may exist once in Camera A history and once in Camera B history
- repeated detections on the same camera must not create duplicates

Camera detail view may display that camera's unique recognized participants.

---

## 16. Local Recognition Agent

For Windows multi-camera CCTV, recognition may be performed through one Local Recognition Agent.

The browser owns the physical camera MediaStreams.

The Local Agent performs InsightFace inference.

The Local Agent does not own the authoritative project database.

Central remains authoritative.

Each physical camera has a stable source identity.

Recognition events sent to Central must preserve the camera/node source.

---

## 17. GPU / CUDA

The working Main system has already been configured to use CUDA/GPU for InsightFace where available.

The existing CUDA initialization must be preserved.

Do not remove or rewrite GPU bootstrap behavior unless explicitly requested.

CPU fallback behavior should not be casually changed.

---

## 18. Event Photo Processing

Reconize has a post-event photo pipeline.

Conceptually it includes:

- source photo retrieval
- image decode
- face detection
- participant matching
- consent lookup/snapshot
- classification
- local output
- privacy blur for MEDIA
- Google Drive output sync

The processing work was split so local processing and Drive output synchronization are separate phases.

Do not collapse or rewrite this architecture unless explicitly requested.

---

## 19. Event Photo Outputs

Existing concepts include:

SORTED:
participant-specific photo output.

MEDIA:
publishable/media version where applicable privacy/consent rules are applied.

Atmosphere:
images without relevant detected faces according to current implementation.

A non-consented participant may still have participant-specific processing behavior that differs from the MEDIA privacy output.

Do not change existing semantics without explicit instruction.

---

## 20. Google Drive

Reconize integrates with Google Drive for event-photo workflows.

Drive links may be shown in the application.

Existing Drive behavior must be preserved when modifying unrelated features.

Do not use real Drive output as destructive test data unless explicitly authorized.

---

## 21. Central Server

Central is the authoritative source of truth for project data including:

- participants
- participant embeddings/index master
- attendance
- consent
- activities
- recognition history
- event-photo processing data
- project settings

Local nodes may perform inference but must not become a competing authoritative database.

---

## 22. Current Multi-Camera Design Philosophy

For multiple cameras on one Windows PC:

Browser:
owns each physical camera MediaStream.

Local Recognition Agent:
performs face inference.

Central:
receives authoritative recognition/attendance events and stores shared project data.

Do not open the same physical camera twice for recognition and recording.

Recording should reuse the existing camera stream or a clone of that stream.

Recognition scanning must not remount or reopen the camera.

---

## 23. Long-Term Goal

The long-term direction for Reconize includes a distributed camera system.

Future goals may include:

1. multiple cameras detecting simultaneously
2. multiple Windows PCs acting as recognition nodes
3. other computers opening Reconize and using their local cameras
4. phone / iPad using their camera while Central performs the AI inference
5. real-time CCTV view across multiple devices
6. recognition-source identification by node/camera
7. real-time People status updates
8. real-time PDPA information

The desired architecture is closer to:

Distributed Camera / Recognition Nodes
+
Central Server / Shared Authoritative Data

It is not intended to become uncontrolled peer-to-peer database synchronization.

---

## 24. Mobile Direction

For future phone / iPad support:

The mobile browser should own its camera.

The mobile device should not be required to run the full InsightFace Python stack locally.

Frames may be sent to Central for inference.

This is a future goal and must not be implemented unless explicitly requested.

---

## 25. Remote CCTV Direction

For future remote/multi-device CCTV viewing, merely placing a remote video element on the page is not sufficient.

Actual network video transport such as WebRTC may be required.

Camera permission remains per browser/device.

A CCTV viewer that is only watching remote streams should not require access to its own local camera.

This is future architecture, not automatic current scope.

---

## 26. Current Development Status

The project already contains many implemented and user-tested features.

Do not assume that existing code is unfinished simply because it is complex.

When modifying existing behavior:

1. inspect current Main implementation
2. inspect Dummy implementation
3. understand why the current code exists
4. preserve unrelated working behavior
5. implement only the requested task

Do not rewrite architecture from scratch unless the user explicitly requests that.

---

## 27. Important Existing Features To Preserve

Unless explicitly requested, preserve:

- participant registration
- CSV / Excel import
- Google Sheet sync
- Google Drive photo support
- HEIC / HEIF enrollment
- primary enrollment face selection
- recognition index
- CUDA/GPU support
- multi-face recognition
- attendance behavior
- kiosk consent flow
- PDPA page and consent history
- admin consent override behavior
- event-photo phase separation
- SORTED / MEDIA behavior
- Activities
- CCTV multi-camera selection
- CCTV persistent camera restore
- CCTV no-flicker preview
- Activity-controlled detection
- CCTV recording
- Live Recognition Feed
- Global Session History
- Per-Camera Session History

---

## 28. Working Style

The user prefers development one task at a time.

Do not overwhelm the user with several future implementation paths while the current task is unfinished.

When a task reaches the user-testing stage:

- explain what was changed
- provide exact URL
- provide simple manual test instructions
- wait for the user's result

Do not automatically continue to another feature.

---

## 29. Historical Note About Claude

Much of Reconize has previously been developed and tested using Claude.

Claude-specific chat history should not be assumed to be available to Codex.

The files in this repository are the authoritative handoff mechanism.

Codex should inspect the existing code rather than assuming older descriptions perfectly reflect the current implementation.

If documentation conflicts with actual code, report the conflict before changing behavior.

---

## 30. Core Principle

Reconize is already a working evolving system.

The objective is not to rebuild it.

The objective is to improve it incrementally while preserving:

- real data
- working behavior
- recognition accuracy
- data semantics
- privacy behavior
- Main stability

Always follow PROJECT_RULES.md before making changes.