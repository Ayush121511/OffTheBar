import { marked } from 'marked';
import wixLocation from 'wix-location';
import { session } from 'wix-storage';

const API_URL = 'https://offthebar-api-607277832880.asia-south1.run.app/api/chat';

const USER_AVATAR = 'https://static.wixstatic.com/media/5dd29f_8946a06cf5544c1f99de458788f5b424~mv2.png';
const BOT_AVATAR  = 'https://static.wixstatic.com/media/5dd29f_d03e44b4f7f1481ab5c12d684830a18b~mv2.jpg';

let conversation = [];
let streaming = false;

// ------- NEW: streaming handles for stop -------
let abortCtrl = null;           // AbortController for fetch
let currentReader = null;       // ReadableStream reader
let lastStreamText = '';        // accumulated text so far

// ------- UI labels -------
const SEND_READY_LABEL = '➤ Send';
const SEND_BUSY_LABEL  = '⏸ Generating…';

// ------- Helpers -------
const postToHtml = (payload) => { try { $w('#chatHtml').postMessage(payload); } catch(_){} };

function normalizeEscapes(s){
  return (s || '')
    .replace(/\\r\\n/g, '\n')
    .replace(/\\n/g, '\n')
    .replace(/\\t/g, '    ');
}

function md(text){
  return marked.parse(text || '', { gfm: true, breaks: false });
}

