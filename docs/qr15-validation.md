# 15 fps QR trial validation — 2026-09-26

The scheduler target is 15 fps (66⅔ ms per image). Relative to 12 fps, this
offers 25% more images per second and 20% less dwell time. Thirty sensor frames
per second does not guarantee two usable camera images per QR: exposure,
capture buffering, decoding, and display presentation also matter.
**Physical transfer results are pending.**

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

These establish software behavior, not optical transfer performance. No physical
Shell measurements were made by the agent. The previous decoder simulations
remain evidence about reconstruction under selected losses, not camera behavior.

## App

The separately named build preserves the earlier 12 fps artifact:

```sh
open /Users/bryan/src/qeth/dist/macos/out/qr15/qeth-macos-qr15.app
```

Quit another running qeth before opening this app: the trial shares its application
identity and wallet configuration. `QETH_QR_TRIAL=1` changes only the bundle name;
15 fps is the source default. The framing comparison runs from the diagnostic
command in the protocol, not from wallet settings.
