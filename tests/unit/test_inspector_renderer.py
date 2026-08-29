from __future__ import annotations

import json
import re

import pytest

from recall_origin.inspector import render_inspector


def _fixture_payload() -> dict[str, object]:
    return {
        "generated_at": "2026-08-30T09:41:12Z",
        "timeline": [
            {
                "timestamp": "2026-08-30T09:31:00Z",
                "kind": "formation",
                "title": "User prefers evidence-backed answers",
                "summary": "Formed from an explicit preference in the current conversation.",
                "claim_id": "clm_01J8PREF",
                "status": "candidate",
                "evidence": ["evt_01J8TURN"],
            },
            {
                "timestamp": "2026-08-30T09:38:04Z",
                "kind": "revision",
                "title": "Preference verified",
                "summary": "Promoted after a second explicit confirmation.",
                "claim_id": "clm_01J8PREF",
                "status": "verified",
                "evidence": ["evt_01J8TURN", "evt_01J8CONFIRM"],
            },
        ],
        "retrieval": {
            "query": "How should the answer cite evidence?",
            "strategy": "fts5 + rrf",
            "latency_ms": 4.7,
            "candidates": [
                {
                    "rank": 1,
                    "claim_id": "clm_01J8PREF",
                    "title": "User prefers evidence-backed answers",
                    "score": 0.92,
                    "lexical_score": 0.81,
                    "rrf_score": 0.032,
                    "selected": True,
                    "reason": "Direct preference match.",
                    "evidence_refs": ["evt_01J8CONFIRM"],
                }
            ],
        },
        "benchmarks": {
            "baseline_label": "FTS only",
            "candidate_label": "FTS + RRF",
            "metrics": [
                {
                    "name": "Recall@10",
                    "unit": "%",
                    "baseline": 71.2,
                    "candidate": 78.6,
                    "sample_size": 240,
                    "direction": "higher",
                },
                {
                    "name": "p95 latency",
                    "unit": "ms",
                    "baseline": 3.8,
                    "candidate": 5.1,
                    "sample_size": 1000,
                    "direction": "lower",
                },
            ],
            "notes": ["Same corpus, query set, and warm-cache protocol."],
        },
    }


def _embedded_payload(document: str) -> dict[str, object]:
    match = re.search(
        r'<script type="application/json" id="inspector-data">(.*?)</script>',
        document,
        flags=re.DOTALL,
    )
    assert match is not None
    value = json.loads(match.group(1))
    assert isinstance(value, dict)
    return value


def test_renders_standalone_three_view_document_from_realistic_payload() -> None:
    payload = _fixture_payload()

    document = render_inspector(payload)

    assert document.startswith("<!doctype html>")
    assert "<title>RecallOrigin Inspector</title>" in document
    assert "Memory Timeline" in document
    assert "Retrieval Explain" in document
    assert "Benchmark Compare" in document
    assert 'role="tablist"' in document
    assert 'role="tabpanel"' in document
    assert "@media print" in document
    assert "@media (max-width: 520px)" in document
    assert "@media (prefers-reduced-motion: reduce)" in document
    assert re.search(r"<(?:script|link)[^>]+(?:src|href)=", document) is None
    assert _embedded_payload(document) == payload


def test_payload_cannot_close_script_or_inject_html() -> None:
    attack = '</script><script>alert("owned")</script><img src=x onerror=alert(1)>'
    payload: dict[str, object] = {
        "timeline": [{"title": attack, "summary": attack}],
        "retrieval": {"query": attack, "candidates": [{"title": attack}]},
        "benchmarks": {"metrics": [{"name": attack}]},
    }

    document = render_inspector(payload, title=attack)

    assert attack not in document
    assert "\\u003c/script\\u003e" in document
    assert "<img src=x" not in document
    assert "<title>&lt;/script&gt;" in document
    assert ".innerHTML" not in document
    assert "textContent" in document
    assert _embedded_payload(document) == payload


def test_empty_payload_has_explicit_empty_states_and_no_fabricated_measurements() -> None:
    document = render_inspector({})

    assert "No memory events in this snapshot" in document
    assert "No retrieval candidates recorded" in document
    assert "No benchmark measurements supplied" in document
    assert "This Inspector does not estimate results." in document
    assert '"baseline":' not in document
    assert '"candidate":' not in document


def test_non_finite_numbers_are_rejected_instead_of_rendered_as_fake_json() -> None:
    with pytest.raises(ValueError, match="Out of range float values"):
        render_inspector({"benchmarks": {"metrics": [{"candidate": float("nan")}]}})


def test_requires_a_dictionary_payload() -> None:
    with pytest.raises(TypeError, match="payload must be a dict"):
        render_inspector([])  # type: ignore[arg-type]


def test_template_marker_text_in_user_data_is_preserved() -> None:
    payload: dict[str, object] = {"timeline": [{"title": "__DOCUMENT_TITLE__"}]}

    document = render_inspector(payload, title="Snapshot __INSPECTOR_PAYLOAD__")

    assert "<title>Snapshot __INSPECTOR_PAYLOAD__</title>" in document
    assert _embedded_payload(document) == payload
