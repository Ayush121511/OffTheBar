import { marked } from 'marked';
import wixLocation from 'wix-location';

const API_URL = 'https://offthebar-api-607277832880.asia-south1.run.app/api/chat';

const USER_AVATAR = 'https://static.wixstatic.com/media/5dd29f_8946a06cf5544c1f99de458788f5b424~mv2.png';
const BOT_AVATAR  = 'https://static.wixstatic.com/media/5dd29f_d03e44b4f7f1481ab5c12d684830a18b~mv2.jpg';

// ------- UI labels -------
const SEND_READY_LABEL = '➤ Send';
const SEND_BUSY_LABEL  = '⏸ Generating…';

// The backend streams this line between its Phase 1 draft and Phase 2 refinement.
const HALFTIME_LINE = /^##\s*HALFTIME!!!.*$/m;

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

// Same as client/js/chat.js: the HALFTIME line shows as a status while Phase 2
// loads, then disappears once Phase 2 text arrives.
function renderBot(text) {
  const match = text.match(HALFTIME_LINE);
  if (!match) return md(text);

  const before = text.slice(0, match.index).trimEnd();
  const after = text.slice(match.index + match[0].length).trim();
  if (after) return md(`${before}\n\n${after}`);
  return md(`${before}\n\n*${match[0].replace(/^##\s*/, '')}*`);
}

function stripHalftime(text) {
  return text.replace(HALFTIME_LINE, '').replace(/\n{3,}/g, '\n\n').trim();
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
  // Messages FROM the chat iframe - registered first so its handshake isn't missed
  $w('#chatHtml').onMessage((event) => {
    const d = event.data || {};
    if (d.type === 'iframe-ready') {
      if (chatReady) sendIframeSetup();   // iframe reloaded - restore theme + avatars
      else markChatReady();
    }
    if (d.type === 'stop-click') stopStreaming();   // cancel stream, keep the partial answer
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

  // Same shape as client/js/chat.js: prior turns in `conversation`, the new
  // message only in `parts` (the backend adds it to the prompt itself).
  const payload = {
    jailbreak: true,                 // required key on the backend, value unused
    conversation_id: conversationId, // groups this chat's turns in the server-side logs
    meta: {
      content: {
        conversation: conversation.map(m => ({ role: m.role, content: m.content })),
        parts: [{ content: userMessage }]
      }
    }
  };
  conversation.push({ role: 'user', content: userMessage });

  // Start an empty bot message for streaming
  postToHtml({ type: 'add-message', from: 'bot', html: '' });

  abortCtrl = new AbortController();

  fetch(API_URL, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
    signal: abortCtrl.signal
  })
    .then(res => {
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
            finishBot('*No answer came back - please try again.*');
            return;
          }

          lastStreamText += decoder.decode(value, { stream: true });
          postToHtml({ type: 'update-bot-html', html: renderBot(normalizeEscapes(lastStreamText)), cursor: true });
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
      finishBot('*Something went wrong - please try again.*');
    });
}

// One place that ends a response: normal end, read error, fetch error and Stop.
function finishBot(fallbackMarkdown) {
  const finalText = stripHalftime(normalizeEscapes(lastStreamText));
  postToHtml({ type: 'finalize-bot', html: md(finalText || fallbackMarkdown) });
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

  finishBot('*Generation stopped.*');
}
