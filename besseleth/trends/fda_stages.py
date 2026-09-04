"""Canonical FDA medical-device regulatory stages, in progress order —
lets the Trends tab plot "how far along is each company" on one shared
axis, even though `devices.fda_status` is free text drafted by an LLM
(so "IDE approved", "Investigational Device Exemption granted", and
"IDE" all need to land on the same rung).

The ordering below reflects the actual FDA pathway for a medical device
(most relevant to neurotech, where most implants are Class III):

  1. Breakthrough Device Designation — an FDA program (not itself a
     marketing authorization) giving more interactive, priority review
     to devices treating life-threatening/irreversibly debilitating
     conditions with no adequate alternative. Usually requested early,
     well before human trials.
  2. IDE (Investigational Device Exemption) — required before a
     significant-risk device (most active implants) can be used in a
     human clinical study at all.
  3. Marketing authorization — the device can actually be sold. Four
     mutually-exclusive pathways exist, roughly by rigor:
       - 510(k) clearance: substantial equivalence to an existing
         (predicate) device — the common, faster route for lower-risk
         devices.
       - De Novo classification: a novel device with no predicate, but
         low/moderate risk — creates a new predicate for later 510(k)s.
       - PMA (Premarket Approval): the most rigorous pathway, required
         for most Class III devices (most neural implants) — demands
         real clinical evidence of safety and effectiveness.
       - HDE (Humanitarian Device Exemption): PMA-level review rigor,
         but only "probable benefit" (not proven effectiveness) is
         required, for conditions affecting <8,000 patients/year in the
         US — relevant for some rare-indication neurotech.
     All four land on the same rung here (comparing 510(k) vs. PMA
     rigor isn't a "how far along" comparison — see stage_pathway()).
  4. Commercially available — actually on the market, which sometimes
     lags a while after authorization (manufacturing scale-up, payer
     coverage) and is worth its own point on the timeline.

A status this can't confidently place lands at rank 0 ("Unknown /
pre-designation") rather than guessing.
"""
from __future__ import annotations

import re

FDA_STAGES: list[tuple[int, str]] = [
    (0, "Unknown / pre-designation"),
    (1, "Breakthrough Device Designation"),
    (2, "IDE approved (clinical trial)"),
    (3, "Marketing authorization"),
    (4, "Commercially available"),
]

_PATTERNS: list[tuple[int, str, re.Pattern]] = [
    (4, "Commercially available", re.compile(r"\b(commercially available|on the market|now (available|shipping)|launched)\b", re.I)),
    (3, "PMA approved", re.compile(r"\bpma\b|premarket approval", re.I)),
    (3, "HDE granted", re.compile(r"\bhde\b|humanitarian device exemption", re.I)),
    (3, "De Novo granted", re.compile(r"de novo", re.I)),
    (3, "510(k) cleared", re.compile(r"510\s*\(?k\)?", re.I)),
    (3, "Marketing authorization", re.compile(r"\b(cleared|approved for market|fda approv)\b", re.I)),
    (2, "IDE approved (clinical trial)", re.compile(r"\bide\b|investigational device exemption", re.I)),
    (1, "Breakthrough Device Designation", re.compile(r"breakthrough device", re.I)),
]


def stage_for(fda_status: str | None) -> tuple[int, str]:
    """Maps a free-text fda_status to (rank, canonical_label). Unmatched
    or empty text is rank 0 — not "wrong", just not yet placeable."""
    text = (fda_status or "").strip()
    if not text or text.lower() in ("unknown", "n/a", "none"):
        return (0, "Unknown / pre-designation")
    for rank, label, pattern in _PATTERNS:
        if pattern.search(text):
            return (rank, label)
    return (0, "Unknown / pre-designation")
