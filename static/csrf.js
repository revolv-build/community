// Auto-inject CSRF token into all forms and fetch requests
(function() {
  var token = document.querySelector('meta[name="csrf-token"]');
  if (!token) return;
  var csrfToken = token.getAttribute('content');

  // Inject hidden input into all forms that don't already have one
  document.addEventListener('submit', function(e) {
    var form = e.target;
    if (form.tagName !== 'FORM') return;
    if (form.querySelector('input[name="csrf_token"]')) return;
    var input = document.createElement('input');
    input.type = 'hidden';
    input.name = 'csrf_token';
    input.value = csrfToken;
    form.appendChild(input);
  }, true);

  // Patch fetch to include CSRF header for POST requests
  var originalFetch = window.fetch;
  window.fetch = function(url, opts) {
    opts = opts || {};
    if (opts.method && opts.method.toUpperCase() === 'POST') {
      opts.headers = opts.headers || {};
      if (opts.headers instanceof Headers) {
        opts.headers.set('X-CSRFToken', csrfToken);
      } else {
        opts.headers['X-CSRFToken'] = csrfToken;
      }
    }
    return originalFetch.call(this, url, opts);
  };
})();
