# 15 fps QR trial validation — 2026-09-26

The scheduler target is 15 fps (66⅔ ms per image). Relative to 12 fps, this
offers 25% more images per second and 20% less dwell time. Thirty sensor frames
per second does not guarantee two usable camera images per QR: exposure,
capture buffering, decoding, and display presentation also matter.
**One user-reported physical completion is recorded below; comparative results
are still pending.**

The wallet's fixed version remains the comparison baseline, not an evidence-based
winner. The diagnostic now supports matched fixed/per-frame trials at one frame
rate. See [the protocol and measured geometry](qr-framing-trials.md).

## Automated validation

- QR suite: **166 passed**, including both framing policies through high sequence
  numbers, exact QR contents, diagnostic policy selection and result records,
  15 fps deadlines, queue starvation, cancellation, resizing, and mask changes.
- Rendering/exchange suites: **44 passed each** at Qt scale factors 1.5 and 2.
- `scripts/check.sh`: Ruff and mypy pass (99 production files). Whole-tree ty
  remains informational with existing diagnostics outside the changed surface.
  Separate Ruff and mypy checks of the diagnostic pass.
- Full suite: **2,172 passed, 11 failed, 2 skipped, 42 deselected**. The same
  11 sandbox-protected cache-write failures in `test_tokens_stateful.py` and
  `test_ui_ens_plugin.py` were previously reproduced on the unchanged baseline;
  this is not a fully green suite. Final diagnostic assertion additions were
  also checked separately.
- macOS bundle built successfully; `codesign --verify --deep --strict` passes.
  Archive inspection confirms the packaged 15 fps scheduler and retained
  fixed-version baseline. Full wallet GUI/camera launch was not tested.

These establish software behavior, not optical transfer performance. The agent
did not independently observe the physical trial below. The previous decoder simulations
remain evidence about reconstruction under selected losses, not camera behavior.

## First user-reported Shell result

On 2026-09-26, the operator recorded completion of the 44,115-byte request in
**166.4309 seconds**, at a 15 fps target with per-frame version selection. The
diagnostic installed 2,476 images (about 14.9 per second): 274 had 85 modules
including borders, and 2,202 had 89. The requested QR width was 320 logical
pixels, DPR 2; brightness and viewing distance were not recorded. Payload SHA-256:
`69146eef04cac8a36fb1ecba607b3a9989f678f1bfa44d9461673fb5d09af632`.

The preceding fixed-policy attempt was restarted after 19.65 seconds; it is an
aborted attempt, not a completion-time comparator. This evidence cannot rank
the framing policies or diagnose the cause of slow reconstruction. Installation
rate was near target, but actual presentation and camera decoding were unmeasured.
Both grid sizes used 7 backing-store pixels per module at this width/DPR.

The original repeated 44 KB protocol imposed too much operator time for this
initial comparison. The revised protocol starts with an eight-chunk dense fixture
and one pair capped at 30 seconds per attempt. It preserves the version boundary
but does not predict large-message performance.

## App

The separately named build preserves the earlier 12 fps artifact:

```sh
open /Users/bryan/src/qeth/dist/macos/out/qr15/qeth-macos-qr15.app
```

Quit another running qeth before opening this app: the trial shares its application
identity and wallet configuration. `QETH_QR_TRIAL=1` changes only the bundle name;
15 fps is the source default. The framing comparison runs from the diagnostic
command in the protocol, not from wallet settings.
