"""
id_stabilizer.py -- temporal identity stabiliser.

Binds a recognised identity to a SPATIAL track and holds it across momentary
recognition failures, so a continuously-present enrolled person never flickers
to UNKNOWN because of one bad frame (motion blur, glasses glare, a hard head
turn, a dropped identification pass).

Why this exists
---------------
Face recognition runs per frame and is noisy: the same enrolled face scores
above threshold on most frames but occasionally dips or is missed entirely.
Deciding "known vs unknown" independently every frame turns that noise into a
stream of false "unregistered person entered / departed" events. Here each
observed face is matched to a persistent track, and the track's identity is
STICKY:

  * A track that has recently matched an enrolled id keeps that identity for
    ID_HOLD_SECONDS after the last confirming match -- momentary misses are
    invisible.
  * A track that has NEVER been identified is only reported as UNKNOWN after it
    has persisted unmatched for NEW_UNKNOWN_DWELL_S -- a face flashing past in
    the background does not immediately raise a critical alert.
  * Severity scales with how long an unknown track has genuinely persisted.

Pure python (standard library only) so the logic is unit-testable without a
camera or any of the vision models. See test at the bottom / tests/ script.
"""

import time

# ---------------------------------------------------------------------------
# Tunables -- edit these, not the logic. All times in seconds.
# ---------------------------------------------------------------------------
ID_HOLD_SECONDS      = 1.5   # keep a committed identity this long after the
                             # last confirming match (hysteresis against dips)
NEW_UNKNOWN_DWELL_S  = 2.5   # a never-identified track must persist unmatched
                             # this long before it counts as UNKNOWN at all
TRACK_STALE_S        = 2.0   # drop a track not observed for this long (person
                             # actually left frame)
GATE_FRAC            = 1.6   # association gate radius = GATE_FRAC * face size

# Severity tiers, by how long a track has been sustained-unknown (seconds).
# Below NEW_UNKNOWN_DWELL_S the track is "pending" and raises nothing.
SEV_WARNING_START_S  = 5.0   # quiet caution below this; visible warning at/above
SEV_CRITICAL_START_S = 15.0  # critical at/above this


def severity_for(unknown_dur):
    """Map a sustained-unknown duration to a severity tier."""
    if unknown_dur < NEW_UNKNOWN_DWELL_S:
        return "none"        # pending -- suppressed, treated as noise
    if unknown_dur < SEV_WARNING_START_S:
        return "caution"     # informational, no urgent banner
    if unknown_dur < SEV_CRITICAL_START_S:
        return "warning"     # visible banner, elevated risk
    return "critical"        # sustained unidentified presence


class IdentityStabilizer:
    """Maintains sticky per-track identity from noisy per-frame recognition."""

    def __init__(self):
        self._tracks = {}     # tid -> state dict
        self._seq = 0

    # -- association --------------------------------------------------------
    def _match(self, cx, cy, size, claimed):
        gate = (GATE_FRAC * max(size, 1)) ** 2
        best, best_d = None, None
        for tid, t in self._tracks.items():
            if tid in claimed:
                continue
            d = (cx - t["cx"]) ** 2 + (cy - t["cy"]) ** 2
            if d <= gate and (best_d is None or d < best_d):
                best_d, best = d, tid
        return best

    # -- main step ----------------------------------------------------------
    def update(self, faces, now=None):
        """Advance one frame.

        faces: list of dicts, each {cx, cy, size, sid, name}. `sid` is this
        frame's RAW recognition result for that face (None = no confident match
        this frame). Order is preserved in the return value.

        Returns a list aligned with `faces`, each:
            {tid, sid, name, state, unknown_dur}
        where `sid`/`name` are the COMMITTED (stabilised) identity (sid None
        while unknown/pending) and `state` is one of:
            "known"   -- committed to an enrolled identity
            "pending" -- unidentified but too briefly present to flag
            "unknown" -- sustained unidentified presence
        """
        if now is None:
            now = time.time()

        # Associate largest faces first, mutually exclusive, nearest-centre.
        order = sorted(range(len(faces)), key=lambda i: -faces[i]["size"])
        claimed, assign = set(), {}
        for i in order:
            f = faces[i]
            tid = self._match(f["cx"], f["cy"], f["size"], claimed)
            if tid is None:
                self._seq += 1
                tid = f"idt{self._seq}"
                self._tracks[tid] = {
                    "cx": f["cx"], "cy": f["cy"], "created": now,
                    "last_seen": now, "last_known_sid": None,
                    "last_known_name": None, "last_match_ts": 0.0,
                    "unknown_since": None,
                }
            claimed.add(tid)
            assign[i] = tid

        out = [None] * len(faces)
        for i, f in enumerate(faces):
            t = self._tracks[assign[i]]
            t["cx"], t["cy"], t["last_seen"] = f["cx"], f["cy"], now

            raw = f.get("sid")
            if raw:
                t["last_known_sid"] = raw
                t["last_known_name"] = f.get("name")
                t["last_match_ts"] = now

            if t["last_known_sid"] and (now - t["last_match_ts"]) <= ID_HOLD_SECONDS:
                # Sticky: hold the identity through a momentary miss.
                committed, cname, state = t["last_known_sid"], t["last_known_name"], "known"
                t["unknown_since"] = None
            else:
                committed, cname = None, None
                if t["unknown_since"] is None:
                    t["unknown_since"] = now
                dur = now - t["unknown_since"]
                state = "pending" if dur < NEW_UNKNOWN_DWELL_S else "unknown"

            udur = 0.0 if t["unknown_since"] is None else (now - t["unknown_since"])
            out[i] = {"tid": assign[i], "sid": committed, "name": cname,
                      "state": state, "unknown_dur": udur}

        # Prune tracks whose person genuinely left frame.
        for tid in list(self._tracks):
            if now - self._tracks[tid]["last_seen"] > TRACK_STALE_S:
                del self._tracks[tid]

        return out
