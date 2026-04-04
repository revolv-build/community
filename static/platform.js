// Platform enhancements: keyboard shortcuts, read state, back to top
(function() {
  'use strict';

  // ── Back to Top ────────────────────────────────────────────────
  var btn = document.createElement('button');
  btn.className = 'back-to-top';
  btn.innerHTML = '&#9650;';
  btn.title = 'Back to top';
  btn.onclick = function() { window.scrollTo({top: 0, behavior: 'smooth'}); };
  document.body.appendChild(btn);

  window.addEventListener('scroll', function() {
    btn.classList.toggle('btt-show', window.scrollY > 400);
  });

  // ── Read State ─────────────────────────────────────────────────
  var READ_KEY = 'community_read_posts';
  var readPosts = {};
  try { readPosts = JSON.parse(localStorage.getItem(READ_KEY) || '{}'); } catch(e) {}

  function markRead(postId) {
    readPosts[postId] = Date.now();
    // Keep only last 500
    var keys = Object.keys(readPosts);
    if (keys.length > 500) {
      keys.sort(function(a,b) { return readPosts[a] - readPosts[b]; });
      for (var i = 0; i < keys.length - 500; i++) delete readPosts[keys[i]];
    }
    localStorage.setItem(READ_KEY, JSON.stringify(readPosts));
  }

  // Apply read state to existing cards
  document.querySelectorAll('.qa-card').forEach(function(card) {
    var link = card.querySelector('.qa-title a');
    if (!link) return;
    var match = link.getAttribute('href').match(/\/posts\/(\d+)$/);
    if (match && readPosts[match[1]]) {
      card.classList.add('qa-read');
    }
  });

  // Track clicks on post links and cards
  document.addEventListener('click', function(e) {
    var card = e.target.closest('.qa-card');
    if (!card) return;
    var link = card.querySelector('.qa-title a');
    if (!link) return;
    var match = link.getAttribute('href').match(/\/posts\/(\d+)$/);
    if (match) {
      markRead(match[1]);
      card.classList.add('qa-read');
    }
  });

  // ── Keyboard Shortcuts ─────────────────────────────────────────
  var currentIndex = -1;

  function getCards() {
    return Array.from(document.querySelectorAll('.qa-card'));
  }

  function highlightCard(index) {
    var cards = getCards();
    cards.forEach(function(c) { c.classList.remove('qa-focused'); });
    if (index >= 0 && index < cards.length) {
      currentIndex = index;
      cards[index].classList.add('qa-focused');
      cards[index].scrollIntoView({behavior: 'smooth', block: 'center'});
    }
  }

  document.addEventListener('keydown', function(e) {
    // Don't trigger when typing in inputs
    if (e.target.tagName === 'INPUT' || e.target.tagName === 'TEXTAREA' || e.target.tagName === 'SELECT') return;
    if (e.target.closest('.EasyMDEContainer')) return;

    var cards = getCards();
    if (!cards.length) return;

    switch(e.key) {
      case 'j': // Next post
        e.preventDefault();
        highlightCard(Math.min(currentIndex + 1, cards.length - 1));
        break;
      case 'k': // Previous post
        e.preventDefault();
        highlightCard(Math.max(currentIndex - 1, 0));
        break;
      case 'Enter': // Open post
        if (currentIndex >= 0 && currentIndex < cards.length) {
          e.preventDefault();
          var link = cards[currentIndex].querySelector('.qa-title a');
          if (link) link.click();
        }
        break;
    }
  });

})();
