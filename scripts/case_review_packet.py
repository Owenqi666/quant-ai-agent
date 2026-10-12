"""Compatibility imports for CLI callers; implementation is an installable library."""
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from paper_alpha.review_material_packet import (
    AUTHOR_IDS, IMPLEMENTATION_FILES, LIMITATIONS, MAX_BUNDLE, MAX_JSON, MAX_SOURCE,
    ROOT, blank_judgments, decode, load_bundle, render_report, require, safe_read,
    source_url, verify_packet,
)
