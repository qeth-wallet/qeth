#!/usr/bin/env bash
# Run Trezor's firmware emulator — the local loop for Trezor work
# (docs/trezor-clear-signing.md), the Trezor side of scripts/speculos.sh. Not a
# gate: the hermetic suite covers qeth's side with a fake device, this checks
# it against the real firmware, and a real device only has to confirm USB.
#
#   scripts/trezor-emu.sh fetch            download the pinned emulators (sha256-checked)
#   scripts/trezor-emu.sh run [MODEL]      start one, loaded with the test seed
#                                          (MODEL: safe3 = T3B1, default | safe5 = T3T1)
#   scripts/trezor-emu.sh stop [MODEL]     stop it (default: all)
#   scripts/trezor-emu.sh test [MODEL]     run tests/test_trezor_emulator.py (default: all)
#   scripts/trezor-emu.sh qeth [MODEL]     launch qeth against it (TREZOR_PATH)
#
# The emulators are Trezor's own builds of the release firmware for Linux
# (data.trezor.io, the ones trezor-user-env and Suite's tests use). They're
# built with Nix — their loader is a /nix/store path and they need SDL3 — so
# each runs headless in a small Debian container (pinned by digest; its SDL3
# comes from trixie at build time) with the loader patched, as
# trezor-user-env does. Emulator builds take Trezor's production-signed
# definitions (the firmware tries the production keys first), so the tests
# use real ones (tests/data/trezor_definitions_sample.tar.xz).
#
# Each listens on loopback only: the Safe 3 on UDP 21324 (debug link 21325),
# the Safe 5 on 21334 / 21335. Both are loaded with Trezor's well-known test
# seed ("all" × 12), never a real one.
#
# Env: QETH_CONTAINER (podman | docker; default podman when present),
# QETH_TREZOR_EMU_CACHE (default ~/.cache/qeth-trezor-emu).
set -euo pipefail
cd "$(dirname "$0")/.."

cache=${QETH_TREZOR_EMU_CACHE:-$HOME/.cache/qeth-trezor-emu}
mkdir -p "$cache"

if [[ -n ${QETH_CONTAINER:-} ]]; then
    ctr=$QETH_CONTAINER
elif command -v podman >/dev/null; then
    ctr=podman
else
    ctr=docker
fi

firmware=2.13.1
base_image=docker.io/library/debian@sha256:1e1a22175fbf1b32157ddaf0219e1db4e5401fab2b94ed285add64d18587cd2d
emu_url=https://data.trezor.io/dev/firmware/releases/emulators-new
seed="all all all all all all all all all all all all"

model_internal() {
    case $1 in
        safe3) echo T3B1 ;;
        safe5) echo T3T1 ;;
        *) echo "unknown model: $1 (safe3 | safe5)" >&2; exit 2 ;;
    esac
}
model_sha256() {
    case $1 in
        safe3) echo 05f30ffdf553ef7c5f9293f9f3b062f2f42a11fb5d3e167d886f81f73ac4e9e8 ;;
        safe5) echo d3354da1ba54ac018c2c86c2686598e403ef235fc052a233c101ef461692729f ;;
    esac
}
model_port() { case $1 in safe3) echo 21324 ;; safe5) echo 21334 ;; esac; }
model_binary() { echo "$cache/trezor-emu-core-$(model_internal "$1")-v$firmware"; }
model_image() { echo "localhost/qeth-trezor-emu:$1-$firmware"; }

fetch_model() {
    local out
    out=$(model_binary "$1")
    if [[ ! -f $out ]]; then
        curl -fsSL -o "$out.part" "$emu_url/$(model_internal "$1")/$(basename "$out")"
        mv "$out.part" "$out"
    fi
    echo "$(model_sha256 "$1")  $out" | sha256sum -c --quiet
    echo "$1 emulator: $out"
}

cmd_fetch() {
    fetch_model safe3
    fetch_model safe5
}

