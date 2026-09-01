# besseleth Clipper

A tiny browser extension for adding things to besseleth without leaving
the page you found them on. Two ways to clip, both landing in the
dashboard's Paste tab, auto-classified the same way pasting there does:

- **A small "B" button on every main post**, on X/Twitter, Bluesky, and
  LinkedIn — appears in the top-right corner of each post, no selecting
  required (hover it for a tooltip). Only on original/main posts in your
  timeline — quote-tweets' embedded content and (on X) replies are
  skipped, so you don't get a button on things that aren't really their
  own post. Toggle this off per-browser in the popup's Settings if you'd
  rather not see it at all and just use selection-based clipping. This
  hooks each site's page structure, which *can* break when a site
  redesigns (X, LinkedIn, and Bluesky all change their internal markup
  periodically) — if buttons stop appearing on a site, that's why; tell
  me and I'll update the selectors in `content.js`'s `SITE_CONFIGS`.
- **Select any text on any page** → a small "+ besseleth" button appears
  near the selection. Works everywhere, including sites not in the list
  above, and never breaks from a redesign since it doesn't depend on any
  site-specific structure — this is the reliable fallback.

Same extension source works on Chrome/Edge/Brave and Safari — Safari's
install path is just different (a native-app wrapper, not "load
unpacked"), see below.

## Install on Safari (macOS)

Requires **Safari 16.4+ on macOS 13.3 (Ventura) or later** — that's when
Safari added Manifest V3 support (background service workers,
`host_permissions`, etc.), which this extension uses. Older Safari can't
run it at all.

Safari doesn't load a raw extension folder like Chromium browsers do —
Apple requires web extensions to be wrapped in a native app, built
through Xcode. This is a one-time setup per machine:

1. **Install Xcode** (free, from the Mac App Store — it's large, budget
   some time/disk). Command Line Tools alone aren't enough; you need
   Xcode itself for the next step's `xcrun` to work fully and to build.
2. Make sure besseleth's dashboard is running:
   `python -m besseleth.web.app` (defaults to `http://127.0.0.1:5050`).
3. In Terminal, from the besseleth repo root:
   ```bash
   xcrun safari-web-extension-converter extension --macos-only --no-open
   ```
   This generates a new Xcode project (in a sibling folder, named
   something like `besseleth Clipper`) that wraps `extension/` as a
   Safari Web Extension. Drop `--no-open` if you'd rather it open Xcode
   for you immediately.
4. Open the generated `.xcodeproj` in Xcode, pick **My Mac** as the run
   destination (top toolbar), and press **▶ Run**. This builds and
   installs a small placeholder app plus its Safari extension — the app
   itself does nothing; it just carries the extension.
5. In Safari: **Safari menu → Settings → Extensions tab**, and turn on
   **besseleth Clipper**.
6. Since this is an unsigned development build, Safari also needs
   **Develop menu → Allow Unsigned Extensions** checked (enable the
   Develop menu first, if it's not in your menu bar: **Safari →
   Settings → Advanced/Developer → Show features for web developers**,
   or on newer Safari versions there's a **Developer** tab directly in
   Settings).
7. Still in **Safari → Settings → Extensions → besseleth Clipper**,
   grant it site access — Safari manages this per-site itself rather
   than a runtime pop-up like Chrome's, so check/allow `127.0.0.1` and
   `localhost` (or "Allow on All Websites" if that's easier for you).

**After editing extension source** (if you ever tweak the JS yourself):
re-run the `safari-web-extension-converter` command with `-f` to
overwrite the existing project, or manually copy the changed files into
the generated Xcode project's Resources folder — then **Run** again in
Xcode. A plain file edit alone doesn't update the installed extension
the way reloading works in Chrome.

**What I can't verify from here**: I don't have macOS/Xcode/Safari
available to actually run this conversion and click through it myself,
so this is written from how Safari's extension platform is documented to
work, not something I've watched succeed end-to-end. The extension code
itself uses standard WebExtensions APIs (`chrome.*`, which Safari
supports as an alias) that should carry over, but if something in Safari
specifically doesn't behave — the permission prompt, the popup, whatever
— tell me what you're seeing and I'll fix it.

## Install on Chrome/Edge/Brave (unpacked — not published to a store)

1. Make sure besseleth's dashboard is running: `python -m besseleth.web.app`
   (defaults to `http://127.0.0.1:5050` — the extension talks to this).
2. Open `chrome://extensions` (or `edge://extensions`, `brave://extensions`).
3. Turn on **Developer mode** (top-right toggle).
4. Click **Load unpacked**, and select this `extension/` folder.
5. Pin it (puzzle-piece icon in the toolbar → pin besseleth Clipper) so
   the popup's settings are easy to reach.

## Use

- **On X, Bluesky, or LinkedIn** → each main post gets a small navy "B"
  button in its top-right corner (hover for the "Add to Besseleth"
  tooltip). Click it — no selecting needed. It turns into a green
  checkmark once done, and a toast confirms what it was classified as.
- **Select text on any page** (these three sites included, or anywhere
  else) → a small "+ besseleth" button appears right below the selection
  → click it. Same toast confirmation.
- **Right-click a selection** → "Add selection to besseleth" also works,
  if you prefer the context menu to the floating button.
- **Extension icon → popup** → a full quick-add form (pre-filled with
  whatever's selected on the current tab and the tab's URL), for when
  you want to edit before adding, or add something with nothing selected.

## Settings

Click the extension icon → **Settings** → set the besseleth server URL
(default `http://127.0.0.1:5050`, i.e. the dashboard running on your own
machine). `127.0.0.1`/`localhost` work with no extra prompt. Pointing it
at besseleth running elsewhere on your network (say, on a home server)
will ask for a one-time permission grant for that host — Chrome requires
this explicitly for any origin outside the extension's default
`host_permissions`.

## Why a background-script fetch, not a content-script one

The actual `fetch()` to besseleth happens in `background.js` (the
extension's service worker), not in `content.js` (which runs inside the
page you're clipping from). A content-script fetch would be subject to
*that page's* CORS/CSP rules — X or LinkedIn's, not besseleth's — and
could be blocked. A background-script fetch to an origin covered by
`host_permissions` bypasses CORS entirely, which is the whole reason
Chrome extensions have `host_permissions` as a concept. No changes were
needed on besseleth's server side for this to work.

## Privacy

This extension does exactly one thing: sends text you explicitly select
(or type into the popup) to the besseleth server URL you configure. It
doesn't run in the background scanning pages, doesn't track browsing,
and doesn't talk to anything except that one URL.
