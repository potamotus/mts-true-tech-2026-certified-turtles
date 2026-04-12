/**
 * Memory toast notification for OpenWebUI.
 *
 * Inject via OpenWebUI Admin > Settings > Interface > Custom JavaScript,
 * or add as a <script> tag.
 *
 * Connects to the memory events SSE endpoint and shows a subtle toast
 * under the chat input when the fork agent updates memory.
 */
(function () {
  // OpenWebUI runs on :3000, API on :8000 — same host, different port.
  const API_BASE = `${window.location.protocol}//${window.location.hostname}:8000`;
  const SCOPE = 'default-scope';

  // Inject toast styles
  const style = document.createElement('style');
  style.textContent = `
    .memory-toast {
      position: fixed;
      bottom: 80px;
      left: 50%;
      transform: translateX(-50%) translateY(10px);
      opacity: 0;
      background: rgba(30, 30, 30, 0.92);
      backdrop-filter: blur(8px);
      border: 1px solid rgba(91, 141, 239, 0.3);
      border-radius: 8px;
      padding: 8px 16px;
      font-size: 12px;
      color: #b0b0b0;
      z-index: 9999;
      pointer-events: none;
      transition: opacity 0.3s, transform 0.3s;
      display: flex;
      align-items: center;
      gap: 6px;
      font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
    }
    .memory-toast.visible {
      opacity: 1;
      transform: translateX(-50%) translateY(0);
    }
    .memory-toast-dot {
      width: 6px;
      height: 6px;
      border-radius: 50%;
      background: #5b8def;
      flex-shrink: 0;
    }
  `;
  document.head.appendChild(style);

  // Create toast element
  const toast = document.createElement('div');
  toast.className = 'memory-toast';
  toast.innerHTML = '<span class="memory-toast-dot"></span><span class="memory-toast-text"></span>';
  document.body.appendChild(toast);

  let hideTimeout = null;

  function showMemoryToast(text) {
    toast.querySelector('.memory-toast-text').textContent = text;
    toast.classList.add('visible');
    if (hideTimeout) clearTimeout(hideTimeout);
    hideTimeout = setTimeout(() => toast.classList.remove('visible'), 4000);
  }

  function connect() {
    const es = new EventSource(`${API_BASE}/api/v1/memory-events?scope_id=${encodeURIComponent(SCOPE)}`);
    es.onmessage = (e) => {
      try {
        const data = JSON.parse(e.data);
        if (data.type === 'connected') return;
        showMemoryToast('Память обновлена');
      } catch {}
    };
    es.onerror = () => {
      es.close();
      setTimeout(connect, 10000);
    };
  }

  // Wait for page to be ready
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', connect);
  } else {
    connect();
  }
})();