ensure_image() {
    local model=$1 image ctx
    image=$(model_image "$model")
    "$ctr" image exists "$image" 2>/dev/null && return
    fetch_model "$model" >/dev/null
    ctx=$(mktemp -d)
    cp "$(model_binary "$model")" "$ctx/trezor-emu"
    cat > "$ctx/Containerfile" <<EOF
FROM $base_image
RUN apt-get update && apt-get install -y --no-install-recommends libsdl3-0 libsdl3-image0 patchelf \\
    && rm -rf /var/lib/apt/lists/*
COPY trezor-emu /emu/trezor-emu
RUN patchelf --set-interpreter /lib64/ld-linux-x86-64.so.2 --remove-rpath /emu/trezor-emu \\
    && chmod +x /emu/trezor-emu && mkdir /profile
ENV SDL_VIDEODRIVER=dummy TREZOR_DISABLE_ANIMATION=1 TREZOR_DISABLE_FADE=1 \\
    TREZOR_UDP_IP=0.0.0.0 TREZOR_PROFILE_DIR=/profile
WORKDIR /emu
ENTRYPOINT ["/emu/trezor-emu", "-O0", "-X", "heapsize=20M", "-m", "main"]
EOF
    "$ctr" build -q -t "$image" "$ctx" >/dev/null
    rm -rf "$ctx"
}

cmd_run() {
    local model=${1:-safe3} port name
    ensure_image "$model"
    port=$(model_port "$model")
    name=qeth-trezor-emu-$model
    "$ctr" rm -f "$name" >/dev/null 2>&1 || true
    "$ctr" run -d --rm --name "$name" -e TREZOR_UDP_PORT="$port" \
        -p "127.0.0.1:$port:$port/udp" -p "127.0.0.1:$((port + 1)):$((port + 1))/udp" \
        "$(model_image "$model")" >/dev/null
    # Wait for it, then load the test seed (a fresh container starts wiped).
    QETH_EMU_PORT=$port QETH_EMU_SEED=$seed uv run python - <<'EOF' || {
import os, sys, time
from trezorlib.debuglink import TrezorTestContext, load_device
from trezorlib.transport.udp import UdpTransport

transport = UdpTransport(f"127.0.0.1:{os.environ['QETH_EMU_PORT']}")
transport.open()
for _ in range(60):
    if transport.is_ready():
        break
    time.sleep(0.5)
else:
    sys.exit("emulator didn't come up")
transport.close()
ctx = TrezorTestContext(transport)
if not ctx.features.initialized:
    load_device(ctx.get_seedless_session(), os.environ["QETH_EMU_SEED"],
                pin=None, passphrase_protection=False, label="qeth test")
EOF
        "$ctr" logs "$name" 2>&1 | tail -20 >&2
        echo "$model: emulator didn't come up" >&2
        exit 1
    }
    echo "$model: 127.0.0.1:$port (debug link $((port + 1)))"
}

cmd_stop() {
    local model
    for model in ${@:-safe3 safe5}; do
        "$ctr" rm -f "qeth-trezor-emu-$model" >/dev/null 2>&1 || true
    done
}

cmd_test() {
    local models=("${@}") m env=()
    [[ ${#models[@]} -gt 0 ]] || models=(safe3 safe5)
    for m in "${models[@]}"; do
        cmd_run "$m"
        env+=("QETH_TREZOR_EMU_${m^^}=127.0.0.1:$(model_port "$m")")
    done
    local rc=0
    env "${env[@]}" uv run pytest -m trezor_emulator tests/test_trezor_emulator.py -v || rc=$?
    cmd_stop "${models[@]}"
    return $rc
}

cmd_qeth() {
    local model=${1:-safe3}
    cmd_run "$model"
    TREZOR_PATH=udp:127.0.0.1:$(model_port "$model") uv run python -m qeth
}

cmd=${1:-}
shift || true
case $cmd in
    fetch) cmd_fetch ;;
    run) cmd_run "$@" ;;
    stop) cmd_stop "$@" ;;
    test) cmd_test "$@" ;;
    qeth) cmd_qeth "$@" ;;
    *) sed -n '2,/^set -euo/p' "$0" | sed '$d' | sed 's/^# \{0,1\}//'; exit 2 ;;
esac
