/**
 * GPTHub custom loader: memory toast + memory button.
 * Loaded by Open WebUI via app.html <script src="/static/loader.js">
 */
(function () {
  const API_BASE = window.location.protocol + '//' + window.location.hostname + ':8000';
  const MEMORY_PAGE = API_BASE + '/memory';
  const SCOPE = 'default-scope';

  /* ── Toast ── */
  var style = document.createElement('style');
  style.textContent =
    '.memory-toast{position:fixed;bottom:80px;left:50%;transform:translateX(-50%) translateY(10px);opacity:0;' +
    'background:rgba(30,30,30,.92);backdrop-filter:blur(8px);border:1px solid rgba(91,141,239,.3);' +
    'border-radius:8px;padding:8px 16px;font-size:12px;color:#b0b0b0;z-index:9999;pointer-events:none;' +
    'transition:opacity .3s,transform .3s;display:flex;align-items:center;gap:6px;' +
    'font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}' +
    '.memory-toast.visible{opacity:1;transform:translateX(-50%) translateY(0)}' +
    '.memory-toast-dot{width:6px;height:6px;border-radius:50%;background:#5b8def;flex-shrink:0}' +
    /* ── Memory button ── */
    '.memory-btn{position:fixed;bottom:16px;right:16px;z-index:9998;width:40px;height:40px;' +
    'border-radius:50%;background:rgba(30,30,30,.85);backdrop-filter:blur(8px);' +
    'border:1px solid rgba(91,141,239,.3);cursor:pointer;display:flex;align-items:center;' +
    'justify-content:center;transition:background .2s,border-color .2s,transform .15s;padding:0}' +
    '.memory-btn:hover{background:rgba(91,141,239,.15);border-color:rgba(91,141,239,.6);transform:scale(1.08)}' +
    '.memory-btn svg{width:20px;height:20px;fill:none;stroke:#5b8def;stroke-width:1.8;stroke-linecap:round;stroke-linejoin:round}' +
    '.memory-btn.pulse svg{animation:mem-pulse .6s ease}' +
    '@keyframes mem-pulse{0%,100%{transform:scale(1)}50%{transform:scale(1.25)}}';
  document.head.appendChild(style);

  var toast = document.createElement('div');
  toast.className = 'memory-toast';
  toast.innerHTML = '<span class="memory-toast-dot"></span><span class="memory-toast-text"></span>';

  var btn = document.createElement('a');
  btn.className = 'memory-btn';
  btn.href = MEMORY_PAGE;
  btn.target = '_blank';
  btn.title = 'Память';
  btn.innerHTML = '<svg viewBox="0 0 24 24"><path d="M12 2a7 7 0 0 1 7 7c0 2.38-1.19 4.47-3 5.74V17a1 1 0 0 1-1 1H9a1 1 0 0 1-1-1v-2.26C6.19 13.47 5 11.38 5 9a7 7 0 0 1 7-7z"/><line x1="9" y1="21" x2="15" y2="21"/><line x1="10" y1="24" x2="14" y2="24"/></svg>';

  var hideTimeout = null;

  function showToast(text) {
    toast.querySelector('.memory-toast-text').textContent = text;
    toast.classList.add('visible');
    btn.classList.add('pulse');
    setTimeout(function () { btn.classList.remove('pulse'); }, 600);
    if (hideTimeout) clearTimeout(hideTimeout);
    hideTimeout = setTimeout(function () { toast.classList.remove('visible'); }, 4000);
  }

  function connectSSE() {
    var es = new EventSource(API_BASE + '/api/v1/memory-events?scope_id=' + encodeURIComponent(SCOPE));
    es.onmessage = function (e) {
      try {
        var data = JSON.parse(e.data);
        if (data.type === 'connected') return;
        showToast('Память обновлена');
      } catch (err) {}
    };
    es.onerror = function () {
      es.close();
      setTimeout(connectSSE, 10000);
    };
  }

  function init() {
    document.body.appendChild(toast);
    document.body.appendChild(btn);
    connectSSE();
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
