const DEFAULT_SERVER_URL = "http://127.0.0.1:5050";

const textEl = document.getElementById("text");
const urlEl = document.getElementById("url");
const statusEl = document.getElementById("status");
const serverUrlEl = document.getElementById("serverUrl");

async function init() {
  const { serverUrl } = await chrome.storage.local.get("serverUrl");
  serverUrlEl.value = serverUrl || DEFAULT_SERVER_URL;

  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  if (!tab?.id) return;
  urlEl.value = tab.url || "";

  try {
    const [{ result }] = await chrome.scripting.executeScript({
      target: { tabId: tab.id },
      func: () => window.getSelection()?.toString().trim() || "",
    });
    if (result) textEl.value = result;
  } catch (e) {
    // Some pages (chrome://, the Web Store, etc.) block script injection — fine, just leave it blank.
  }
}

document.getElementById("addBtn").onclick = async () => {
  const text = textEl.value.trim();
  if (!text) {
    statusEl.className = "err";
    statusEl.textContent = "Nothing to add — select text on the page or type something.";
    return;
  }
  statusEl.className = "";
  statusEl.textContent = "Adding…";
  const response = await chrome.runtime.sendMessage({
    type: "clip",
    payload: { text, url: urlEl.value.trim(), title: "" },
  });
  if (response.ok) {
    statusEl.className = "ok";
    statusEl.textContent = `Added as ${response.detectedAs}: "${response.title}"`;
    textEl.value = "";
  } else {
    statusEl.className = "err";
    statusEl.textContent = response.message;
  }
};

document.getElementById("saveBtn").onclick = async () => {
  let value = serverUrlEl.value.trim().replace(/\/+$/, "");
  if (!value) value = DEFAULT_SERVER_URL;
  if (!/^https?:\/\//i.test(value)) value = `http://${value}`;

  let origin;
  try {
    origin = new URL(value).origin + "/*";
  } catch (e) {
    statusEl.className = "err";
    statusEl.textContent = "That doesn't look like a valid URL.";
    return;
  }

  const isLocal = /^https?:\/\/(127\.0\.0\.1|localhost)(:\d+)?$/i.test(new URL(value).origin);
  if (!isLocal) {
    const granted = await chrome.permissions.request({ origins: [origin] });
    if (!granted) {
      statusEl.className = "err";
      statusEl.textContent = "Permission needed to reach that URL — not granted, settings not saved.";
      return;
    }
  }

  await chrome.storage.local.set({ serverUrl: value });
  statusEl.className = "ok";
  statusEl.textContent = `Saved. Using ${value}.`;
};

init();
