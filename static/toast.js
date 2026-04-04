// Toast notification system — auto-dismissing stacked toasts
(function() {
  var container = document.createElement('div');
  container.className = 'toast-container';
  document.body.appendChild(container);

  // Convert existing flash messages to toasts
  document.querySelectorAll('.msg').forEach(function(msg) {
    var type = msg.classList.contains('msg-ok') ? 'success' : 'error';
    showToast(msg.textContent.trim(), type);
    msg.remove();
  });

  window.showToast = function(message, type) {
    type = type || 'success';
    var toast = document.createElement('div');
    toast.className = 'toast toast-' + type;
    toast.innerHTML = '<span class="toast-text">' + message + '</span><button class="toast-close" onclick="this.parentNode.remove()">&times;</button>';
    container.appendChild(toast);

    // Animate in
    requestAnimationFrame(function() { toast.classList.add('toast-show'); });

    // Auto dismiss after 5s
    setTimeout(function() {
      toast.classList.remove('toast-show');
      setTimeout(function() { toast.remove(); }, 300);
    }, 5000);
  };
})();
