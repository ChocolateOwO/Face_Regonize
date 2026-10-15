"""Face tracks for the tagged-video render: which detection in this frame is
the same face as in the previous ones, and which name a track has earned.

Display logic only. It never feeds recognition counts, previews or the CSV;
those come from the experiment's own sampled pass. Pure Python, no model.

A name is shown only once it is confirmed, so one borderline match on a
passer-by does not flash a wrong name: the most-voted identity in the track's
recent window must have CONFIRM_HITS matched observations, or one observation
at CONFIRM_SCORE or better. The constants come from the robot team's
prototype and are covered by tests of the behaviour, not assumed correct for
every clip.
"""
from __future__ import annotations

from collections import Counter, deque

TRACK_MAX_GAP = 8      # frames a track survives without a detection
TRACK_DRAW_GAP = 2     # frames a box stays drawn after its last detection
VOTE_WINDOW = 30       # recent observations used for a track's identity
CONFIRM_HITS = 2       # matched observations before a name is trusted...
CONFIRM_SCORE = 0.55   # ...unless one observation is this strong
MIN_IOU = 0.2

Box = tuple[float, float, float, float]


def iou(a: Box, b: Box) -> float:
    ix = max(0.0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0.0, min(a[3], b[3]) - max(a[1], b[1]))
    inter = ix * iy
    union = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / union if union > 0 else 0.0


def center_close(a: Box, b: Box) -> bool:
    """A fast-moving face can leave no overlap between frames; centres within
    0.6 of the larger box still count as the same face."""
    size = max(a[2] - a[0], b[2] - b[0])
    return (abs((a[0] + a[2]) - (b[0] + b[2])) / 2 < 0.6 * size
            and abs((a[1] + a[3]) - (b[1] + b[3])) / 2 < 0.6 * size)


class Track:
    def __init__(self, bbox: Box, frame_no: int):
        self.bbox = bbox
        self.last_frame = frame_no
        self.obs: deque = deque(maxlen=VOTE_WINDOW)  # (person_id or None, score)

    def identity(self) -> str | None:
        """The confirmed person for this track, or None (drawn as Unknown)."""
        votes: Counter = Counter()
        hits: Counter = Counter()
        best: dict[str, float] = {}
        for pid, score in self.obs:
            if pid:
                votes[pid] += score
                hits[pid] += 1
                best[pid] = max(best.get(pid, 0.0), score)
        if not votes:
            return None
        # Highest summed score; ties resolved by id so the result is stable.
        pid = max(sorted(votes), key=lambda p: votes[p])
        return pid if hits[pid] >= CONFIRM_HITS or best[pid] >= CONFIRM_SCORE else None


def update_tracks(tracks: list[Track], detections: list[tuple[Box, str | None, float]], frame_no: int) -> None:
    """Greedy best-overlap association, in place. `detections` are
    (bbox, person_id or None, match score) for this frame."""
    pairs = sorted(((iou(t.bbox, d[0]), ti, di) for ti, t in enumerate(tracks) for di, d in enumerate(detections)),
                   reverse=True)
    used_t: set[int] = set()
    used_d: set[int] = set()
    assigned: list[tuple[Track, tuple]] = []
    for score, ti, di in pairs:
        if ti in used_t or di in used_d:
            continue
        if score < MIN_IOU and not center_close(tracks[ti].bbox, detections[di][0]):
            continue
        used_t.add(ti)
        used_d.add(di)
        assigned.append((tracks[ti], detections[di]))
    for di, d in enumerate(detections):
        if di not in used_d:
            track = Track(d[0], frame_no)
            tracks.append(track)
            assigned.append((track, d))
    for track, (bbox, pid, score) in assigned:
        track.bbox = bbox
        track.last_frame = frame_no
        track.obs.append((pid, score))
    tracks[:] = [t for t in tracks if frame_no - t.last_frame <= TRACK_MAX_GAP]


def drawable(tracks: list[Track], frame_no: int) -> list[Track]:
    return [t for t in tracks if frame_no - t.last_frame <= TRACK_DRAW_GAP]
