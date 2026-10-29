# Wix client

The chat page on therunofplay.com is built in the Wix editor. These files are
the source of truth for its code: edit them here, then paste them into the
editor and publish. Nothing in this folder goes live on its own.

| File | Where it goes in the Wix editor |
|---|---|
| `offthebar-page.js` | Page code of the Off The Bar page (`/offthebar`). Needs the `marked` npm package installed in the site. |
| `chat-iframe.html` | The HTML embed with ID `chatHtml` (the message list). |
| not checked in yet | The HTML embed with ID `inputHtml` (the text box). |

The page code calls `/api/chat` on Cloud Run (see `../CLOUDRUN.md`) and streams
the answer into `chatHtml`. It sends the same request shape as
`../client/js/chat.js`: prior turns in `meta.content.conversation`, the new
message only in `meta.content.parts[0].content`, plus `conversation_id` and the
`jailbreak` key the backend requires.

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
`force-send`. `inputHtml` → page code: `input-ready`, and `send-intent` or
`value` (both carry `value`, the text to send).

## Before publishing a change

In the editor's Preview, try a quick question, a news question (the HALFTIME
status should appear and then disappear when the update arrives), Stop in the
middle of an answer (the partial answer should stay), and a `?msg=` link.
