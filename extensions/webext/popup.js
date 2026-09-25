// qeth extension — status popup. Mirrors the Falkon connector's status dialog:
// is the wallet reachable, on which chain, as which account. Also the place a
// Firefox user grants host access (host_permissions are user-grantable there;
// without them the content scripts never inject and dapps can't see qeth).

"use strict";

var HOST_PERMS = { origins: ["http://*/*", "https://*/*"] };

var CHAIN_NAMES = {
  1: "Ethereum", 10: "Optimism", 56: "BNB Chain", 100: "Gnosis",
  137: "Polygon", 8453: "Base", 42161: "Arbitrum", 43114: "Avalanche",
};

function chainName(hexId) {
  var cid = parseInt(hexId, 16);
  if (isNaN(cid)) return String(hexId);
  return CHAIN_NAMES[cid] || ("Chain " + cid);
}

function $(id) { return document.getElementById(id); }

function clear(node) {
  while (node.firstChild) node.removeChild(node.firstChild);
}

// Build an element with plain text content — avoids innerHTML so untrusted
// values (the account address from the wallet) can never inject markup, and
// keeps AMO's addons-linter happy ("unsafe assignment to innerHTML").
function el(tag, text) {
  var e = document.createElement(tag);
  if (text != null) e.textContent = text;
  return e;
}

function text(s) { return document.createTextNode(s); }

function setVersion() {
  try { $("version").textContent = "qeth " + chrome.runtime.getManifest().version; }
  catch (e) {}
}

function networkName(chain) {
  return (chain && chain.name) || chainName(chain && chain.chainId);
}

// What to show (res.wallet = qeth_status): the active tab's site when it's
// connected — each network it obtained an account on, with that account —
// else the network selected in qeth and its account. Without qeth_status (an
// older qeth), the EVM chain dapps get and its account.
function view(res) {
  var wallet = res.wallet || {};
  var site = wallet.site;
  if (site && site.connections && site.connections.length) {
    var host = "";
    try { host = new URL(site.origin).host; } catch (e) {}
    return { host: host, shown: site.connections.map(function (c) {
      return { network: networkName(c.chain), account: c.account };
    }) };
  }
  if (wallet.chain) {
    return { host: null, shown: [{ network: networkName(wallet.chain),
                                   account: wallet.account }] };
  }
  return { host: null, shown: [{ network: chainName(res.chainId), account: res.account }] };
}

function showConnected(res) {
  var v = view(res);
  $("status").className = "status ok";
  $("status").textContent = "Connected to qeth";
  var detail = $("detail");
  clear(detail);
  if (v.host) {
    detail.appendChild(text("Site: "));
    detail.appendChild(el("b", v.host));
    detail.appendChild(document.createElement("br"));
  }
  v.shown.forEach(function (row, i) {
    if (i) detail.appendChild(document.createElement("br"));
    detail.appendChild(text("Network: "));
    detail.appendChild(el("b", row.network));
    detail.appendChild(document.createElement("br"));
    detail.appendChild(text("Account: "));
    if (row.account) {
      var addr = el("span", row.account);
      addr.className = "addr";
      detail.appendChild(addr);
    } else {
      detail.appendChild(text("No account selected in qeth"));
    }
  });
}

function showDisconnected() {
  $("status").className = "status off";
  $("status").textContent = "Not connected";
  var detail = $("detail");
  clear(detail);
  detail.appendChild(text(
    "The qeth wallet doesn't seem to be running. Start qeth — it serves the " +
    "connector on "));
  detail.appendChild(el("code", "127.0.0.1:1248"));
  detail.appendChild(text(" — then press Recheck."));
}

// The active tab's http(s) origin, or null — the site the popup describes.
function activeOrigin(cb) {
  if (!chrome.tabs || !chrome.tabs.query) { cb(null); return; }
  chrome.tabs.query({ active: true, currentWindow: true }, function (tabs) {
    var url = tabs && tabs[0] && tabs[0].url;      // host permissions expose it
    var origin = null;
    try {
      var u = new URL(url);
      if (u.protocol === "http:" || u.protocol === "https:") origin = u.origin;
    } catch (e) {}
    cb(origin);
  });
}

function probe() {
  $("status").className = "status off";
  $("status").textContent = "Checking…";
  $("detail").textContent = "";
  activeOrigin(function (origin) {
    chrome.runtime.sendMessage({ type: "status", origin: origin }, function (res) {
      if (chrome.runtime.lastError || !res || !res.connected) { showDisconnected(); return; }
      showConnected(res);
    });
  });
}

function checkPermissionsThenProbe() {
  // Chrome grants host_permissions at install; Firefox may not.
  if (!chrome.permissions || !chrome.permissions.contains) { probe(); return; }
  chrome.permissions.contains(HOST_PERMS, function (granted) {
    if (granted) { $("grant").style.display = "none"; probe(); }
    else {
      $("grant").style.display = "block";
      $("status").className = "status off";
      $("status").textContent = "Site access needed";
      $("detail").textContent = "";
    }
  });
}

document.addEventListener("DOMContentLoaded", function () {
  setVersion();
  $("recheck").addEventListener("click", checkPermissionsThenProbe);
  $("grant-btn").addEventListener("click", function () {
    // A popup button click is a valid user gesture for a permission request.
    chrome.permissions.request(HOST_PERMS, function (granted) {
      if (granted) { $("grant").style.display = "none"; probe(); }
    });
  });
  checkPermissionsThenProbe();
});
