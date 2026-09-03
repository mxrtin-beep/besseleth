// Two ways to clip, both landing on the same clipToBesseleth() call:
//
// 1. A per-post "B" button, injected onto individual posts on a short
//    list of known sites (X/Twitter, Bluesky, LinkedIn) — see
//    SITE_CONFIGS. This hooks each site's DOM structure, which WILL
//    occasionally break when a site redesigns — that's the tradeoff for
//    a per-post button vs. the selection method never breaking. Wrapped
//    defensively so a missed selector just skips that post rather than
//    throwing, and console.warns so a failure is visible in DevTools
//    instead of just "nothing happened."
//
// 2. A selection-based floating button that works on ANY page,
//    regardless of the list above — select text, a "+ besseleth"
//    button appears. This is the reliable fallback when a site isn't
//    in SITE_CONFIGS, or if a site's markup has drifted from the
//    selectors below.

const SITE_CONFIGS = [
  {
    name: "X",
    hostnames: ["twitter.com", "x.com"],
    // X occasionally A/B tests or drops data-testid attributes for some
    // users/rollouts — role="article" is a broader fallback for the
    // same element.
    postSelector: 'article[data-testid="tweet"], article[role="article"]',
    textSelector: '[data-testid="tweetText"]',
    linkSelector: 'a[href*="/status/"]',
    // Best-effort skip for replies shown inline in a timeline — X
    // renders a "Replying to @user" line as the post's own leading
    // text in that case. This is a text-content heuristic, not a
    // documented API, so it may not catch every case; tell me if it's
    // missing some or catching too many and I'll refine it.
    skipTextPattern: /^Replying to/i,
    // The "..." more-options button on each tweet — used to line our
    // button up at the same height instead of an arbitrary corner
    // offset. Also a best-effort selector; falls back to the corner
    // position below if X doesn't have this on a given tweet.
    anchorSelector: '[data-testid="caret"]',
  },
  {
    name: "Bluesky",
    hostnames: ["bsky.app"],
    postSelector: '[data-testid^="feedItem-"], [data-testid^="postThreadItem-"]',
    textSelector: '[data-testid="postText"]',
    linkSelector: 'a[href*="/post/"]',
  },
  {
    name: "LinkedIn",
    hostnames: ["linkedin.com"],
    // LinkedIn's feed classes are the least stable of the three — if
    // the per-post button stops appearing here, selection still works.
    // [data-urn*="urn:li:activity"] is a broader fallback: LinkedIn
    // stamps that attribute on the feed's post containers for its own
    // internal tracking/linking, which tends to survive a class-name
    // redesign that would break the others.
    postSelector: 'div.feed-shared-update-v2, div.occludable-update, [data-urn*="urn:li:activity"]',
    textSelector: ".feed-shared-update-v2__description, .update-components-text, .feed-shared-text",
    linkSelector: 'a[href*="/feed/update/"], a.app-aware-link[href*="/posts/"]',
  },
];

const activeSiteConfig = SITE_CONFIGS.find((c) => c.hostnames.some((h) => location.hostname.endsWith(h)));

let toastEl = null;

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

async function clip(text, url, btn) {
  if (btn) {
    btn.disabled = true;
    btn.classList.add("besseleth-post-btn-busy");
  }
  const response = await chrome.runtime.sendMessage({
    type: "clip",
    payload: { text, url, title: document.title },
  });
  if (response.ok) {
    if (btn) {
      btn.classList.remove("besseleth-post-btn-busy");
      btn.classList.add("besseleth-post-btn-done");
      btn.innerHTML = "&#10003;"; // checkmark
    }
    showToast(`Added as ${response.detectedAs}: "${response.title}"`, true);
  } else {
    if (btn) {
      btn.classList.remove("besseleth-post-btn-busy");
      btn.disabled = false;
    }
    showToast(response.message, false);
  }
}

// --- 1. Per-post buttons on known sites ---------------------------------

function isMainPost(postEl, config) {
  // Skip anything nested inside another matching post — a quote-tweet's
  // embedded original, a repost-with-comment's embed, etc. render as a
  // second matching element inside the outer one; only the outer post
  // (what's actually in your timeline) gets a button.
  if (postEl.parentElement?.closest(activeSiteConfig.postSelector)) return false;
  if (config.skipTextPattern && config.skipTextPattern.test(postEl.innerText.trim())) return false;
  return true;
}

