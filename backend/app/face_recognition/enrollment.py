"""Choosing which detected face owns a participant's enrollment photo.

This is ENROLLMENT ONLY. It runs *after* normal detection and never changes
it: `detect_faces()` still returns every face it finds, because check-in,
Event Photos and photo sorting all depend on that. The only thing decided
here is which single face is allowed to become a participant's stored
embedding.

Why it is needed: an enrollment photo often catches a bystander in the
background. Rejecting every such photo is too strict, and silently taking
`faces[0]` is worse — the detector's order is not "most prominent first", so
that quietly enrolls the wrong person under someone else's name.

The rule uses relative prominence, not absolute size, so it works the same for
a close-up selfie and a photo taken from across a room:

    face_area          = bbox_width * bbox_height
    largest_face_ratio = largest_face_area / sum(all face areas)

    >= 0.60  ->  the largest face owns the photo; every other face is ignored
    <  0.60  ->  reject; no face is prominent enough to be unambiguous

Note this is deliberately NOT "the face covers 60% of the image".
"""
from __future__ import annotations

from typing import Sequence

# The share of total detected face area the main subject must hold. At 0.60 a
# second face the same size (ratio 0.50) is always rejected, while a bystander
# at two-thirds the width of the subject (area ratio ~0.69) still passes.
PRIMARY_FACE_AREA_RATIO = 0.60

NO_FACE_MESSAGE = "No face detected. Please upload a clear face image."
MULTIPLE_PROMINENT_MESSAGE = (
    "Multiple prominent faces detected. Please use a photo where the participant "
    "is clearly the main person."
)


class EnrollmentFaceError(ValueError):
    """No single face could be identified as the owner of an enrollment photo.

    Carries the user-facing text, so every enrollment path (Add Person, photo
    replacement, spreadsheet import, Google Sheet sync, Drive import) reports
    the same wording for the same situation.
    """

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


def face_area(face) -> float:
    """Area of the detector's bounding box, in pixels.

    bbox is (x1, y1, x2, y2). abs() because a degenerate or inverted box must
    not produce a negative area that could distort the ratio.
    """
    x1, y1, x2, y2 = (float(v) for v in face.bbox)
    return abs(x2 - x1) * abs(y2 - y1)


def largest_face_ratio(faces: Sequence) -> float:
    """The largest face's share of all detected face area, 0.0-1.0."""
    areas = [face_area(f) for f in faces]
    total = sum(areas)
    if total <= 0:
        return 0.0
    return max(areas) / total


def select_primary_enrollment_face(faces: Sequence):
    """Return the one face that owns this enrollment photo.

    Raises EnrollmentFaceError when the photo has no face, or no face that is
    clearly the main subject. Never returns more than one face, and never
    falls back to faces[0] — the winner is chosen by area.
    """
    if not faces:
        raise EnrollmentFaceError(NO_FACE_MESSAGE)

    if len(faces) == 1:
        return faces[0]

    if largest_face_ratio(faces) >= PRIMARY_FACE_AREA_RATIO:
        # max() with a key, not sorting or indexing — the stored embedding must
        # come from the largest face regardless of detector ordering.
        return max(faces, key=face_area)

    raise EnrollmentFaceError(MULTIPLE_PROMINENT_MESSAGE)
