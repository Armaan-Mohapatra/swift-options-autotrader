"""Report cannot drift from the parser behavior checked into GitHub."""
import json
from pathlib import Path

from scripts.export_synthetic_parser_results import generate


def test_checked_in_synthetic_results_match_current_parser():
    root = Path(__file__).resolve().parents[2]
    report = json.loads(
        (root / "docs/PHASE_REPORTS/PHASE_2_PARSED_EXAMPLES.json").read_text(encoding="utf-8")
    )
    assert report == generate()
    assert report["sample_count"] == 81
    assert report["intent_agreement_count"] == 81
