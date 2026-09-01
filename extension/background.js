// Service worker: does the actual fetch() to besseleth (a background/
// extension-page fetch to an origin covered by host_permissions bypasses
// CORS entirely — unlike a fetch from the content script, which runs in
// the target page's own origin and would be blocked by the page's CSP).
// Keeping this here also means the server URL is read from storage in
// one place, not duplicated between content.js and popup.js.

const DEFAULT_SERVER_URL = "http://127.0.0.1:5050";

async function getServerUrl() {
  const { serverUrl } = await chrome.storage.local.get("serverUrl");
  return (serverUrl || DEFAULT_SERVER_URL).replace(/\/+$/, "");
}

async function clipToBesseleth({ text, url, title }) {
  const serverUrl = await getServerUrl();
  const body = title && !text.startsWith(title) ? `${title}\n${text}` : text;
  try {
    const resp = await fetch(`${serverUrl}/api/paste`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text: body, url: url || "" }),
    });
    if (!resp.ok) {
      const errText = await resp.text().catch(() => "");
      return { ok: false, message: `besseleth returned ${resp.status}. ${errText.slice(0, 200)}` };
    }
    const data = await resp.json();
    if (!data.ok) return { ok: false, message: data.message || "besseleth rejected the paste." };
    return { ok: true, title: data.title, detectedAs: data.detected_as };
  } catch (e) {
    return {
      ok: false,
      message: `Couldn't reach besseleth at ${serverUrl} (${e.message}). Is \`python -m besseleth.web.app\` running? Check the server URL in the extension popup's settings.`,
    };
  }
}

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (msg.type === "clip") {
    clipToBesseleth(msg.payload).then(sendResponse);
    return true; // keep the message channel open for the async response
  }
  if (msg.type === "getServerUrl") {
    getServerUrl().then((serverUrl) => sendResponse({ serverUrl }));
    return true;
  }
});

// Right-click context menu on a selection, as an alternative to the
// floating button content.js shows — some users prefer it, and it works
// even on pages where the floating button gets hidden by page CSS.
chrome.runtime.onInstalled.addListener(() => {
  chrome.contextMenus.create({
    id: "besseleth-clip-selection",
    title: 'Add selection to besseleth',
    contexts: ["selection"],
  });
});

chrome.contextMenus.onClicked.addListener(async (info, tab) => {
  if (info.menuItemId !== "besseleth-clip-selection" || !info.selectionText) return;
  const result = await clipToBesseleth({ text: info.selectionText, url: tab.url, title: tab.title });
  chrome.tabs.sendMessage(tab.id, { type: "clip-result", result }).catch(() => {});
});