function injectPostButton(postEl, config) {
  if (postEl.dataset.besselethInjected) return;
  if (!inPageUiEnabled) return; // don't mark injected — retry once re-enabled, see the storage.onChanged listener below
  postEl.dataset.besselethInjected = "1";

  if (!isMainPost(postEl, config)) return;

  try {
    if (getComputedStyle(postEl).position === "static") {
      postEl.style.position = "relative";
    }

    const btn = document.createElement("button");
    btn.className = "besseleth-post-btn";
    btn.type = "button";
    btn.title = "Add to Besseleth";
    btn.setAttribute("aria-label", "Add to Besseleth");
    btn.textContent = "B";

    // Line up with the post's own "..." button when we can find it,
    // rather than guessing a fixed corner offset — reads its actual
    // rendered position instead of assuming where it sits in the DOM,
    // so this doesn't depend on knowing the site's exact markup nesting.
    const anchorEl = config.anchorSelector ? postEl.querySelector(config.anchorSelector) : null;
    if (anchorEl) {
      const postRect = postEl.getBoundingClientRect();
      const anchorRect = anchorEl.getBoundingClientRect();
      // Shifted left by the button's own width (16px, see .besseleth-post-btn
      // in content.css) plus a wider gap (16px, up from the standard 6px) —
      // on X the "..." button sits right next to the Grok icon, and this
      // extra margin keeps ours from crowding it.
      btn.style.top = `${anchorRect.top - postRect.top + anchorRect.height / 2 - 8}px`;
      btn.style.right = `${postRect.right - anchorRect.left + 16 + 16}px`;
    }

    btn.addEventListener("click", (e) => {
      e.preventDefault();
      e.stopPropagation();
      const textEl = postEl.querySelector(config.textSelector);
      const text = (textEl ? textEl.innerText : postEl.innerText || "").trim().slice(0, 4000);
      if (!text) {
        showToast("Couldn't find text in this post — try selecting it manually instead.", false);
        return;
      }
      const linkEl = postEl.querySelector(config.linkSelector);
      const url = linkEl ? new URL(linkEl.getAttribute("href"), location.href).href : location.href;
      clip(text, url, btn);
    });

    postEl.appendChild(btn);
  } catch (e) {
    // A selector mismatch or unexpected DOM shape on this post — skip
    // it rather than breaking the page, but log it: a silent catch here
    // was exactly why "no button anywhere" was hard to diagnose from
    // the outside. Open DevTools console on the page to see this.
    console.warn("[besseleth] failed to inject button on a post:", e);
  }
}

function scanForPosts() {
  if (!activeSiteConfig) return;
  const posts = document.querySelectorAll(activeSiteConfig.postSelector);
  posts.forEach((postEl) => injectPostButton(postEl, activeSiteConfig));
  return posts.length;
}

// Single on/off switch for BOTH in-page mechanisms (the per-post button
// AND the selection-based floating button), live — flipping it in the
// popup takes effect in any already-open tab immediately via the
// storage.onChanged listener below, no reload needed. The popup's manual
// paste box is unaffected either way, since it isn't injected into the
// page.
let inPageUiEnabled = true; // optimistic default until the read below resolves
const inPageUiEnabledReady = chrome.storage.local.get("perPostButtonsEnabled").then(({ perPostButtonsEnabled }) => {
  inPageUiEnabled = perPostButtonsEnabled !== false;
});

chrome.storage.onChanged.addListener((changes, area) => {
  if (area !== "local" || !("perPostButtonsEnabled" in changes)) return;
  inPageUiEnabled = changes.perPostButtonsEnabled.newValue !== false;
  console.log(`[besseleth] In-page clipping ${inPageUiEnabled ? "enabled" : "disabled"} (live, no reload).`);
  if (inPageUiEnabled) {
    scanForPosts(); // re-injects into posts that were skipped while disabled
  } else {
    document.querySelectorAll(".besseleth-post-btn").forEach((el) => el.remove());
    // Un-mark every post so scanForPosts() re-injects them once re-enabled
    // instead of treating them as already handled.
    document.querySelectorAll("[data-besseleth-injected]").forEach((el) => delete el.dataset.besselethInjected);
    removeButton(); // clears the selection-based button if one's showing
  }
});

async function initPerPostButtons() {
  if (!activeSiteConfig) return;
  await inPageUiEnabledReady;

  console.log(`[besseleth] Clipper active on ${activeSiteConfig.name} — watching for posts matching "${activeSiteConfig.postSelector}".`);
  const firstScanCount = scanForPosts();
  console.log(
    `[besseleth] Initial scan found ${firstScanCount} post(s), injected ${document.querySelectorAll(".besseleth-post-btn").length} button(s). ` +
      (firstScanCount === 0
        ? "0 posts found usually means the page hadn't finished loading yet — it'll keep watching and catch them as they load. If it's still 0 after scrolling the feed, the site's selectors have likely changed."
        : "")
  );
  let scanScheduled = false;
  const observer = new MutationObserver(() => {
    if (scanScheduled) return;
    scanScheduled = true;
    setTimeout(() => {
      scanScheduled = false;
      scanForPosts();
    }, 400); // debounced — these feeds mutate constantly on scroll
  });
  observer.observe(document.body, { childList: true, subtree: true });
}

initPerPostButtons();

// --- 2. Selection-based floating button (works on any site) ------------

let clipButton = null;

function removeButton() {
  if (clipButton) {
    clipButton.remove();
    clipButton = null;
  }
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
    await clip(text, location.href, null);
    removeButton();
  });

  document.body.appendChild(clipButton);
}

document.addEventListener("mouseup", async (e) => {
  if (e.target?.closest?.(".besseleth-post-btn")) return;
  await inPageUiEnabledReady;
  if (!inPageUiEnabled) return;
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
