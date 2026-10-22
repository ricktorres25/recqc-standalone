"""
compare.py - field-by-field comparison of RecQC records, with the M1 parity tolerances.

Rulings (Rick): exact on every verdict, disposition, band, reason, hash, count and
string, and dB within 0.01 (the 2026-10-02 parity-probe ruling); RT60 seconds within
0.001, r-squared within 0.0001 and any other number within 1e-6 relative (gate 1,
2026-10-21). Timestamps and timings are not compared, and paths are compared by file
name only, since each side runs in its own folder.
"""
import math
import os

TOL_DB = 0.01
TOL_SECONDS = 0.001
TOL_R_SQUARED = 0.0001
REL_OTHER = 1e-6
ZERO = 1e-12   # numbers this close to zero count as equal; a relative tolerance means nothing at 0

IGNORED_KEYS = {"timestamp", "scanned_at", "timings"}
PATH_KEYS = {"source_wav", "output_wav", "file", "noise_profile_used"}
DB_SUFFIXES = ("_db", "_dbfs", "_dbtp", "_dba", "_lufs", "_broadband")
SECONDS_KEYS = {"duration", "duration_sec", "timestamp_sec", "rt60_gate_metric"}
SECONDS_PARENTS = {"rt60_per_band", "rt60_median_per_band"}
LIMITS = {"dB": TOL_DB, "s": TOL_SECONDS, "r2": TOL_R_SQUARED}
# The summary's metric rows and margins (M4) carry their unit beside the number: a
# dict's "value" and "by" take the tolerance of the unit the same dict names.
UNIT_KINDS = {"dB": "dB", "dBTP": "dB", "dBFS": "dB", "LUFS": "dB", "LU": "dB", "s": "s"}
UNIT_VALUE_KEYS = {"value", "by"}

MISSING = "<missing>"


def kind_of(path):
    """The tolerance class of a numeric field, from its key and its parents' keys. An item
    of a list of numbers takes its list's key (a [lowest, highest] r² pair)."""
    key = str(path[-2] if isinstance(path[-1], int) and len(path) > 1 else path[-1])
    if key == "r_squared":
        return "r2"
    if key.endswith(DB_SUFFIXES):
        return "dB"
    if key in SECONDS_KEYS or any(str(p) in SECONDS_PARENTS for p in path):
        return "s"
    return "rel"


def numbers_match(a, b, kind):
    if math.isnan(a) or math.isnan(b):
        return math.isnan(a) and math.isnan(b)
    if math.isinf(a) or math.isinf(b):
        return a == b
    if kind == "rel":
        return math.isclose(a, b, rel_tol=REL_OTHER, abs_tol=ZERO)
    return abs(a - b) <= LIMITS[kind] + 1e-12   # slack for float representation at the limit


def is_number(x):
    return isinstance(x, (int, float)) and not isinstance(x, bool)


class Comparison:
    """Walks pairs of records (A against B) and collects differences and largest deltas.

    `expected` holds functions (path, a, b) -> reason or None; a difference one of them
    explains is listed as expected rather than counted. `skip` is how many leading path
    parts only identify the record (a folder name), so field labels aggregate across them.
    """

    def __init__(self, expected=(), skip=1):
        self.expected = expected
        self.skip = skip
        self.differences = []   # (path, a, b, why)
        self.explained = []     # (path, reason)
        self.largest = {}       # field label -> (kind, largest |a - b|)
        self.values = 0         # leaf values compared

    def label(self, path):
        return ".".join("[]" if isinstance(p, int) else str(p) for p in path[self.skip:])

    def differ(self, path, a, b, why):
        for check in self.expected:
            reason = check(path, a, b)
            if reason:
                self.explained.append((path, reason))
                return
        self.differences.append((path, a, b, why))

    def walk(self, a, b, path=(), unit=None):
        if path and str(path[-1]) in IGNORED_KEYS:
            return
        if isinstance(a, dict) and isinstance(b, dict):
            # A unit both sides name applies to this dict's own "value" and "by" only.
            own = a.get("unit") if a.get("unit") == b.get("unit") and a.get("unit") in UNIT_KINDS else None
            for k in sorted(set(a) | set(b), key=str):
                if k in IGNORED_KEYS:
                    continue
                if k not in b:
                    self.differ(path + (k,), a[k], MISSING, "only in A")
                elif k not in a:
                    self.differ(path + (k,), MISSING, b[k], "only in B")
                else:
                    self.walk(a[k], b[k], path + (k,), own)
            return
        if isinstance(a, list) and isinstance(b, list):
            if len(a) != len(b):
                self.differ(path, f"{len(a)} items", f"{len(b)} items", "list length")
                return
            for i, (x, y) in enumerate(zip(a, b)):
                self.walk(x, y, path + (i,))
            return

        self.values += 1
        if path and str(path[-1]) in PATH_KEYS and isinstance(a, str) and isinstance(b, str):
            if os.path.basename(a) != os.path.basename(b):
                self.differ(path, a, b, "file name")
            return
        if is_number(a) and is_number(b) and (isinstance(a, float) or isinstance(b, float)):
            kind = UNIT_KINDS[unit] if unit and str(path[-1]) in UNIT_VALUE_KEYS else kind_of(path)
            delta = abs(a - b) if not (math.isnan(a) or math.isnan(b)) else 0.0
            label = self.label(path)
            if delta > self.largest.get(label, (kind, -1.0))[1]:
                self.largest[label] = (kind, delta)
            if not numbers_match(a, b, kind):
                limit = f"{LIMITS[kind]:g}" if kind in LIMITS else f"{REL_OTHER:g} relative"
                self.differ(path, a, b, f"{kind} beyond {limit}")
            return
        if a != b:
            self.differ(path, a, b, "not equal")

    def largest_by_kind(self):
        """{kind: (field label, largest delta)} over every numeric field compared."""
        best = {}
        for label, (kind, delta) in self.largest.items():
            if delta > best.get(kind, ("", -1.0))[1]:
                best[kind] = (label, delta)
        return best
