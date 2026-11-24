#!/usr/bin/env bash
# Run Ledger's Speculos emulator with the real Ethereum app — the local loop for
# Ledger work (docs/ledger-clear-signing.md). Not a gate, like scripts/wine.sh:
# the hermetic suite covers the protocol with fakes, this checks it against the
# app itself, and a real device only has to confirm the USB layer.
#
#   scripts/speculos.sh fetch          download the pinned Nano X / S Plus apps (release ELFs)
#   scripts/speculos.sh build-nanos    build the last Nano S app (1.16.0) from source
#   scripts/speculos.sh run MODEL      start the emulator (MODEL: nanos | nanox | nanosp)
#   scripts/speculos.sh stop [MODEL]   stop it (default: both)
#   scripts/speculos.sh test [MODEL]   run tests/test_ledger_speculos.py (default: all)
#   scripts/speculos.sh qeth MODEL     launch qeth against the emulator
#
# Ledger stopped shipping the Nano S in app 1.17.0 and in Speculos 0.25.7, and
# its release ELFs only start at 1.22.0, so the Nano S app is built from the
# 1.16.0 tag in the app-builder image of the same day (4.3.0) and emulated by
# Speculos 0.25.5. The Nano X and Nano S Plus run the current release on
# current Speculos. (Stax / Flex are touchscreens; the test driver only
# presses buttons so far.)
# Everything runs in containers (the Speculos image bundles qemu-arm), so the
# host needs only podman or docker.
#
# The emulator listens on loopback only: Nano S APDUs on 9999 / its REST API on
# 5000, Nano X on 9998 / 5001, Nano S Plus on 9997 / 5002. Both use Speculos' well-known test seed, never a
# real one.
#
# Env: QETH_CONTAINER (podman | docker; default podman when present),
# QETH_SPECULOS_CACHE (default ~/.cache/qeth-speculos).
set -euo pipefail
cd "$(dirname "$0")/.."

cache=${QETH_SPECULOS_CACHE:-$HOME/.cache/qeth-speculos}
mkdir -p "$cache"

if [[ -n ${QETH_CONTAINER:-} ]]; then
    ctr=$QETH_CONTAINER
elif command -v podman >/dev/null; then
    ctr=podman
else
    ctr=docker
fi

seed="glory promote mansion idle axis finger extra february uncover one trip resource lawn turtle enact monster seven myth punch hobby comfort wild raise skin"

builder_image=ghcr.io/ledgerhq/ledger-app-builder/ledger-app-builder-lite:4.3.0
nanos_app_tag=1.16.0
nanos_app_commit=ab139cb228336af463084dbc6c89e8b9a600a0e1
nanox_app_version=1.24.0
nanox_app_sha256=484f6621fe569cf7cd0edacb352a8f0dac36f164f8b167b6db61e00782340e49
# The release names the Nano S Plus "nanos2".
nanosp_app_sha256=0e4005b32369e5cd3152ab6bd887a421b8daf0c6705da0b8c299fcc80d2c101b

model_app() {
    case $1 in
        nanos) echo "$cache/app-$nanos_app_tag-nanos.elf" ;;
        nanox) echo "$cache/app-$nanox_app_version-nanox.elf" ;;
        nanosp) echo "$cache/app-$nanox_app_version-nanosp.elf" ;;
        *) echo "unknown model: $1 (nanos | nanox | nanosp)" >&2; exit 2 ;;
    esac
}
model_image() {
    case $1 in
        nanos) echo ghcr.io/ledgerhq/speculos:0.25.5 ;;
        # 0.27.4 (no version tag is published past 0.25.13; app 1.24 needs
        # API level 27), pinned by digest.
        nanox|nanosp) echo ghcr.io/ledgerhq/speculos@sha256:be01fba8c774677f68c1b2711a99fd52d4defc08c1c4cdee201c619e093be2eb ;;
    esac
}
model_apdu_port() { case $1 in nanos) echo 9999 ;; nanox) echo 9998 ;; nanosp) echo 9997 ;; esac; }
model_api_port() { case $1 in nanos) echo 5000 ;; nanox) echo 5001 ;; nanosp) echo 5002 ;; esac; }

