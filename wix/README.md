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
python3 -m http.server 8000
# then open http://localhost:8000/preview/
```

Pick the backend from the toolbar:

- **mock** (default) - a canned two-phase answer with a HALFTIME pause, no API
  calls. Send a message containing `mock-error` to test the failure path.
- **local** - your local backend at `http://localhost:1338` (`python -u run.py`
  from the repo root).
- **live** - the real Cloud Run API. Uses real Gemini quota.

"Last /api/chat request body" under the chat shows exactly what was sent,
which is the quickest way to check the history and `conversation_id`.

What the preview can't tell you: the real Wix layout (embed sizes, positions and
overlap), quirks of the Velo runtime itself (the element APIs are stand-ins),
and the exact `marked` version (the preview pins 12.0.2; check the Wix package
manager). So still do one pass in Wix's Preview before publishing.

## Publishing a change

1. Page code: open the Off The Bar page's code panel and replace its whole
   contents with `offthebar-page.js`.
2. `chatHtml`: select the embed, **Edit Code**, and replace the code with
   `chat-iframe.html`.
3. `inputHtml`: same, with `input-iframe.html`.
4. In Wix's Preview, try a quick question, a news question (the HALFTIME
   status should appear and then disappear when the update arrives), Stop in
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
| `add-message` | `from` (`user`/`bot`), `html` | Appends a message; a bot message with empty `html` shows typing dots |
| `update-bot-html` | `html`, `cursor` | Replaces the current bot message while it streams |
| `finalize-bot` | `html` | Final render of the current bot message (empty `html` is ignored) |
| `set-stop-visible` | `visible` | Shows/hides the Stop button |
| `clear-chat` | | Empties the message list |

`chatHtml` → page code: `iframe-ready` (on load, and in reply to `ping`) and
`stop-click`. The page code queues everything it sends to `chatHtml` until the
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
