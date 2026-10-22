"""
cores - vendored copies of the MAD platform's Rec QC code, run by RecQC unmodified.

    level_check_phrase_core.py        <- MAD/Level Check Phrase Test
    noise_profile_scanner_core.py     <- MAD/Noise Profile Scanner
    configs/noise/platform_band.yaml  <- MAD/Noise Profile Scanner/configs
    rt60_analyzer_core_v5.py          <- MAD/RT60 Analyzer
    configs/rt60/*.yaml               <- MAD/RT60 Analyzer/configs
    screening_pipeline_core.py        <- MAD/Screening Pipeline (the stage gates)
    provenance.py, tiers.py           <- MAD/Talent Platform/app
    platform_ceiling.py               <- two functions lifted from
                                         MAD/Talent Platform/app/audition_reviews.py
    platform_ingest.py                <- _derive_working, lifted from app/cores.py
    platform_clap.py                  <- the clap-collection functions and constants,
                                         lifted from app/cores.py
    platform_stages.py                <- rt60_in_band, lifted from app/drift_baseline.py

Those files are copied in by py/vendor_cores.sh and never edited here. A change a
core needs is made in that core's own repo, committed there, then vendored again.
VENDORED.json records each file's source folder, repo, commit and sha256.

This file and _vendor_check.py are RecQC's own code, not vendored.

The start check: the first import of this package compares every file listed in
VENDORED.json with its recorded sha256, before any vendored module can load. On a
mismatch it raises VendoredCoreError naming the file, and RecQC does not start.
"""
from pathlib import Path

from ._vendor_check import VendoredCoreError, check_vendored, load_manifest, require_vendored

CORES_DIR = Path(__file__).resolve().parent

require_vendored(CORES_DIR)
