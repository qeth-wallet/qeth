"""The diagnostic uses identical payloads and records actual display attempts."""

import argparse
import hashlib
import json
import runpy
from pathlib import Path


def test_diagnostic_payload_and_result_records(qtbot, tmp_path):
    harness = runpy.run_path(
        str(Path(__file__).parents[1] / "scripts/keycard_fountain_gui.py")
    )
    build = harness["build_message"]
    ur_type, message = build(120, None)
    assert build(120, None) == (ur_type, message)
    args = argparse.Namespace(
        fps=12,
        qr_size=320,
        seed=71,
        fragment_len=None,
        timeout=180,
        results=str(tmp_path / "trials.jsonl"),
        brightness="50%",
        distance_cm=10,
    )
    window = harness["FountainWindow"](ur_type, message, args)
    qtbot.addWidget(window)
    window.show()
    qtbot.waitUntil(lambda: window._started is not None)
    window._record("complete")
    window._record("complete")  # No duplicate completion on key repeat.
    window._restart()
    qtbot.waitUntil(lambda: window._started is not None)
    window._record("failed")
    rows = [json.loads(line) for line in Path(args.results).read_text().splitlines()]
    assert [r["outcome"] for r in rows] == ["complete", "failed"]
    assert [r["attempt"] for r in rows] == [1, 2]
    for row in rows:
        assert row["payload_sha256"] == hashlib.sha256(message).hexdigest()
        assert row["fps"] == 12
        assert row["frames"] >= 1
        assert row["seconds"] >= 0
        assert (row["brightness"], row["distance_cm"]) == ("50%", 10)
