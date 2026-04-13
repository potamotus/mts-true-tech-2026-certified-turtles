/**
 * GPTHub custom loader: memory toast.
 * Loaded by Open WebUI via app.html <script src="/static/loader.js">
 */
(function () {
  const API_BASE = window.location.protocol + '//' + window.location.hostname + ':8000';
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
    'td .rounded-full{white-space:nowrap}';
  document.head.appendChild(style);

  var toast = document.createElement('div');
  toast.className = 'memory-toast';
  toast.innerHTML = '<span class="memory-toast-dot"></span><span class="memory-toast-text"></span>';

  var hideTimeout = null;

  function showToast(text) {
    toast.querySelector('.memory-toast-text').textContent = text;
    toast.classList.add('visible');
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

  /* ── Agent Settings injection (Settings > Connections page) ── */
  function injectAgentSettings() {
    if (document.getElementById('ct-agent-settings')) return;
    // Find "Direct Connections" text node and insert before its parent section
    var allEls = document.querySelectorAll('div');
    var directConnDiv = null;
    for (var i = 0; i < allEls.length; i++) {
      var el = allEls[i];
      if (el.children.length === 0 && el.textContent.trim() === 'Direct Connections') {
        directConnDiv = el.closest('.my-2');
        break;
      }
    }
    if (!directConnDiv) return;

    // Load current value
    fetch(API_BASE + '/api/v1/agent/config')
      .then(function(r) { return r.json(); })
      .then(function(d) {
        var input = document.getElementById('ct-agent-tokens-input');
        if (input) input.value = d.max_agent_tokens || 128000;
      })
      .catch(function() {});

    var section = document.createElement('div');
    section.id = 'ct-agent-settings';
    section.className = 'my-2';
    section.innerHTML =
      '<div class="flex justify-between items-center text-sm">' +
        '<div class="font-medium">Agent Settings</div>' +
        '<span id="ct-agent-save-status" class="text-xs font-medium" style="display:none"></span>' +
      '</div>' +
      '<div class="mt-1 text-xs text-gray-400 dark:text-gray-500">' +
        'Token budget per agent request. The agent loop stops when cumulative tokens exceed this limit.' +
      '</div>' +
      '<div class="mt-2.5">' +
        '<div class="text-xs font-medium mb-1">Max Agent Tokens</div>' +
        '<div class="flex gap-2">' +
          '<input id="ct-agent-tokens-input" ' +
            'class="w-full rounded-lg py-1.5 px-4 text-sm bg-gray-50 dark:text-gray-300 dark:bg-gray-850 outline-hidden" ' +
            'type="number" min="1000" max="1000000" step="1000" value="128000">' +
          '<button id="ct-agent-save-btn" ' +
            'class="px-3 py-1.5 text-xs font-medium rounded-lg border border-gray-200 dark:border-gray-700 hover:bg-gray-100 dark:hover:bg-gray-800 transition" ' +
            'type="button">Save</button>' +
        '</div>' +
      '</div>';

    // Insert: <hr> + section before Direct Connections
    var hr = document.createElement('hr');
    hr.className = 'border-gray-100/30 dark:border-gray-850/30 my-2';
    var parent = directConnDiv.parentNode;
    parent.insertBefore(hr, directConnDiv);
    parent.insertBefore(section, directConnDiv);

    document.getElementById('ct-agent-save-btn').addEventListener('click', function() {
      var val = parseInt(document.getElementById('ct-agent-tokens-input').value) || 128000;
      var status = document.getElementById('ct-agent-save-status');
      fetch(API_BASE + '/api/v1/agent/config', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ max_agent_tokens: val })
      })
        .then(function(r) { return r.json(); })
        .then(function(d) {
          document.getElementById('ct-agent-tokens-input').value = d.max_agent_tokens;
          status.textContent = 'Saved';
          status.style.display = '';
          status.style.color = '#22c55e';
          setTimeout(function() { status.style.display = 'none'; }, 2000);
        })
        .catch(function(e) {
          status.textContent = 'Error';
          status.style.display = '';
          status.style.color = '#ef4444';
          setTimeout(function() { status.style.display = 'none'; }, 3000);
        });
    });
  }

  var _agentSettingsInterval = null;
  function watchForSettingsPage() {
    if (_agentSettingsInterval) return;
    _agentSettingsInterval = setInterval(function() {
      if (!document.getElementById('ct-agent-settings')) {
        injectAgentSettings();
      }
    }, 200);
  }

  function init() {
    document.body.appendChild(toast);
    connectSSE();
    watchForSettingsPage();
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
