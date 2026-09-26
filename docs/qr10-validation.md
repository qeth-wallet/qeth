# 10 fps QR trial — 2026-09-26

The current scheduler target is **10 fps (100 ms per image)**, with the same
fixed QR version per transfer. After trying the 15 fps app, the operator reported
that it felt too fast and that the Shell screen refreshed more slowly than the
laptop. This motivates a slower trial; screen refresh alone does not measure
camera decoding throughput.

Compared with 15 fps, 10 fps provides 50% longer dwell and one-third fewer images
per second. It is a tradeoff, not a proven transfer-speed improvement. No timed
10 fps physical result is available yet. The [15 fps framing observations and
decision](qr15-validation.md) remain historical evidence; they do not compare rates.

Only the target cadence changes in production. Fixed framing, fragment size,
retry selection, worker queue, stall handling, and static requests are preserved.
The diagnostic default follows the same target and still accepts explicit rates.
Tests also exercise diagnostic records and stall rebasing at 10 fps.

## Validation

- QR and affected wallet UI suites: **227 passed**, including 10 fps stall
  rebasing, default-cadence starvation behavior, and diagnostic records.
- `scripts/check.sh`: Ruff and mypy pass (99 production files). Whole-tree ty
  remains informational with existing diagnostics outside this change.
- macOS qr10 bundle built successfully and passed strict/deep codesign
  verification. Archive inspection confirmed the packaged 10 fps target and
  verified that the retained qr15 app still contains its 15 fps scheduler.
- No timed physical result or full wallet launch at 10 fps has been observed
  by the agent. The earlier green full-suite CI run at `363cc30` validates the
  preceding 15 fps source, not this new cadence change.

## Launch

Quit any running qeth before opening the trial; it shares the wallet configuration
and application identity. The separately named output preserves the local 15 fps
app for comparison:

```sh
open /Users/bryan/src/qeth/dist/macos/out/qr10/qeth-macos-qr10.app
```

`QETH_QR_TRIAL=1` changes only naming. The source default is 10 fps, including in
ordinary builds. The local qr15 app remains at its earlier source revision.
