import { marked } from 'marked';
import wixLocation from 'wix-location';

const API_URL = 'https://offthebar-api-607277832880.asia-south1.run.app/api/chat';
const WARM_URL = 'https://offthebar-api-607277832880.asia-south1.run.app/api/warm';

const USER_AVATAR = 'https://static.wixstatic.com/media/5dd29f_8946a06cf5544c1f99de458788f5b424~mv2.png';
const BOT_AVATAR  = 'https://static.wixstatic.com/media/5dd29f_d03e44b4f7f1481ab5c12d684830a18b~mv2.jpg';

// ------- UI labels -------
const SEND_READY_LABEL = '➤ Send';
const SEND_BUSY_LABEL  = '⏸ Generating…';

// The backend streams a "## HALFTIME!!! <status>" line at each stage
// ("Searching The Run of Play", "Searching the web", "Generating"); only the
// latest status shows, while nothing follows it (see renderBot).
const HALFTIME_LINES = /^##\s*HALFTIME!!!\s*(.*)$/gm;
// After a finished answer, the Run of Play articles it drew on, as
// "## SOURCES!!! [{"title", "url"}]" - shown as links at the end, never web pages.
const SOURCES_LINES = /^##\s*SOURCES!!!\s*(.*)$/gm;
const RUN_OF_PLAY_SITE = 'https://www.therunofplay.com/';
const FIRST_STATUS = 'Searching The Run of Play';   // shown before the first chunk arrives
const DEFAULT_STATUS = 'Generating';
// Cloud Run's answer when every instance is full (--max-instances, see CLOUDRUN.md).
const BUSY_STATUSES = [429, 503];
const BUSY_MESSAGE = "*The dugout's packed right now. Give it a moment and ask again.*";
const CUT_OFF_MESSAGE = '*The connection dropped before the rest arrived. Ask again for the full answer.*';

// Full answers end with an "Ask next:" line and a short list of follow-up
// prompts (the backend's prompt asks for exactly that). They're cut from the
// answer text and shown as buttons once the answer is done; clicking one
// sends it as the next question.
const FOLLOW_UPS_LINE = /^[ \t]*(?:\*\*|__)?Ask next:?(?:\*\*|__)?[ \t]*$/im;
const FOLLOW_UP_ITEM = /^\s*(?:[-*•]|\d+[.)])\s+(.+?)\s*$/;
const MAX_FOLLOW_UPS = 3;

// Fallback for a chat iframe that never answers the 'ping' handshake (an older embed).
const CHAT_READY_FALLBACK_MS = 3000;

let conversation = [];          // prior turns only - never the message being sent
let conversationId = newConversationId();
let streaming = false;

// ------- Streaming handles for Stop -------
let abortCtrl = null;           // AbortController for fetch
let currentReader = null;       // ReadableStream reader
let lastStreamText = '';        // accumulated text so far
let requestId = 0;              // bumped on Stop, so callbacks from a stopped stream bail out

// ------- Smooth reveal -------
// Gemini streams in bursts of a few hundred characters (about four per answer
// phase, within half a second), so received text is revealed at a steady pace,
// a word at a time, catching up faster the further behind it is - instead of
// jumping in paragraph-sized blocks.
const REVEAL_TICK_MS = 40;
const REVEAL_MIN_CHARS = 3;      // per tick, when nearly caught up
const REVEAL_CATCH_UP = 8;       // per tick, reveal at least 1/8 of the backlog
let shownChars = 0;             // how much of the normalized stream text is on screen
let revealTimer = null;
let streamDone = false;         // the response has fully arrived; finish once revealed

// ------- Chat iframe handshake -------
// Messages posted before the chat iframe has loaded can be lost, so they're
// queued until it reports 'iframe-ready'.
let chatReady = false;
let chatQueue = [];

// ------- Helpers -------
function postToHtml(payload) {
  if (!chatReady) { chatQueue.push(payload); return; }
  try { $w('#chatHtml').postMessage(payload); } catch (_) {}
}

const postToInput = (payload) => { try { $w('#inputHtml').postMessage(payload); } catch (_) {} };

function markChatReady() {
  if (chatReady) return;
  chatReady = true;
  const queued = chatQueue;
  chatQueue = [];
  queued.forEach(postToHtml);
}

function newConversationId() {
  return `wix-${Date.now().toString(16)}-${Math.random().toString(16).slice(2, 10)}`;
}

