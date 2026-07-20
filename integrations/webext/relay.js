// qeth extension — isolated-world relay (content script, document_start).
//
// Bridges the page-world provider (provider.js, MAIN world) to the extension
// background over a per-frame chrome.runtime Port. Runs in every frame; the
// Port is frame-specific in both directions and auto-cleans on navigation, so
// responses/pushes never leak across frames and ids can't collide.
//
//   provider.js  ── window.postMessage ──▶  relay.js  ── Port ──▶  background.js
//
// The provider's postMessage envelope is the same one the Falkon relay speaks
// (source: "qeth-provider"/"qeth-relay", kind: hello/ready/data/event), so the
// shared provider needs no transport-specific code.

(function () {
  "use strict";
  if (window.__qethRelayInstalled) return;
  window.__qethRelayInstalled = true;

  var PROVIDER_SRC = "qeth-provider";
  var RELAY_SRC = "qeth-relay";
  var PING_MS = 20000;          // keep the (Firefox) event page alive while busy

  var port = null;
  var outstanding = {};         // json-rpc id -> true (requests awaiting a reply)
  var outstandingCount = 0;
  var backoff = 500;            // reconnect backoff, doubles to 5s
  var closeReported = false;    // did we already surface a disconnect to the page?
  var failures = 0;             // consecutive port failures (quiet single restarts)
  var pingTimer = null;
  var dead = false;             // extension context invalidated (update/removed)

  // --- page (MAIN world) side -----------------------------------------
  function toPage(msg) { window.postMessage(msg, "*"); }

  window.addEventListener("message", function (e) {
    if (e.source !== window) return;
    var d = e.data;
    if (!d || d.source !== PROVIDER_SRC) return;
    if (d.kind === "hello") {
      toPage({ source: RELAY_SRC, kind: "ready" });
    } else if (d.kind === "data") {
      var payload;
      try { payload = JSON.parse(d.data); } catch (e2) { return; }
      if (payload && payload.id != null) {
        outstanding[payload.id] = true; outstandingCount++;
      }
      sendToBackground({ type: "req", payload: payload });
      updatePing();          // a request may pend a long time (signing prompt)
    }
  });

  // Announce the relay is present (order-independent with the provider's
  // hello — the provider handles "ready" idempotently).
  toPage({ source: RELAY_SRC, kind: "ready" });

  // --- background (Port) side -----------------------------------------
  function contextAlive() {
    try { return !!(chrome.runtime && chrome.runtime.id); }
    catch (e) { return false; }
  }

  function connectPort() {
    if (dead) return;
    if (!contextAlive()) { dead = true; return; }
    try {
      port = chrome.runtime.connect({ name: "qeth" });
    } catch (e) { port = null; scheduleReconnect(); return; }
    port.onMessage.addListener(onPortMessage);
    port.onDisconnect.addListener(onPortDisconnect);
    backoff = 500;
  }

  function sendToBackground(msg) {
    if (!port) { connectPort(); }
    if (!port) return;
    try { port.postMessage(msg); }
    catch (e) { /* port died between check and post; onDisconnect handles it */ }
  }

  function onPortMessage(msg) {
    if (!msg) return;
    failures = 0;
    if (msg.type === "res") {
      var p = msg.payload;
      if (p && p.id != null && outstanding[p.id]) {
        delete outstanding[p.id]; outstandingCount--;
      }
      toPage({ source: RELAY_SRC, kind: "data", data: JSON.stringify(p) });
    } else if (msg.type === "push") {
      toPage({ source: RELAY_SRC, kind: "data", data: JSON.stringify(msg.payload) });
    } else if (msg.type === "event") {
      if (msg.event === "close") { if (closeReported) return; closeReported = true; }
      else if (msg.event === "connect") { closeReported = false; }
      toPage({ source: RELAY_SRC, kind: "event", event: msg.event });
    }
    updatePing();
  }

  function onPortDisconnect() {
    port = null;
    if (!contextAlive()) { dead = true; stopPing(); return; }
    // Fail whatever was in flight so page promises reject instead of hanging.
    for (var id in outstanding) {
      if (Object.prototype.hasOwnProperty.call(outstanding, id)) {
        toPage({ source: RELAY_SRC, kind: "data", data: JSON.stringify({
          jsonrpc: "2.0", id: isNaN(id) ? id : Number(id),
          error: { code: 4900, message: "qeth disconnected" } }) });
      }
    }
    outstanding = {}; outstandingCount = 0;
    // A routine service-worker restart must not flap the page's `disconnect`.
    // Only surface a close after two consecutive failures.
    failures++;
    if (failures >= 2 && !closeReported) {
      closeReported = true;
      toPage({ source: RELAY_SRC, kind: "event", event: "close" });
    }
    scheduleReconnect();
  }

  function scheduleReconnect() {
    if (dead) return;
    setTimeout(function () {
      if (!port && !dead) connectPort();
    }, backoff);
    backoff = Math.min(backoff * 2, 5000);
  }

  // --- keepalive ping while requests are in flight --------------------
  // Port traffic resets the background's idle timer in both engines, so a
  // long-held signing prompt keeps the event page alive. Only ping while busy.
  function updatePing() {
    if (outstandingCount > 0) startPing(); else stopPing();
  }
  function startPing() {
    if (pingTimer || dead) return;
    pingTimer = setInterval(function () {
      if (outstandingCount > 0) sendToBackground({ type: "ping" });
      else stopPing();
    }, PING_MS);
  }
  function stopPing() {
    if (pingTimer) { clearInterval(pingTimer); pingTimer = null; }
  }

  connectPort();
})();
