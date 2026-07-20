// qeth extension — background (Chrome service worker / Firefox event page).
//
// Holds ONE WebSocket to the qeth wallet (ws://127.0.0.1:1248) and multiplexes
// every tab and frame over it. A page cannot open an insecure loopback ws
// (mixed content); the extension background can, because the manifest CSP
// whitelists connect-src ws://127.0.0.1:1248.
//
// Per-frame relay Ports connect here. For each JSON-RPC request the background
// remaps the (frame-local, collision-prone) id to a global wsId, stamps the
// dapp's origin as __frameOrigin (from the unforgeable port.sender), and sends
// it on. Responses restore the original id and go back to the exact port;
// server-pushed eth_subscription notifications are demultiplexed to the frame
// that subscribed. All state is rebuilt from scratch on a service-worker cold
// start, so nothing here relies on the worker staying alive.

"use strict";

var WS_URL = "ws://127.0.0.1:1248?identity=qeth-extension";
var RECONNECT_MS = 5000;
var KEEPALIVE_MIN = 0.5;          // chrome.alarms minimum; Firefox clamps to ~1m
var KEEPALIVE_TIMEOUT_MS = 10000; // no reply → assume a half-dead socket
var GRACE_MS = 2000;              // queue requests this long while connecting

var ws = null;
var wsOpen = false;
var connecting = false;
var nextId = 1;
var ports = new Set();
var pending = {};                 // wsId -> {port|null, originalId, method, subArg}
var subs = {};                    // subscriptionId -> {port}
var outbox = [];                  // {wsId, text, timer} queued while not open
var keepaliveTimer = null;

// --- icon -----------------------------------------------------------
function setIcon(connected) {
  var suffix = connected ? "" : "-off";
  var path = {};
  [16, 32, 48, 128].forEach(function (n) {
    path[n] = "icons/icon" + n + suffix + ".png";
  });
  try { chrome.action.setIcon({ path: path }); } catch (e) { /* no action api */ }
}

// --- WebSocket ------------------------------------------------------
function connect() {
  if (wsOpen || connecting) return;
  connecting = true;
  try { ws = new WebSocket(WS_URL); }
  catch (e) { connecting = false; scheduleReconnect(); return; }

  ws.onopen = function () {
    connecting = false; wsOpen = true;
    setIcon(true);
    flushOutbox();
    broadcast({ type: "event", event: "connect" });
  };
  ws.onmessage = function (ev) { onWsMessage(ev.data); };
  ws.onerror = function () { try { ws.close(); } catch (e) {} };
  ws.onclose = function () {
    connecting = false; wsOpen = false; ws = null;
    setIcon(false);
    failAllPending(4900, "qeth disconnected");
    subs = {};
    broadcast({ type: "event", event: "close" });
    scheduleReconnect();
  };
}

function scheduleReconnect() {
  setTimeout(function () { if (!wsOpen) connect(); }, RECONNECT_MS);
}

function wsSend(wsId, text) {
  if (wsOpen && ws) { ws.send(text); return; }
  // Startup grace: hold briefly while the socket comes up, then fail (4900).
  var item = { wsId: wsId, text: text, timer: null };
  item.timer = setTimeout(function () {
    var i = outbox.indexOf(item);
    if (i >= 0) { outbox.splice(i, 1); failPending(wsId, 4900, "qeth unreachable"); }
  }, GRACE_MS);
  outbox.push(item);
  connect();
}

function flushOutbox() {
  var q = outbox; outbox = [];
  q.forEach(function (item) {
    if (item.timer) clearTimeout(item.timer);
    if (ws) ws.send(item.text);
  });
}

// --- ports (relay connections) --------------------------------------
chrome.runtime.onConnect.addListener(function (port) {
  if (port.name !== "qeth") return;
  ports.add(port);
  // Tell the new frame the current wallet state right away.
  port.postMessage({ type: "event", event: wsOpen ? "connect" : "close" });
  connect();
  port.onMessage.addListener(function (msg) { onPortMessage(port, msg); });
  port.onDisconnect.addListener(function () { onPortGone(port); });
});

function onPortMessage(port, msg) {
  if (!msg) return;
  if (msg.type === "ping") { connect(); return; }   // traffic keeps us awake
  if (msg.type !== "req" || !msg.payload) return;
  var payload = msg.payload;
  var wsId = nextId++;
  var method = payload.method;
  pending[wsId] = {
    port: port,
    originalId: payload.id,
    method: method,
    subArg: (method === "eth_subscribe" || method === "eth_unsubscribe")
      ? (payload.params && payload.params[0]) : undefined,
  };
  var out = {
    jsonrpc: "2.0", id: wsId, method: method, params: payload.params || [],
  };
  var origin = originOf(port);
  if (origin) out.__frameOrigin = origin;
  wsSend(wsId, JSON.stringify(out));
}

function onPortGone(port) {
  ports.delete(port);
  // Drop this frame's in-flight requests (page is gone; no reply needed).
  for (var wsId in pending) {
    if (pending[wsId] && pending[wsId].port === port) delete pending[wsId];
  }
  // Best-effort unsubscribe its server subscriptions and forget them.
  for (var sid in subs) {
    if (subs[sid] && subs[sid].port === port) {
      if (wsOpen && ws) {
        try {
          ws.send(JSON.stringify({ jsonrpc: "2.0", id: nextId++,
            method: "eth_unsubscribe", params: [sid] }));
        } catch (e) {}
      }
      delete subs[sid];
    }
  }
}