function normalizeEscapes(s) {
  return (s || '')
    .replace(/\\r\\n/g, '\n')
    .replace(/\\n/g, '\n')
    .replace(/\\t/g, '    ');
}

// marked doesn't sanitize HTML, and answers can quote scraped web pages -
// escaping "<" makes any raw HTML render as text instead of running in the iframe.
function md(text) {
  return marked.parse((text || '').replace(/</g, '&lt;'), { gfm: true, breaks: false });
}

function escapeHtml(s) {
  return (s || '')
    .replace(/[&<>"']/g, m => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#039;' }[m]))
    .replace(/\r?\n/g, '<br>');
}

// The latest status, while nothing follows its line yet; else null.
function currentStatus(text) {
  const matches = [...text.matchAll(HALFTIME_LINES)];
  const last = matches[matches.length - 1];
  if (!last || text.slice(last.index + last[0].length).trim()) return null;
  return last[1].trim() || DEFAULT_STATUS;
}

function statusHtml(status) {
  return `<div class="otb-status" role="status">${escapeHtml(status)}</div>`;
}

function splitFollowUps(text) {
  const match = text.match(FOLLOW_UPS_LINE);
  if (!match) return { body: text, followUps: [] };
  const followUps = text.slice(match.index + match[0].length)
    .split('\n')
    .map(line => line.match(FOLLOW_UP_ITEM))
    .filter(Boolean)
    .map(m => m[1].replace(/\*\*/g, '').replace(/^["“]+|["”]+$/g, '').trim())
    .filter(Boolean)
    .slice(0, MAX_FOLLOW_UPS);
  return { body: text.slice(0, match.index).trimEnd(), followUps };
}

// While streaming, a last line that could still grow into "Ask next:" stays
// hidden, so the label never flashes up before it's cut.
function hidePartialFollowUpsLine(text) {
  const start = text.lastIndexOf('\n') + 1;
  const tail = text.slice(start).replace(/[*_\s]/g, '').toLowerCase();
  return tail && 'asknext:'.startsWith(tail) ? text.slice(0, start) : text;
}

function followUpButtons(followUps) {
  const buttons = followUps
    .map(q => `<button type="button" class="otb-suggestion" data-q="${escapeHtml(q)}">${escapeHtml(q)}</button>`)
    .join('');
  return `<div class="otb-suggestions">${buttons}</div>`;
}

// Same as client/js/chat.js: the current status shows (under the draft, if
// there is one) as a quiet shimmer line with a live seconds counter
// (.otb-status in chat-iframe.html), and goes away once text arrives or the
// answer ends. Follow-up buttons only appear on the final render.
function sourcesHtml(text) {
  const line = [...text.matchAll(SOURCES_LINES)].pop();
  if (!line) return '';
  let sources = [];
  try { sources = JSON.parse(line[1]); } catch (_) { return ''; }
  const links = (Array.isArray(sources) ? sources : [])
    .filter(s => s && typeof s.url === 'string' && s.url.startsWith(RUN_OF_PLAY_SITE))
    .map(s => `<a class="otb-source" href="${escapeHtml(s.url)}" target="_blank" rel="noopener">${escapeHtml(s.title || s.url)}</a>`)
    .join('');
  return links ? `<div class="otb-sources"><div class="otb-sources-label">From The Run of Play</div>${links}</div>` : '';
}

function renderBot(text, final = false) {
  const status = currentStatus(text);
  let { body, followUps } = splitFollowUps(stripHalftime(text));
  if (!final) body = hidePartialFollowUpsLine(body);
  let html = md(body);
  if (status) html += statusHtml(status);
  if (final && followUps.length) html += followUpButtons(followUps);
  if (final) html += sourcesHtml(text);
  return html;
}

function stripHalftime(text) {
  return text.replace(HALFTIME_LINES, '').replace(SOURCES_LINES, '').replace(/\n{3,}/g, '\n\n').trim();
}

// Tell chat iframe to show/hide its Stop button
function setIframeStopVisible(show) {
  postToHtml({ type: 'set-stop-visible', visible: !!show });
}

// Button label compatibility: Button vs IconButton
function setSendBusy(busy) {
  const btn = $w('#sendBtn');
  const label = busy ? SEND_BUSY_LABEL : SEND_READY_LABEL;

  if ('label' in btn && btn.label !== undefined) btn.label = label;
  else if ('text' in btn && btn.text !== undefined) btn.text = label;

  // `enabled` is read-only in Velo - enable()/disable() are what toggle it
  if (typeof btn.disable === 'function') {
    if (busy) btn.disable();
    else btn.enable();
  }

  // Disable/enable the input iframe's textarea, and toggle Stop in the chat iframe
  postToInput({ type: 'set-disabled', disabled: busy });
  setIframeStopVisible(busy);
}

function sendIframeSetup() {
  postToHtml({ type: 'set-theme-vars', vars: { '--font': 'Google Sans, sans-serif' } });
  postToHtml({ type: 'set-avatars', user: USER_AVATAR, bot: BOT_AVATAR });
}

$w.onReady(() => {
  // Wake the backend while the visitor reads, so the first question finds it ready.
  fetch(WARM_URL, { cache: 'no-store' }).catch(() => {});

  // Messages FROM the chat iframe - registered first so its handshake isn't missed
  $w('#chatHtml').onMessage((event) => {
    const d = event.data || {};
    if (d.type === 'iframe-ready') {
      if (chatReady) sendIframeSetup();   // iframe reloaded - restore theme + avatars
      else markChatReady();
    }
    if (d.type === 'stop-click') stopStreaming();   // cancel stream, keep the partial answer
    if (d.type === 'suggestion-click' && !streaming) handleSend(d.value || '');
  });

  // If the iframe loaded before this handler existed, its load-time
  // 'iframe-ready' was missed - 'ping' makes it announce again.
  try { $w('#chatHtml').postMessage({ type: 'ping' }); } catch (_) {}
  setTimeout(markChatReady, CHAT_READY_FALLBACK_MS);

  sendIframeSetup();

  $w('#titleStack').onClick(() => {
    stopStreaming();
    conversation = [];
    conversationId = newConversationId();
    postToHtml({ type: 'clear-chat' });
    setSendBusy(false);
    wixLocation.to('/offthebar');
  });

  // Messages FROM the input iframe
  $w('#inputHtml').onMessage((event) => {
    const d = event.data || {};
    if ((d.type === 'send-intent' || d.type === 'value') && !streaming) {
      handleSend(d.value || '');
    }
  });

  // Click on Send — ask input iframe to force-send current value
  $w('#sendBtn').onClick(() => {
    if (!streaming) postToInput({ type: 'force-send' });
  });

  // Initialize
  setSendBusy(false);

  // ?msg=... deep link starts the conversation
  let initialMsg = ((wixLocation.query || {}).msg || '').trim();
  // Optional safety: prevent huge payloads
  // initialMsg = initialMsg.slice(0, 2000);
  if (initialMsg) handleSend(initialMsg);
});

function handleSend(raw) {
  const text = (raw || '').trim();
  if (!text) return;

  setSendBusy(true);
  postToHtml({ type: 'add-message', from: 'user', html: escapeHtml(text) });
  postToInput({ type: 'set-value', value: '' });   // clear + shrink the input
  sendToChatbot(text);
}

// ---------------- CHAT FLOW -------------
function sendToChatbot(userMessage) {
  const thisRequest = ++requestId;
  const isCurrent = () => thisRequest === requestId;

  streaming = true;
  lastStreamText = '';
  shownChars = 0;
  streamDone = false;

  // Same shape as client/js/chat.js: prior turns in `conversation`, the new
  // message only in `parts` (the backend adds it to the prompt itself).
  const payload = {
    jailbreak: true,                 // required key on the backend, value unused
    conversation_id: conversationId, // groups this chat's turns in the server-side logs
    stage_statuses: true,            // a status line per stage (see HALFTIME_LINES)
    sources: true,                   // the Run of Play articles used (see SOURCES_LINES)
    meta: {
      content: {
        conversation: conversation.map(m => ({ role: m.role, content: m.content })),
        parts: [{ content: userMessage }]
      }
    }
  };
  conversation.push({ role: 'user', content: userMessage });

  // Start the bot message with the first stage's status
  postToHtml({ type: 'add-message', from: 'bot', html: statusHtml(FIRST_STATUS) });

  abortCtrl = new AbortController();

  fetch(API_URL, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
    signal: abortCtrl.signal
  })
    .then(res => {
      if (BUSY_STATUSES.includes(res.status)) {
        if (isCurrent()) finishBusy();
        return;
      }
      if (!res.ok) throw new Error(`Bad response: ${res.status}`);
      const reader = res.body.getReader();
      const decoder = new TextDecoder('utf-8');
      currentReader = reader;

      const pump = () => {
        reader.read().then(({ done, value }) => {
          // reader.cancel() resolves the pending read with done=true, so after
          // Stop this would otherwise finalize a second, empty answer.
          if (!isCurrent()) return;

          if (done) {
            lastStreamText += decoder.decode();   // flush a trailing multi-byte character
            streamDone = true;                    // revealTick finishes once it's all shown
            startReveal();
            return;
          }

          lastStreamText += decoder.decode(value, { stream: true });
          startReveal();
          pump();
        }).catch(err => {
          if (!isCurrent()) return;
          console.error('Stream read error:', err);
          finishBot('*Something went wrong - please try again.*');
        });
      };

      pump();
    })
    .catch(err => {
      if (!isCurrent()) return;   // aborted by Stop, already finalized
      console.error('Fetch error:', err);
      // Cloud Run's own "full" reply has no CORS headers, so the browser
      // reports a failed fetch (a TypeError) instead of the 429.
      if (err instanceof TypeError) finishBusy();
      else finishBot('*Something went wrong - please try again.*');
    });
}

function finishBusy() {
  conversation.pop();   // never answered, so asking again doesn't repeat it in the history
  finishBot(BUSY_MESSAGE);
}

function startReveal() {
  if (!revealTimer) revealTimer = setInterval(revealTick, REVEAL_TICK_MS);
}

function stopReveal() {
  clearInterval(revealTimer);
  revealTimer = null;
}

function revealTick() {
  const text = normalizeEscapes(lastStreamText);
  if (shownChars >= text.length) {
    if (streamDone) finishBot('*No answer came back - please try again.*');
    else stopReveal();   // caught up - the next chunk restarts it
    return;
  }

  const backlog = text.length - shownChars;
  let next = shownChars + Math.max(REVEAL_MIN_CHARS, Math.ceil(backlog / REVEAL_CATCH_UP));
  // Whole words only: run on to the next whitespace.
  const gap = text.slice(next).search(/\s/);
  next = gap === -1 ? text.length : next + gap;
  // Status lines appear in one go, never as a half-typed "## HALF...".
  for (const status of [...text.matchAll(HALFTIME_LINES), ...text.matchAll(SOURCES_LINES)]) {
    const end = status.index + status[0].length;
    if (next > status.index && shownChars < end) next = Math.max(next, end);
  }

  shownChars = Math.min(next, text.length);
  const shown = text.slice(0, shownChars);
  postToHtml({ type: 'update-bot-html', html: renderBot(shown), cursor: !currentStatus(shown) });
}

// One place that ends a response: normal end, read error, fetch error and Stop
// (which passes showFollowUps=false - a stopped answer's list may be cut off).
function finishBot(fallbackMarkdown, showFollowUps = true) {
  stopReveal();
  const streamed = normalizeEscapes(lastStreamText);
  // Ending on a status line means the rest never came. Left in place, its
  // counter would tick on forever with the answer looking stuck.
  const cutOff = showFollowUps && !!currentStatus(streamed);
  if (cutOff) console.error('Stream ended on a status line', streamed.length);
  const rawText = streamed.replace(HALFTIME_LINES, '');
  const finalText = stripHalftime(rawText);
  const shown = cutOff ? `${rawText.trimEnd()}\n\n${CUT_OFF_MESSAGE}` : rawText;
  const html = finalText ? renderBot(shown, showFollowUps) : md(fallbackMarkdown);
  postToHtml({ type: 'finalize-bot', html });
  if (finalText) conversation.push({ role: 'model', content: finalText });

  streaming = false;
  setSendBusy(false);

  currentReader = null;
  abortCtrl = null;
  lastStreamText = '';
}

function stopStreaming() {
  if (!streaming) return;
  requestId++;   // pending read/fetch callbacks from this stream now bail out

  if (currentReader) currentReader.cancel('user-stopped').catch(() => {});
  try { if (abortCtrl) abortCtrl.abort(); } catch (_) {}

  // Keep what was on screen - text that arrived but wasn't revealed yet is dropped.
  lastStreamText = normalizeEscapes(lastStreamText).slice(0, shownChars);
  finishBot('*Generation stopped.*', false);
}
