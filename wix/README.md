# Wix client

The chat page on therunofplay.com is built in the Wix editor. These files are
the source of truth for its code: edit them here, check them in the local
preview, then paste them into the editor and publish. Nothing in this folder
goes live on its own.

| File | Where it goes in the Wix editor |
|---|---|
| `offthebar-page.js` | Page code of the Off The Bar page (`/offthebar`). Needs the `marked` npm package installed in the site. |
| `chat-iframe.html` | The HTML embed with ID `chatHtml` (the message list). |
| `input-iframe.html` | The HTML embed with ID `inputHtml` (the text box). |
| `preview/` | Local preview only - never pasted into Wix. |

The page code calls `/api/chat` on Cloud Run (see `../CLOUDRUN.md`) and streams
the answer into `chatHtml`. It sends the same request shape as
`../client/js/chat.js`: prior turns in `meta.content.conversation`, the new
message only in `meta.content.parts[0].content`, plus `conversation_id` and the
`jailbreak` key the backend requires.

## Local preview

Runs the real page code and both embeds, unmodified, in a page that stands in
for Wix (`$w`, `wix-location`, the `marked` import), so most changes can be
checked without pasting into Wix:

```bash
cd wix
python3 preview/serve.py
# then open http://localhost:8000/preview/
```

`serve.py` is `python3 -m http.server` with browser caching turned off, and
the preview loads the page code and both embeds under a new `?v=` URL every
time. Under the plain server, reloads kept running a cached, older
`offthebar-page.js` and `chat-iframe.html` (typing dots, a "HALFTIME!!!"
heading) against a newer backend.

Pick the backend from the toolbar:

- **mock** (default) - a canned two-phase answer with a pause between the
  phases, no API calls. Send a message containing `mock-busy` or `mock-unreachable` to test the "all instances full" message (the second is what a browser sees if the reply has no CORS headers), or `mock-error` to test the
  failure path, or `mock-nothing-new` for an answer that ends on the draft.
- **local** - your local backend at `http://localhost:1338` (`python -u run.py`
  from the repo root).
- **live** - the real Cloud Run API. Uses real Gemini quota.

"Last /api/chat request body" under the chat shows exactly what was sent,
which is the quickest way to check the history and `conversation_id`.

What the preview can't tell you: the real Wix layout (embed sizes, positions and
overlap), quirks of the Velo runtime itself (the element APIs are stand-ins),
and the exact `marked` version (the preview pins 12.0.2; check the Wix package
manager). So still do one pass in Wix's Preview before publishing.

## Answer rendering

The page code renders each answer from the backend's markdown stream, with two
pieces of it handled specially (the iframe only sees the resulting HTML).
Gemini sends text in bursts of a few hundred characters, so the page reveals
what has arrived a word at a time (every 40ms, faster the further behind it
is) instead of showing each burst at once. Stop keeps only what was on screen.

- `GET /api/warm` - sent once when the page loads, and its response is ignored.
  It wakes a sleeping backend while the visitor reads, and the backend holds
  it open until loading is done. A backend without this route answers 404,
  which does no harm, so the publish and the deploy can happen in either order.
- `## HALFTIME!!! <status>` - a status line the backend streams at each stage:
  `Searching The Run of Play` first, `Searching the web` while it waits for the
  web search, and `Generating` until the first words of an answer (or of the
  update after a draft). Only the latest status shows, while nothing follows
  it, as a grey shimmering `.otb-status` line with a live seconds counter
  (added by the iframe); every status line is stripped from the answer text.
  The page shows `Searching The Run of Play` itself as soon as a message is
  sent, so there's no blank wait before the first chunk. The page asks for
  these with `stage_statuses: true` in the request; without it (an older page
  or a tab opened before a publish) the backend sends only the one status line
  older pages handle - before the web update or the combined answer - so
  publishing the page code and redeploying the backend don't have to happen
  at the same moment.
- `## SOURCES!!! [{"title", "url"}]` - one line after a finished answer, for pages
  that send `sources: true`: the Run of Play articles it drew on. Shown as
  `.otb-source` links at the end of the answer, after the suggestions; anything
  not on www.therunofplay.com is dropped. Stripped from the answer text.
- `Ask next:` followed by a list - the follow-up prompts every full answer ends
  with (`prompt_templates.FOLLOW_UP_FORMAT`). They're cut from the text and,
  once the answer is done, shown as `.otb-suggestion` buttons. Only the latest
  answer's buttons are visible.

## Publishing a change

1. Page code: open the Off The Bar page's code panel and replace its whole
   contents with `offthebar-page.js`.
2. `chatHtml`: select the embed, **Edit Code**, and replace the code with
   `chat-iframe.html`.
3. `inputHtml`: same, with `input-iframe.html`.
4. In Wix's Preview, try a quick question (a grey shimmering "Searching The
   Run of Play" line with a seconds counter, then "Generating", then the
   answer), a news question ("Searching the web" and "Generating" under the
   draft, gone once the update arrives or the answer ends), a follow-up suggestion button (it should send as the next question), Stop in
   the middle of an answer (the partial answer should stay), and a `?msg=`
   link. Then publish.

Update the page code and `chatHtml` together: the page code waits for the
chat embed's `iframe-ready` handshake, and with an older embed that doesn't
answer `ping`, the first messages can be held back for up to 3 seconds.

## Messages between the page code and the embeds

Page code → `chatHtml`:

| `type` | Fields | Effect |
|---|---|---|
| `ping` | | Asks the iframe to send `iframe-ready` again |
| `set-theme-vars` | `vars` | Sets CSS custom properties on `:root` |
| `set-avatars` | `user`, `bot` | Avatar image URLs for messages added after this |
| `add-message` | `from` (`user`/`bot`), `html` | Appends a message; a bot message with empty `html` shows the "Searching The Run of Play" status |
| `update-bot-html` | `html`, `cursor` | Replaces the current bot message while it streams |
| `finalize-bot` | `html` | Final render of the current bot message (empty `html` is ignored) |
| `set-stop-visible` | `visible` | Shows/hides the Stop button |
| `clear-chat` | | Empties the message list |

`chatHtml` → page code: `iframe-ready` (on load, and in reply to `ping`),
`stop-click`, and `suggestion-click` (`value`, a follow-up suggestion the user
clicked - the page code sends it as the next question unless an answer is
still streaming). The page code queues everything it sends to `chatHtml` until the
first `iframe-ready` (or 3 seconds, whichever comes first), because messages
posted before the embed has loaded can be lost.

Page code → `inputHtml`: `set-disabled` (`disabled`), `set-value` (`value`),
`force-send`. The embed also accepts `focus` and `get-value`, which the page
code doesn't currently send.

`inputHtml` → page code: `send-intent` (`value`, the text to send - sent on
Enter or in reply to `force-send`), `value` (reply to `get-value`),
`input-ready`, and `input-height` (`height`, the embed's ideal height in px).
The page code ignores `input-ready` and `input-height`, so the Wix embed box
has to be tall enough for the textarea's largest size (about 100px), or its
top gets clipped.
