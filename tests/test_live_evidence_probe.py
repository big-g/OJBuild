"""Check probe verdict accounting without pretending to test a live model."""

import json
import runpy
from pathlib import Path


def _probe():
    return runpy.run_path(str(
        Path(__file__).resolve().parents[1] / "scripts" / "check_live_evidence.py"
    ))


def test_probe_accepts_complete_expected_verdict_sequence():
    probe = _probe()
    verdicts = iter([
        {"supported": True, "unsupported_claims": [], "reason": "Supported"},
        {"supported": False, "unsupported_claims": ["canceled"], "reason": "Unsupported"},
        {"conflicting": True, "conflicts": [{
            "claim": "Launch status", "source_ids": ["E1", "E2"],
            "values": ["approved", "canceled"],
        }], "reason": "Incompatible status"},
        {"conflicting": False, "conflicts": [], "reason": "Consistent"},
        {"supported": True, "unsupported_claims": [], "reason": "Supported"},
    ])

    class Engine:
        def generate(self, *args, **kwargs):
            return {"content": json.dumps(next(verdicts))}

    for case in probe["CASES"]:
        passed, detail = probe["run_case"](Engine(), "test", case)
        assert passed, f"{case.name}: {detail}"
    assert next(verdicts, None) is None


def test_probe_does_not_count_validator_outage_as_successful_block():
    probe = _probe()

    class OfflineEngine:
        def generate(self, *args, **kwargs):
            raise ConnectionError("offline")

    results = {
        case.name: probe["run_case"](OfflineEngine(), "test", case)[0]
        for case in probe["CASES"]
    }
    assert results == {
        "supported": False,
        "unsupported_relationship": False,
        "unsupported_number": True,
        "conflicting_sources": False,
        "agreeing_sources": False,
        "failed_tool": True,
        "empty_retrieval": True,
    }
