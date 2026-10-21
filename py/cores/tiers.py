"""Three-axis tier vocabulary + the server-authoritative tier matrix.

This is the ONE home for the tier vocabulary and the language x audio -> aggregate
mapping (design sketch APPROVED 2026-06-22). Movability guardrail 1: `tier_for` is
server-authoritative and standalone — never inlined into a handler, never client-only.
The client renders the grid; the server owns the mapping. The Slice C tier-decision
endpoint is the one caller that computes + persists the aggregate; the interim admin
emitter (app/main.py) deliberately does NOT call this (it sets the three columns
independently, trusted/interim).

Ported verbatim (precedence preserved) from the frozen demo `tierFor(ling, audio)`
(Screening Pipeline/demo_static/index.html). The order is load-bearing: the ling==2
test fires before the audio tests, so ling 2 with any passing audio lands S-Special.
"""

from typing import NamedTuple

# Per-axis score labels (1..4). These are the score vocabularies; co-located here so
# there is one home for the tier vocabulary.
LANGUAGE_TIERS = {1: "Not qualified", 2: "Basic", 3: "Fluent", 4: "Native"}
AUDIO_TIERS = {1: "Reject", 2: "Lo-fi", 3: "Good", 4: "Hi-fi"}

# Map the demo's short tier codes to the persisted aggregate value-set. These MUST
# stay in step with models.TIERS (the CHECK on users.aggregate_tier).
_AGGREGATE = {"A": "A-Studio", "B": "B-Prod", "C": "C-LoFi", "S": "S-Special"}

# The fidelity ORDER of the three ladder tiers (higher rank = higher fidelity):
# A-Studio > B-Prod > C-LoFi. S-Special is deliberately OFF the ladder (a niche
# Basic-language cell) — it is never ordered against A/B/C; it satisfies only an
# exact match. This is THE canonical rank; `assignability.satisfies` reads it for
# the per-side spec comparators, and `realtime._TIER_RANK` is an equivalent local
# copy for the min_tier floor (a later DRY-up may fold that one into this).
AGGREGATE_RANK = {"C-LoFi": 1, "B-Prod": 2, "A-Studio": 3}


class TierResult(NamedTuple):
    """The outcome of the matrix.

    `is_tier` is True for the four aggregate tiers (A/B/C/S); then `aggregate` is the
    persisted value-set string to write to users.aggregate_tier. `gate` and `rerecord`
    are non-tier SENTINELS: is_tier is False and aggregate is None — they must never be
    written to aggregate_tier.
    """

    code: str            # "A" | "B" | "C" | "S" | "gate" | "rerecord"
    aggregate: str | None  # persisted TIERS value, or None for the sentinels
    is_tier: bool


def tier_for(language: int, audio: int) -> TierResult:
    """Compute the aggregate tier from the two axis scores (each 1..4).

    Precedence is order-dependent exactly as in the demo; do not reorder.
    """
    if language not in LANGUAGE_TIERS or audio not in AUDIO_TIERS:
        raise ValueError("language and audio scores must each be in 1..4")

    if language == 1:
        return TierResult("gate", None, False)       # never reaches audio review
    if audio == 1:
        return TierResult("rerecord", None, False)   # never reaches tier assignment
    if language == 2:
        return TierResult("S", _AGGREGATE["S"], True)
    if audio == 2:
        return TierResult("C", _AGGREGATE["C"], True)
    if audio == 3:
        return TierResult("B", _AGGREGATE["B"], True)
    if language == 4 and audio == 4:
        return TierResult("A", _AGGREGATE["A"], True)
    return TierResult("B", _AGGREGATE["B"], True)     # else-fallthrough
