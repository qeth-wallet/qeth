# Matched framing trials

Neither fixed version nor per-frame version has established better physical
Shell transfer performance. The wallet keeps the existing fixed-version
baseline while this diagnostic compares the two through the same worker,
scheduler, renderer, fragment order, and mask-variation logic. No firmware,
transaction-signing behavior, or wallet settings change.

## What the evidence establishes

Shell [redetects QR codes for each camera image](https://github.com/keycard-tech/keycard-shell/blob/825ba4cae48d88a20e91ac7a70c3acf407879f87/app/qrcode/qrscan.c#L174-L187).
Its [decoder resets grids and finder candidates](https://github.com/keycard-tech/keycard-shell/blob/825ba4cae48d88a20e91ac7a70c3acf407879f87/app/qrcode/qrcode.c#L1736-L1756).
Fixed version therefore should not be defended as preserving decoder state.

Software measurements of 400 production frames, using unsigned requests with
deterministic random calldata (seed 892) and frame-order seed 71:

| Request bytes | Smallest versions observed | Reserved version |
| --- | --- | --- |
| 1,915 | 9 | 9 |
| 10,115 | 9 | 9 |
| 44,115 | 15, 16 | 16 |

For the 44,115-byte request, version 15 has 85 modules including the white
border; version 16 has 89. Both use error correction L in this sample.
Integer scaling produces these dimensions (pixels in Qt's backing store,
not a claim about final physical monitor pixels):

| Available width | Per-frame v15 | Per-frame v16 / fixed v16 |
| --- | --- | --- |
| 320 px | 255 px wide, 3 px/module | 267 px wide, 3 px/module |
| 340 px | 340 px wide, 4 px/module | 267 px wide, 3 px/module |
| 640 px | 595 px wide, 7 px/module | 623 px wide, 7 px/module |
| 680 px | 680 px wide, 8 px/module | 623 px wide, 7 px/module |

The cost of fixed version depends on the integer-scaling threshold. It sometimes
changes only the footprint, and sometimes sacrifices a larger module size.
Per-frame selection can cause substantial footprint changes at those thresholds.
Neither geometry calculation establishes which effect matters more to Shell.
Adding white padding would not stabilize the finder-pattern positions inside it.

## Run the comparison

From `/Users/bryan/src/qeth`:

```sh
uv run python scripts/keycard_fountain_gui.py --fps 15 --compare-framing \
  --calldata-bytes 44000 --qr-size 320 \
  --results /tmp/qeth-framing-320.jsonl
```

This creates the same 44,115-byte unsigned request each time. Successive attempts
cycle **fixed, per-frame, per-frame, fixed**, repeating every four attempts.
The title and status show the policy. Each attempt resets the same frame order
and mask-visit sequence. Changing QR version also changes the encoded pattern
and may change Segno's chosen mask/error level: this is a comparison of the
complete encoding policies, not an isolated laboratory measurement of geometry.

1. Keep the display mode, brightness, window position, and room lighting fixed.
   Support the Shell at a repeatable distance/angle where the whole QR and white
   border fit. Do not move it to compensate when a frame becomes smaller.
2. Reset the Shell scanner before every attempt. The first attempt starts on
   launch; if it began before you were ready, close and relaunch to start a fresh
   block. The log retains the closed attempt; report it as an operator abort,
   separately from an optical failure.
   Press R only when the scanner is ready for the next attempt. Enter records
   reaching 100%; X records failure; the timeout is 180 seconds. Q closes.
3. Complete two four-attempt blocks (four attempts per policy). Do not sign or
   broadcast the diagnostic transaction. Keep aborted and timed-out attempts in
   the results. If more attempts are needed, add complete blocks for both policies.
4. Quit and repeat at `--qr-size 340`, writing to
   `/tmp/qeth-framing-340.jsonl`. This probes an integer-scaling threshold; keep
   distance and other conditions fixed within each size. Record any repositioning
   between sizes. Use `--brightness` and `--distance-cm` to attach measurements.

For an actual unsigned calldata fixture, replace `--calldata-bytes 44000` with
`--calldata /path/to/calldata.hex`. To run one policy explicitly, replace
`--compare-framing` with `--grid-policy fixed` or `--grid-policy per-frame`.
Small requests may generate identical images under both policies; that is a
useful control, but cannot choose a winner for dense transfers.

## Interpret results

Records include payload hash, policy, fps, frame-order seed, requested QR size,
DPR, source grid sizes including borders, elapsed time, and outcome. `frames`
counts image installations, not monitor presentations or camera decodes.
Timing begins at the first installation and ends at the operator's keypress;
manual reaction time and compositor delay remain measurement limitations.

Compare failures/timeouts first, then median and range of completion times at
each size. Keep the attempt order visible to detect practice or lighting drift.
A policy that is faster only when it finishes is not an unconditional improvement.
If results disagree across sizes, report that dependence rather than declare a
universal winner. Repeat on the actual signing payload before changing the wallet
default. After selecting framing, compare 12 and 15 fps separately with that
policy held constant. Fifteen fps is a trial target, not a proven improvement.
