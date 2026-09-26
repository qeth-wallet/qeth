# 12 fps QR trial validation — 2026-09-25

Historical snapshot of the fixed-grid 12 fps build. The current trial is
[10 fps with fixed framing](qr10-validation.md). The intervening
[15 fps framing measurements](qr15-validation.md) remain recorded separately.

This build targets 12 fps. Faster **physical transfer completion is unverified**.
No physical Shell attempt was performed during this implementation session.

## Automated checks

- `uv run pytest -q tests/test_qr*.py`: **154 passed**. Covers exact payload
  reconstruction with periodic misses, random losses, bursts, and late starts;
  shuffled retry coverage, standard recovery selection, bounded alias search,
  fractional deadlines, starvation, cancellation/destruction, worker errors,
  static requests, resizing, borders, mask cycling, and diagnostic records.
- Rendering/exchange suites at `QT_SCALE_FACTOR=1.5` and `2`: **41 passed each**.
- `scripts/check.sh`: Ruff and mypy passed (99 production files). The repository's
  informational whole-tree ty run still reports diagnostics outside the changed
  files; ty on all changed production Python files and the diagnostic passes.
  Separate Ruff/mypy checks of the diagnostic also pass.
- Full `uv run pytest -q`: **2,160 passed, 11 failed, 2 skipped, 42 deselected**.
  All 11 failures attempt writes to sandbox-protected `~/.qeth/token_metadata`
  or `~/.qeth/ens`. An unchanged HEAD archive reproduces all 11 in
  `test_tokens_stateful.py` and `test_ui_ens_plugin.py` (184 pass in that baseline
  subset). These failures are not QR regressions; the full suite is not green.
- The separately named macOS bundle builds successfully. `codesign --verify
  --deep --strict` passes. Archive inspection confirms the packaged module has
  `TARGET_FPS = 12.0`, `QRAnimation`, and `FramePreparer`. Bundle display name:
  **qeth QR 12fps**. Full wallet GUI/camera launch was not tested.

## Display cadence, not optical measurements

Offscreen Qt, 44,345-byte payload, 25 displayed frames per rate on this Mac.
Times measure GUI image installations, not physical monitor presentation or
camera decode attempts. OS scheduling may reduce the achieved rate; stalls
rebase the deadline instead of producing catch-up flashes.

| Target fps | Observed fps | Minimum dwell (ms) | Maximum dwell (ms) |
| --- | --- | --- | --- |
| 5 | 4.923 | 200.61 | 208.20 |
| 8 | 7.907 | 124.88 | 127.78 |
| 10 | 9.774 | 100.14 | 110.01 |
| 12 | 11.684 | 83.38 | 86.82 |

## Supplementary native decoder simulation

The existing local C harness in `/tmp/qeth-qr-native-study` was fed production
`frame_source` output. Its `ur.c` was compared byte-for-byte with Shell v1.4.0.
Its completion check compares reconstructed length and every payload byte.
Each stream used 44,345 pseudorandom bytes and up to 3,000 frames. All 12 trials
completed exactly; this is decoder evidence, not physical Shell evidence.

| Loss scenario | Seed 71 | Seed 117 | Seed 892 |
| --- | --- | --- | --- |
| Accept every third frame | 865 | 1180 | 850 |
| Drop 60% randomly | 731 | 720 | 685 |
| Drop first 60 of each 90-frame burst | 1070 | 1151 | 1144 |
| Start after frame 384 | 575 | 567 | 568 |

Numbers are displayed-frame positions to exact completion, including frames
missed before a late start. They must not be converted into claimed hardware
transfer times. The maintained Python reconstruction regressions are in
`tests/test_qr_multipart.py`; supplementary native results and runner are in
`/tmp/qeth-qr12-native.json` and `/tmp/qeth-qr12-native.py`.

## Physical comparison pending

Use the procedure in [signers-qr.md](signers-qr.md#hardware-transfer-trials).
Keep the unsigned payload, QR size, brightness, and camera distance constant.
Record repeated time-to-100% results and all failed attempts at 5, 8, 10, 12 fps.
The harness writes `/tmp/qeth-qr-trials.jsonl`; it does not infer success from
animation speed. There are no physical results to report for any of these rates.

Launch the separately named app:

```sh
open /Users/bryan/src/qeth/dist/macos/out/qr12/qeth-macos-qr12.app
```