// port.sender.origin is unforgeable (set by the browser). Fall back to the
// url's origin; skip opaque ("null") origins — the request still goes,
// unlabelled, and the server treats it as origin-less.
function originOf(port) {
  var s = port.sender || {};
  var o = s.origin;
  if (!o && s.url) { try { o = new URL(s.url).origin; } catch (e) {} }
  if (!o || o === "null") return null;
  return o;
}

// --- WebSocket → ports ----------------------------------------------
function onWsMessage(text) {
  var msg;
  try { msg = JSON.parse(text); } catch (e) { return; }
  if (Array.isArray(msg)) { msg.forEach(routeOne); return; }
  routeOne(msg);
}

function routeOne(msg) {
  if (!msg) return;
  // Server push (no id) — demux to the subscribing frame.
  if (msg.method === "eth_subscription" && msg.params) {
    var sub = subs[msg.params.subscription];
    if (sub && sub.port) {
      try { sub.port.postMessage({ type: "push", payload: msg }); } catch (e) {}
    }
    return;
  }
  var rec = pending[msg.id];
  if (!rec) return;                 // keepalive with no record, or stale
  delete pending[msg.id];
  // Track/forget subscriptions so pushes can be routed / cleaned up.
  if (rec.method === "eth_subscribe" && msg.result && rec.port) {
    subs[msg.result] = { port: rec.port };
  } else if (rec.method === "eth_unsubscribe" && rec.subArg) {
    delete subs[rec.subArg];
  }
  if (rec.cb) { rec.cb(msg); return; }   // internal query (popup status)
  if (!rec.port) return;            // keepalive request — nothing to deliver
  var reply = { jsonrpc: "2.0", id: rec.originalId };
  if ("error" in msg) reply.error = msg.error; else reply.result = msg.result;
  try { rec.port.postMessage({ type: "res", payload: reply }); } catch (e) {}
}

// --- failure fan-out ------------------------------------------------
function failPending(wsId, code, message) {
  var rec = pending[wsId];
  if (!rec) return;
  delete pending[wsId];
  if (!rec.port) return;
  try {
    rec.port.postMessage({ type: "res", payload: {
      jsonrpc: "2.0", id: rec.originalId, error: { code: code, message: message } } });
  } catch (e) {}
}

function failAllPending(code, message) {
  var ids = Object.keys(pending);
  ids.forEach(function (wsId) { failPending(wsId, code, message); });
  outbox.forEach(function (item) { if (item.timer) clearTimeout(item.timer); });
  outbox = [];
}

function broadcast(msg) {
  ports.forEach(function (p) { try { p.postMessage(msg); } catch (e) {} });
}

// --- keepalive ------------------------------------------------------
// A 30s alarm sends eth_chainId (locally answered by qeth, side-effect-free —
// web3_clientVersion would be proxied upstream). On Chrome >=116 the WS traffic
// keeps the service worker alive while connected; a missing reply means a
// half-dead socket, so close and reconnect. Also revives us from cold start.
chrome.alarms.get("qeth-keepalive", function (a) {
  if (!a) chrome.alarms.create("qeth-keepalive", { periodInMinutes: KEEPALIVE_MIN });
});

chrome.alarms.onAlarm.addListener(function (alarm) {
  if (alarm.name !== "qeth-keepalive") return;
  if (!wsOpen) { connect(); return; }
  var wsId = nextId++;
  var rec = { port: null, originalId: null, method: "eth_chainId" };
  pending[wsId] = rec;              // port null → routeOne drops the reply
  try { ws.send(JSON.stringify({ jsonrpc: "2.0", id: wsId, method: "eth_chainId" })); }
  catch (e) { try { ws.close(); } catch (e2) {} return; }
  setTimeout(function () {
    // A reply deletes pending[wsId] in routeOne; if it's still ours the
    // socket is wedged — force a reconnect.
    if (pending[wsId] === rec) { delete pending[wsId]; try { ws.close(); } catch (e) {} }
  }, KEEPALIVE_TIMEOUT_MS);
});

// --- popup status query ---------------------------------------------
// The popup asks "is qeth reachable, on which chain, as which account?".
// eth_chainId / eth_accounts are sent origin-less (no __frameOrigin), so the
// server answers for the wallet's default chain — the Falkon StatusDialog
// semantics. Opening the popup also nudges a reconnect.
function askLocal(method, cb) {
  if (!wsOpen || !ws) { cb(null); return; }
  var wsId = nextId++;
  pending[wsId] = { port: null, originalId: null, method: method, cb: cb };
  try { ws.send(JSON.stringify({ jsonrpc: "2.0", id: wsId, method: method })); }
  catch (e) { delete pending[wsId]; cb(null); }
}

function queryStatus(sendResponse) {
  var res = { connected: true, chainId: null, account: null };
  var left = 2, done = false;
  function finish() { if (!done) { done = true; sendResponse(res); } }
  var t = setTimeout(finish, 2000);
  function got() { if (--left <= 0) { clearTimeout(t); finish(); } }
  askLocal("eth_chainId", function (m) {
    if (m && "result" in m) res.chainId = m.result; got();
  });
  askLocal("eth_accounts", function (m) {
    if (m && m.result && m.result[0]) res.account = m.result[0]; got();
  });
}

chrome.runtime.onMessage.addListener(function (msg, sender, sendResponse) {
  if (!msg || msg.type !== "status") return false;
  connect();
  var deadline = Date.now() + 1500;         // give a just-started socket a moment
  (function attempt() {
    if (wsOpen) { queryStatus(sendResponse); return; }
    if (Date.now() >= deadline) { sendResponse({ connected: false }); return; }
    setTimeout(attempt, 150);
  })();
  return true;                              // async sendResponse
});

chrome.runtime.onStartup.addListener(connect);
chrome.runtime.onInstalled.addListener(connect);

setIcon(false);
connect();
