# besseleth Clipper

A tiny Chrome/Edge/Brave extension: select text on any page — a LinkedIn
post, a tweet, a Bluesky post, a Luma event, anything — and a "+
besseleth" button appears. Click it and the text (plus the page's URL)
goes straight to your besseleth dashboard's Paste tab, auto-classified
the same way the dashboard does it. No copy-paste round trip.

Works on any site, not just LinkedIn/X — it doesn't hook site-specific
page structure (which breaks every time a site redesigns), just "you
selected some text, here's a button."

## Install (unpacked — not published to a store)

1. Make sure besseleth's dashboard is running: `python -m besseleth.web.app`
   (defaults to `http://127.0.0.1:5050` — the extension talks to this).
2. Open `chrome://extensions` (or `edge://extensions`, `brave://extensions`).
3. Turn on **Developer mode** (top-right toggle).
4. Click **Load unpacked**, and select this `extension/` folder.
5. Pin it (puzzle-piece icon in the toolbar → pin besseleth Clipper) so
   the popup's settings are easy to reach.

## Use

- **Select text on any page** → a small "+ besseleth" button appears
  right below the selection → click it. A toast confirms what it was
  added as ("Added as LinkedIn: ...") or tells you what went wrong.
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
