(function() {
  'use strict';

  var slug = document.body.dataset.slug;
  if (!slug) return;
  var base = '/c/' + slug;

  // Create lightbox DOM
  var overlay = document.createElement('div');
  overlay.className = 'lb-overlay';
  overlay.innerHTML = '<div class="lb-backdrop"></div><div class="lb-container"><div class="lb-close">&times;</div><div class="lb-body"></div></div>';
  document.body.appendChild(overlay);

  var backdrop = overlay.querySelector('.lb-backdrop');
  var closeBtn = overlay.querySelector('.lb-close');
  var body = overlay.querySelector('.lb-body');
  var previousUrl = null;

  function close() {
    overlay.classList.remove('lb-open');
    document.body.style.overflow = '';
    if (previousUrl) history.pushState(null, '', previousUrl);
  }

  backdrop.addEventListener('click', close);
  closeBtn.addEventListener('click', close);
  document.addEventListener('keydown', function(e) {
    if (e.key === 'Escape' && overlay.classList.contains('lb-open')) close();
  });

  function esc(s) {
    var d = document.createElement('div');
    d.textContent = s;
    return d.innerHTML;
  }

  function timeago(iso) {
    if (!iso) return '';
    var now = Date.now();
    var then = new Date(iso.replace(' ', 'T') + (iso.includes('Z') ? '' : 'Z')).getTime();
    var diff = Math.floor((now - then) / 1000);
    if (diff < 60) return 'just now';
    if (diff < 3600) return Math.floor(diff / 60) + 'm ago';
    if (diff < 86400) return Math.floor(diff / 3600) + 'h ago';
    if (diff < 604800) return Math.floor(diff / 86400) + 'd ago';
    if (diff < 2592000) return Math.floor(diff / 604800) + 'w ago';
    return iso.substring(0, 10);
  }

  // Build nested comment tree
  function buildTree(comments) {
    var map = {}, roots = [];
    comments.forEach(function(c) { c.children = []; map[c.id] = c; });
    comments.forEach(function(c) {
      if (c.parent_id && map[c.parent_id]) map[c.parent_id].children.push(c);
      else roots.push(c);
    });
    return roots;
  }

  function renderComment(c, depth, data) {
    if (depth > 4) depth = 4;
    var upA = c.my_vote === 1 ? ' cv-up-active' : '';
    var downA = c.my_vote === -1 ? ' cv-down-active' : '';
    var scoreClass = c.vote_score > 0 ? ' qa-score-positive' : c.vote_score < 0 ? ' qa-score-negative' : '';
    var hasAward = c.has_awards ? ' cmt-awarded' : '';
    var indent = depth > 0 ? ' cmt-depth-' + depth : '';

    var h = '<div class="cmt' + indent + hasAward + '">';
    h += '<div class="cmt-left">';
    h += '<span class="qa-avatar qa-avatar-sm">' + esc(c.author_initial) + '</span>';
    // Comment vote arrows
    h += '<div class="cv-mini">';
    h += '<form method="POST" action="' + base + '/comments/' + c.id + '/vote"><input type="hidden" name="value" value="1"><button class="cv-btn' + upA + '">&#9650;</button></form>';
    h += '<span class="cv-score' + scoreClass + '">' + c.vote_score + '</span>';
    h += '<form method="POST" action="' + base + '/comments/' + c.id + '/vote"><input type="hidden" name="value" value="-1"><button class="cv-btn' + downA + '">&#9660;</button></form>';
    h += '</div>';
    h += '</div>';

    h += '<div class="cmt-right">';
    h += '<div class="cmt-header">';
    h += '<a href="' + base + '/members/' + c.user_id + '" class="qa-author-name">' + esc(c.author_name) + '</a>';
    h += '<span class="qa-date">' + timeago(c.created) + '</span>';
    if (c.can_delete) h += '<form method="POST" action="' + base + '/comments/' + c.id + '/delete" style="display:inline"><button class="qa-delete-btn">delete</button></form>';
    h += '</div>';
    h += '<div class="cmt-body md-content">' + (c.body_html || esc(c.body)) + '</div>';

    // Awards display
    if (c.awards && c.awards.length > 0) {
      h += '<div class="cmt-awards">';
      c.awards.forEach(function(a) {
        var mine = a.mine ? ' award-mine' : '';
        h += '<form method="POST" action="' + base + '/comments/' + c.id + '/award" class="award-form"><input type="hidden" name="emoji" value="' + a.emoji + '"><button class="award-pill' + mine + '">' + a.symbol + ' ' + a.count + '</button></form>';
      });
      h += '</div>';
    }

    // Award + Reply buttons
    h += '<div class="cmt-actions">';
    h += '<button class="cmt-action-btn" onclick="this.nextElementSibling.classList.toggle(\'open\')">Award</button>';
    h += '<div class="award-picker">';
    data.award_emojis.forEach(function(e) {
      h += '<form method="POST" action="' + base + '/comments/' + c.id + '/award" class="award-form"><input type="hidden" name="emoji" value="' + e.key + '"><button class="award-pick-btn">' + e.symbol + '</button></form>';
    });
    h += '</div>';
    if (depth < 4) {
      h += '<button class="cmt-action-btn" onclick="var f=this.parentNode.parentNode.querySelector(\'.cmt-reply-form\');f.style.display=f.style.display===\'none\'?\'block\':\'none\';f.querySelector(\'textarea\').focus()">Reply</button>';
    }
    h += '</div>';

    // Inline reply form (hidden by default)
    if (depth < 4) {
      h += '<div class="cmt-reply-form" style="display:none;">';
      h += '<form method="POST" action="' + base + '/posts/' + data.id + '/comment">';
      h += '<input type="hidden" name="parent_id" value="' + c.id + '">';
      h += '<textarea name="body" placeholder="Reply to ' + esc(c.author_name) + '..." required style="min-height:50px;font-size:12px;"></textarea>';
      h += '<div style="text-align:right;margin-top:6px;"><button class="btn btn-primary btn-sm">Reply</button></div>';
      h += '</form></div>';
    }

    // Render children
    if (c.children.length > 0) {
      h += '<div class="cmt-children">';
      c.children.forEach(function(child) {
        h += renderComment(child, depth + 1, data);
      });
      h += '</div>';
    }

    h += '</div></div>';
    return h;
  }

  function renderPost(data) {
    var upActive = data.my_vote === 1 ? ' qa-vote-up-active' : '';
    var downActive = data.my_vote === -1 ? ' qa-vote-down-active' : '';
    var scoreClass = data.vote_score > 0 ? ' qa-score-positive' : data.vote_score < 0 ? ' qa-score-negative' : '';
    var bookmarkIcon = data.is_bookmarked ? '★' : '☆';
    var bookmarkLabel = data.is_bookmarked ? 'Bookmarked' : 'Bookmark';
    var followIcon = data.is_following ? '◆' : '◇';
    var followLabel = data.is_following ? 'Following' : 'Follow';
    var followCount = data.follow_count ? ' (' + data.follow_count + ')' : '';

    var h = '<div class="lb-post">';
    h += '<div class="qa-card qa-card-detail" style="border:none;margin:0;">';
    h += '<div class="qa-votes">';
    h += '<form method="POST" action="' + base + '/posts/' + data.id + '/vote"><input type="hidden" name="value" value="1"><button class="qa-vote-btn' + upActive + '">&#9650;</button></form>';
    h += '<span class="qa-vote-score' + scoreClass + '">' + data.vote_score + '</span>';
    h += '<form method="POST" action="' + base + '/posts/' + data.id + '/vote"><input type="hidden" name="value" value="-1"><button class="qa-vote-btn' + downActive + '">&#9660;</button></form>';
    h += '</div><div class="qa-content">';
    h += '<div class="qa-detail-header"><div class="qa-author" style="margin-bottom:8px;">';
    h += '<span class="qa-avatar">' + esc(data.author_initial) + '</span>';
    h += '<a href="' + base + '/members/' + data.user_id + '" class="qa-author-name">' + esc(data.author_name) + '</a>';
    if (data.flair) h += '<span class="qa-flair">' + esc(data.flair) + '</span>';
    h += '<span class="qa-date">' + timeago(data.created) + '</span>';
    h += '</div>';
    if (data.is_owner || data.is_admin) {
      h += '<div class="flex gap-8">';
      if (data.is_owner) h += '<a href="' + base + '/posts/' + data.id + '/edit" class="btn btn-ghost btn-sm">Edit</a>';
      h += '<form method="POST" action="' + base + '/posts/' + data.id + '/delete" onsubmit="return confirm(\'Delete?\')"><button class="btn btn-danger btn-sm">Delete</button></form></div>';
    }
    h += '</div>';
    if (data.is_pinned) h += '<span class="qa-pin-badge">Pinned</span> ';
    if (data.category) h += '<a href="' + base + '/?category=' + encodeURIComponent(data.category) + '" class="qa-cat-badge">' + esc(data.category) + '</a>';
    h += '<h1 class="qa-detail-title">' + esc(data.title) + '</h1>';
    if (data.body_html) h += '<div class="post-body md-content">' + data.body_html + '</div>';
    else if (data.body) h += '<div class="post-body">' + esc(data.body) + '</div>';
    h += '<div class="qa-detail-actions">';
    h += '<form method="POST" action="' + base + '/posts/' + data.id + '/bookmark" class="qa-action-form"><button class="qa-action-btn-lg ' + (data.is_bookmarked ? 'qa-action-active' : '') + '">' + bookmarkIcon + ' ' + bookmarkLabel + '</button></form>';
    h += '<form method="POST" action="' + base + '/posts/' + data.id + '/follow" class="qa-action-form"><button class="qa-action-btn-lg ' + (data.is_following ? 'qa-action-active' : '') + '">' + followIcon + ' ' + followLabel + followCount + '</button></form>';
    h += '<button class="qa-action-btn-lg" onclick="navigator.clipboard.writeText(window.location.href);this.innerHTML=\'✓ Copied!\';setTimeout(()=>this.innerHTML=\'↗ Share\',1500)">↗ Share</button>';
    h += '</div></div></div>';

    // Comments
    var tree = buildTree(data.comments);
    h += '<div class="lb-comments">';
    h += '<div class="lb-comments-header" onclick="this.parentNode.classList.toggle(\'lb-collapsed\')">';
    h += '<h2 style="margin-bottom:0;">' + data.comments.length + ' Comment' + (data.comments.length !== 1 ? 's' : '') + '</h2>';
    h += '<span class="lb-collapse-icon">▾</span></div>';
    h += '<form method="POST" action="' + base + '/posts/' + data.id + '/comment" class="qa-comment-form">';
    h += '<div class="qa-comment-input-row"><span class="qa-avatar qa-avatar-sm">' + esc(data.current_user_initial) + '</span>';
    h += '<textarea name="body" placeholder="Add a comment..." required></textarea></div>';
    h += '<div style="text-align:right;margin-top:8px;"><button class="btn btn-primary btn-sm">Comment</button></div></form>';
    h += '<div class="lb-comment-list">';
    tree.forEach(function(c) { h += renderComment(c, 0, data); });
    h += '</div></div></div>';
    return h;
  }

  function openPost(postId) {
    previousUrl = window.location.href;
    body.innerHTML = '<div class="lb-loading">Loading...</div>';
    overlay.classList.add('lb-open');
    document.body.style.overflow = 'hidden';
    history.pushState(null, '', base + '/posts/' + postId);
    fetch(base + '/posts/' + postId + '/json')
      .then(function(r) { return r.json(); })
      .then(function(data) {
        if (data.error) { body.innerHTML = '<div class="lb-loading">Post not found.</div>'; return; }
        body.innerHTML = renderPost(data);
      })
      .catch(function() { body.innerHTML = '<div class="lb-loading">Failed to load.</div>'; });
  }

  // Full card click — intercept clicks on .qa-card
  document.addEventListener('click', function(e) {
    // Don't intercept clicks on interactive elements
    if (e.target.closest('button, a, form, input, textarea, select, .qa-votes, .qa-actions, .qa-action-form')) return;

    var card = e.target.closest('.qa-card');
    if (!card) return;
    // Find the post link inside the card
    var link = card.querySelector('.qa-title a');
    if (!link) return;
    var href = link.getAttribute('href');
    var match = href.match(/\/c\/([^/]+)\/posts\/(\d+)$/);
    if (!match || match[1] !== slug) return;
    e.preventDefault();
    openPost(parseInt(match[2]));
  });

  // Also intercept direct post title link clicks
  document.addEventListener('click', function(e) {
    var link = e.target.closest('.qa-title a');
    if (!link) return;
    var href = link.getAttribute('href');
    var match = href.match(/\/c\/([^/]+)\/posts\/(\d+)$/);
    if (!match || match[1] !== slug) return;
    e.preventDefault();
    openPost(parseInt(match[2]));
  });

  // Intercept form submissions inside the lightbox — use AJAX, then reload post
  overlay.addEventListener('submit', function(e) {
    var form = e.target;
    if (!form.closest('.lb-body')) return;

    // Allow edit links and delete confirmations to navigate normally
    var action = form.getAttribute('action') || '';
    if (action.includes('/edit') || action.includes('/delete')) return;

    e.preventDefault();

    var formData = new FormData(form);
    fetch(action, {
      method: 'POST',
      body: formData
    }).then(function() {
      // Extract post ID from the action URL and reload lightbox content
      var match = action.match(/\/posts\/(\d+)/);
      if (match) {
        openPost(parseInt(match[1]));
      } else {
        // For comment votes/awards, extract from comments URL pattern
        var cmatch = action.match(/\/comments\/(\d+)/);
        if (cmatch) {
          // Get current post id from the URL
          var urlMatch = window.location.pathname.match(/\/posts\/(\d+)/);
          if (urlMatch) openPost(parseInt(urlMatch[1]));
        }
      }
    });
  });

  window.addEventListener('popstate', function() {
    if (overlay.classList.contains('lb-open')) {
      overlay.classList.remove('lb-open');
      document.body.style.overflow = '';
    }
  });
})();