fetch_release_app() {   # MODEL RELEASE-NAME SHA256
    local out
    out=$(model_app "$1")
    if [[ ! -f $out ]]; then
        curl -fsSL -o "$out.part" \
            "https://github.com/LedgerHQ/app-ethereum/releases/download/$nanox_app_version/app-$nanox_app_version-$2.elf"
        mv "$out.part" "$out"
    fi
    echo "$3  $out" | sha256sum -c --quiet
    echo "$1 app: $out"
}

cmd_fetch() {
    fetch_release_app nanox nanox "$nanox_app_sha256"
    fetch_release_app nanosp nanos2 "$nanosp_app_sha256"
}

cmd_build_nanos() {
    local src=$cache/app-ethereum-$nanos_app_tag out
    out=$(model_app nanos)
    if [[ ! -d $src ]]; then
        # The plugin-SDK submodule is declared with an ssh URL; fetch it over https.
        git -c url."https://github.com/".insteadOf="git@github.com:" clone -q \
            --depth 1 --branch "$nanos_app_tag" --recurse-submodules --shallow-submodules \
            https://github.com/LedgerHQ/app-ethereum.git "$src"
    fi
    [[ $(git -C "$src" rev-parse HEAD) == "$nanos_app_commit" ]] || {
        echo "app-ethereum $nanos_app_tag is not at the pinned commit" >&2; exit 1; }
    "$ctr" run --rm -v "$src:/app:Z" -w /app "$builder_image" \
        bash -c 'make -j"$(nproc)" BOLOS_SDK="$NANOS_SDK" >/dev/null && cp build/nanos/bin/app.elf /app/app-nanos.elf'
    mv "$src/app-nanos.elf" "$out"
    echo "nanos app: $out"
}

ensure_app() {
    local app
    app=$(model_app "$1")
    [[ -f $app ]] && return
    case $1 in
        nanos) cmd_build_nanos ;;
        nanox|nanosp) cmd_fetch ;;
    esac
}

cmd_run() {
    local model=$1 app name api
    ensure_app "$model"
    app=$(model_app "$model")
    name=qeth-speculos-$model
    api=$(model_api_port "$model")
    "$ctr" rm -f "$name" >/dev/null 2>&1 || true
    "$ctr" run -d --rm --name "$name" \
        -v "$(dirname "$app"):/apps:ro,Z" \
        -p "127.0.0.1:$(model_apdu_port "$model"):9999" -p "127.0.0.1:$api:5000" \
        "$(model_image "$model")" \
        --model "$model" --display headless --apdu-port 9999 --api-port 5000 \
        --seed "$seed" "/apps/$(basename "$app")" >/dev/null
    for _ in $(seq 60); do
        curl -fs "http://127.0.0.1:$api/events" >/dev/null 2>&1 && {
            echo "$model: APDU 127.0.0.1:$(model_apdu_port "$model"), API http://127.0.0.1:$api"
            return; }
        sleep 0.5
    done
    "$ctr" logs "$name" >&2 || true
    echo "$model: emulator didn't come up" >&2
    exit 1
}

cmd_stop() {
    local model
    for model in "${@:-nanos nanox nanosp}"; do
        for m in $model; do
            "$ctr" rm -f "qeth-speculos-$m" >/dev/null 2>&1 || true
        done
    done
}

cmd_test() {
    local models=("${@}") m env=()
    [[ ${#models[@]} -gt 0 ]] || models=(nanos nanox nanosp)
    for m in "${models[@]}"; do
        cmd_run "$m"
        env+=("QETH_SPECULOS_${m^^}=127.0.0.1:$(model_apdu_port "$m")"
              "QETH_SPECULOS_${m^^}_API=http://127.0.0.1:$(model_api_port "$m")")
    done
    local rc=0
    env "${env[@]}" uv run pytest -m speculos tests/test_ledger_speculos.py -v || rc=$?
    cmd_stop "${models[@]}"
    return $rc
}

cmd_qeth() {
    local model=$1
    cmd_run "$model"
    QETH_LEDGER_SPECULOS=127.0.0.1:$(model_apdu_port "$model") uv run python -m qeth
}

cmd=${1:-}
shift || true
case $cmd in
    fetch) cmd_fetch ;;
    build-nanos) cmd_build_nanos ;;
    run) cmd_run "${1:?model}" ;;
    stop) cmd_stop "$@" ;;
    test) cmd_test "$@" ;;
    qeth) cmd_qeth "${1:?model}" ;;
    *) sed -n '2,/^set -euo/p' "$0" | sed '$d' | sed 's/^# \{0,1\}//'; exit 2 ;;
esac