function escapeHtml(s){
  return (s || '')
    .replace(/[&<>"']/g, m => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#039;'}[m]))
    .replace(/\r?\n/g, '<br>');
}

// Tell chat iframe to show/hide its Stop button
function setIframeStopVisible(show){
  postToHtml({ type:'set-stop-visible', visible: !!show });
}

// Button label compatibility: Button vs IconButton
function setSendBusy(busy){
  const btn = $w('#sendBtn');
  const ready = SEND_READY_LABEL;
  const busyTxt = SEND_BUSY_LABEL;

  if ('label' in btn && btn.label !== undefined) btn.label = busy ? busyTxt : ready;
  else if ('text' in btn && btn.text !== undefined) btn.text = busy ? busyTxt : ready;

  if ('enabled' in btn) btn.enabled = !busy;

  // Disable/enable the input iframe’s textarea
  try { $w('#inputHtml').postMessage({type:'set-disabled', disabled: busy}); } catch(_) {}

  // NEW: toggle Stop button inside chat iframe
  setIframeStopVisible(busy);
}

$w.onReady(() => {
  // Theme + avatars for the chat view iframe
  postToHtml({ type:'set-theme-vars', vars: { '--font':'Google Sans, sans-serif' } });
  postToHtml({ type:'set-avatars', user: USER_AVATAR, bot: BOT_AVATAR });
  $w('#titleStack').onClick(() => {
    stopStreaming();

    conversation = [];

    postToHtml({
        type: 'clear-chat'
    });
    setSendBusy(false);
    wixLocation.to('/offthebar');
  });
  // Listen for messages FROM the input iframe
  $w('#inputHtml').onMessage((event) => {
    const d = event.data || {};
    if (d.type === 'input-ready'){
      // $w('#inputHtml').postMessage({type:'focus'});
    }
    if (d.type === 'send-intent'){
      if (!streaming) handleSend(d.value || '');
    }
    if (d.type === 'value'){
      if (!streaming) handleSend(d.value || '');
    }
  });

  // Messages FROM the chatHtml iframe
  $w('#chatHtml').onMessage((event) => {
    const d = event.data || {};
    if (d.type === 'iframe-ready') {
      // chatHtml ready
    }
    // NEW: Stop clicked inside iframe
    if (d.type === 'stop-click') {
      stopStreaming(); // cancel stream & finalize partial
    }
  });

  // Click on Send — ask input iframe to force-send current value
  $w('#sendBtn').onClick(() => {
    if (streaming) return;
    try { $w('#inputHtml').postMessage({type:'force-send'}); } catch(_) {}
  });

  // Initialize
  setSendBusy(false);
  const qp = wixLocation.query || {};
  const initialMsg = (qp.msg || '').trim();

  conversation = [];

  // Optional safety: prevent huge payloads
  // if (initialMsg.length > 2000) initialMsg = initialMsg.slice(0, 2000);

  if (initialMsg && !streaming) {
    handleSend(initialMsg); // uses your existing flow
  }
});

// Core send handler (unchanged)
function handleSend(raw){
  const text = (raw || '').trim();
  if (!text) return;

  setSendBusy(true);

  // Show user bubble
  postToHtml({ type:'add-message', from:'user', html: escapeHtml(text) });

  // Clear input + shrink
  try { $w('#inputHtml').postMessage({type:'set-value', value:''}); } catch(_) {}

  // send to API (stream)
  sendToChatbot(text);
}

// ---------------- CHAT FLOW -------------
function sendToChatbot(userMessage){
  streaming = true;
  lastStreamText = '';   // reset accumulator

  conversation.push({ role:'user', content:userMessage });

  const payload = {
    jailbreak: true,
    meta: {
      content: {
        conversation: conversation.map(m => ({ role:m.role, content:m.content })),
        parts: [{ content: userMessage }]
      }
    }
  };

  // Start an empty bot message for streaming
  postToHtml({ type:'add-message', from:'bot', html: '' });

  // NEW: abort controller for stopping
  abortCtrl = new AbortController();

  fetch(API_URL, {
    method:'POST',
    headers:{ 'Content-Type':'application/json' },
    body: JSON.stringify(payload),
    signal: abortCtrl.signal
  })
  .then(res => {
    if (!res.ok) throw new Error('Bad response');
    currentReader = res.body.getReader();
    const decoder = new TextDecoder('utf-8');

    const pump = () => {
      currentReader.read().then(({done, value}) => {
        if (done){
          // normal end -> finalize
          const finalText = normalizeEscapes(lastStreamText).trim();
          const finalHtml = md(finalText);
          postToHtml({ type:'finalize-bot', html: finalHtml });

          conversation.push({ role:'model', content: finalText });
          streaming = false;
          setSendBusy(false);

          // cleanup
          currentReader = null;
          abortCtrl = null;
          lastStreamText = '';
          return;
        }

        const chunk = decoder.decode(value, { stream:true });
        lastStreamText += chunk;

        // Progressive Markdown render
        const partialText = normalizeEscapes(lastStreamText);
        const partialHtml = md(partialText);
        postToHtml({ type:'update-bot-html', html: partialHtml, cursor: true });

        pump();
      }).catch(err => {
        // If aborted, we finalize in stopStreaming()
        if (err && err.name === 'AbortError') return;

        console.error('Stream read error:', err);
        // finalize with whatever we have
        const finalText = normalizeEscapes(lastStreamText).trim();
        const finalHtml = md(finalText || '*Generation stopped.*');
        postToHtml({ type:'finalize-bot', html: finalHtml });

        conversation.push({ role:'model', content: finalText });
        streaming = false;
        setSendBusy(false);

        currentReader = null;
        abortCtrl = null;
        lastStreamText = '';
      });
    };

    pump();
  })
  .catch(err => {
    if (err && err.name === 'AbortError') return; // handled by stop
    console.error('Fetch error:', err);

    const finalText = normalizeEscapes(lastStreamText).trim();
    const finalHtml = md(finalText || '*Generation stopped.*');
    postToHtml({ type:'finalize-bot', html: finalHtml });

    if (finalText) conversation.push({ role:'model', content: finalText });
    streaming = false;
    setSendBusy(false);

    currentReader = null;
    abortCtrl = null;
    lastStreamText = '';
  });
}

// ------- NEW: Stop generation consistent with your original flow -------
function stopStreaming(){
  if (!streaming) return;

  // 1) cancel reader + abort fetch
  try { if (currentReader) currentReader.cancel('user-stopped'); } catch(_) {}
  try { if (abortCtrl) abortCtrl.abort(); } catch(_) {}

  // 2) finalize with whatever text we have so far
  const finalText = normalizeEscapes(lastStreamText).trim();
  const finalHtml = md(finalText || '*Generation stopped.*');
  postToHtml({ type:'finalize-bot', html: finalHtml });
  if (finalText) conversation.push({ role:'model', content: finalText });

  // 3) reset UI + state
  streaming = false;
  setSendBusy(false);

  currentReader = null;
  abortCtrl = null;
  lastStreamText = '';
}
