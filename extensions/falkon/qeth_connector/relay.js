// qeth connector — SafeJsWorld relay.
//
// Runs in Falkon's privileged "SafeJsWorld" (ApplicationWorld), where
// Falkon's own QWebChannel client has exposed our native bridge as
// `window.external.extra.qeth` (Falkon maps every registered "qz_<id>"
// extra object to `external.extra[<id>]`). This script bridges that
// native object to the page-world provider using window.postMessage,
// which crosses JS worlds (shared DOM) and is not governed by the
// page's Content-Security-Policy — so a strict-CSP dapp can still reach
// the qeth wallet.
//
//   provider.js (MainWorld)  ⇄ postMessage ⇄  relay.js (SafeJsWorld)
//                                                   ⇄ external.extra.qeth (QWebChannel)
//                                                   ⇄ QethBridge (Python) ⇄ qeth
//
// One native bridge object is shared across all frames, so every call
// carries a per-frame connection id; the relay filters replies to its
// own id and forwards the frame's real origin for qeth's per-origin
// chain tracking.

(function () {
  "use strict";
  if (window.__qethRelayInstalled) return;
  window.__qethRelayInstalled = true;

  var PROVIDER_SRC = "qeth-provider";
  var RELAY_SRC = "qeth-relay";

  function cidGen() {
    if (window.crypto && window.crypto.randomUUID) return window.crypto.randomUUID();
    return "qeth-" + Date.now() + "-" + Math.floor(Math.random() * 1e9);
  }

  // Idempotency guard. window.external becomes available asynchronously,
  // so BOTH the `_falkon_external_created` event and the interval poll
  // can fire — without this, hook() would install a second message
  // listener and every request would be forwarded to the bridge twice
  // (two eth_sendTransaction dialogs, etc.).
  var hooked = false;

  function hook(bridge) {
    if (hooked) return;
    if (!bridge || typeof bridge.send !== "function" || !bridge.message) return;
    hooked = true;
    var cid = cidGen();

    function toProvider(kind, data) {
      window.postMessage({ source: RELAY_SRC, kind: kind, data: data }, "*");
    }

    // Native → page: only our frame's replies.
    bridge.message.connect(function (msgCid, text) {
      if (msgCid === cid) toProvider("data", text);
    });

    // Page → native.
    window.addEventListener("message", function (e) {
      if (e.source !== window) return;
      var d = e.data;
      if (!d || d.source !== PROVIDER_SRC) return;
      if (d.kind === "data") {
        bridge.send(cid, window.location.origin || "", String(d.data));
      } else if (d.kind === "hello") {
        toProvider("ready");      // answer late-arriving providers
      }
    });

    // Announce readiness for providers already listening.
    toProvider("ready");
  }

  function tryHook() {
    if (hooked) return true;
    var ext = window.external;
    if (ext && ext.extra && ext.extra.qeth) { hook(ext.extra.qeth); return true; }
    return false;
  }

  if (!tryHook()) {
    // Falkon sets up window.external asynchronously (its own web-channel
    // callback), firing this event on the document when ready. Remove
    // the listener once hooked so it can't re-fire.
    var onCreated = function () {
      if (tryHook()) document.removeEventListener("_falkon_external_created", onCreated);
    };
    document.addEventListener("_falkon_external_created", onCreated);
    // Belt-and-suspenders: poll in case the event was missed (e.g.
    // external created between our checks). The `hooked` guard makes
    // this safe even if it races the event above.
    var tries = 0;
    var iv = setInterval(function () {
      if (tryHook() || ++tries > 40) clearInterval(iv);
    }, 100);
  }
})();
