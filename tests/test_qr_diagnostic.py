"""The diagnostic uses identical payloads and records actual display attempts."""

import argparse
import hashlib
import json
import random
import runpy
from pathlib import Path

import pytest


@pytest.mark.parametrize("fps", [10, 12, 15])
@pytest.mark.parametrize("policy", ["fixed", "per-frame", "compare"])
def test_diagnostic_payload_and_result_records(qtbot, tmp_path, fps, policy):
    harness = runpy.run_path(
        str(Path(__file__).parents[1] / "scripts/keycard_fountain_gui.py")
    )
    build = harness["build_message"]
    ur_type, message = build(120, None)
    assert build(120, None) == (ur_type, message)
    ur_type, message = build(120, random.Random(892).randbytes(44_000))
    assert len(message) == 44_115
    args = argparse.Namespace(
        fps=fps,
        qr_size=320,
        seed=71,
        fragment_len=None,
        timeout=180,
        results=str(tmp_path / "trials.jsonl"),
        brightness="50%",
        distance_cm=10,
        grid_policy="per-frame" if policy == "per-frame" else "fixed",
        compare_framing=policy == "compare",
    )
    window = harness["FountainWindow"](ur_type, message, args)
    qtbot.addWidget(window)
    window.show()
    first_frames = []
    for attempt, outcome in enumerate(["complete", "failed", "timeout", "complete"]):
        if attempt:
            window._restart()
        qtbot.waitUntil(lambda: window._started is not None)
        first_frames.append(window._animation.shown.content)
        assert window._animation.shown.image.width() == (
            89 if window._grid_policy == "fixed" else 85
        )
        window._record(outcome)
        window._record(outcome)  # No duplicate completion on key repeat.
    assert first_frames == [first_frames[0]] * 4
    rows = [json.loads(line) for line in Path(args.results).read_text().splitlines()]
    assert [r["outcome"] for r in rows] == ["complete", "failed", "timeout", "complete"]
    assert [r["attempt"] for r in rows] == [1, 2, 3, 4]
    assert [r["grid_policy"] for r in rows] == (
        ["fixed", "per-frame", "per-frame", "fixed"]
        if policy == "compare"
        else [policy] * 4
    )
    for row in rows:
        assert row["payload_sha256"] == hashlib.sha256(message).hexdigest()
        assert row["fps"] == fps
        assert row["frames"] >= 1
        assert sum(row["source_modules_including_border"].values()) == row["frames"]
        assert row["seconds"] >= 0
        assert (row["brightness"], row["distance_cm"]) == ("50%", 10)
