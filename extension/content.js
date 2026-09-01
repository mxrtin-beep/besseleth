// Shows a small floating button near any text selection, on any page.
// Deliberately NOT hooking site-specific DOM (X/LinkedIn/etc. change
// their markup constantly) — "select text, click button" works
// everywhere and never breaks when a site redesigns.

let clipButton = null;
let toastEl = null;

function removeButton() {
  if (clipButton) {
    clipButton.remove();
    clipButton = null;
  }
}

function showToast(message, ok) {
  if (toastEl) toastEl.remove();
  toastEl = document.createElement("div");
  toastEl.className = "besseleth-toast" + (ok ? " besseleth-toast-ok" : " besseleth-toast-err");
  toastEl.textContent = message;
  document.body.appendChild(toastEl);
  setTimeout(() => {
    toastEl?.remove();
    toastEl = null;
  }, 4500);
}

function showButton(rect) {
  removeButton();
  clipButton = document.createElement("button");
  clipButton.className = "besseleth-clip-btn";
  clipButton.textContent = "+ besseleth";
  clipButton.style.top = `${window.scrollY + rect.bottom + 6}px`;
  clipButton.style.left = `${window.scrollX + rect.left}px`;

  clipButton.addEventListener("mousedown", (e) => e.preventDefault()); // don't clear the selection
  clipButton.addEventListener("click", async (e) => {
    e.preventDefault();
    e.stopPropagation();
    const text = window.getSelection()?.toString().trim();
    if (!text) return;
    clipButton.textContent = "Adding…";
    clipButton.disabled = true;
    const response = await chrome.runtime.sendMessage({
      type: "clip",
      payload: { text, url: location.href, title: document.title },
    });
    removeButton();
    if (response.ok) {
      showToast(`Added as ${response.detectedAs}: "${response.title}"`, true);
    } else {
      showToast(response.message, false);
    }
  });

  document.body.appendChild(clipButton);
}

document.addEventListener("mouseup", () => {
  // Let the browser finish updating the selection first.
  setTimeout(() => {
    const selection = window.getSelection();
    const text = selection?.toString().trim();
    if (!text || text.length < 3) {
      removeButton();
      return;
    }
    const range = selection.getRangeAt(0);
    const rect = range.getBoundingClientRect();
    if (rect.width === 0 && rect.height === 0) {
      removeButton();
      return;
    }
    showButton(rect);
  }, 0);
});

document.addEventListener("mousedown", (e) => {
  if (clipButton && e.target !== clipButton) removeButton();
});
document.addEventListener("scroll", removeButton, true);
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") removeButton();
});

chrome.runtime.onMessage.addListener((msg) => {
  if (msg.type === "clip-result") {
    if (msg.result.ok) {
      showToast(`Added as ${msg.result.detectedAs}: "${msg.result.title}"`, true);
    } else {
      showToast(msg.result.message, false);
    }
  }
});
