// Короткий тур по интерфейсу для нового пользователя. Показывается один раз, повтор — кнопка «?» в шапке.
(function () {
  var I18N = window.TOUR_I18N || { next: 'Next', done: 'Done', skip: 'Skip', of: '/' };
  var KEY = window.TOUR_KEY || 'cafepos_tour';
  var steps = [], index = 0, spot = null, card = null;

  function seen() { try { return localStorage.getItem(KEY) === '1'; } catch (e) { return true; } }
  function remember() { try { localStorage.setItem(KEY, '1'); } catch (e) {} }

  function collect() {
    return Array.prototype.slice.call(document.querySelectorAll('[data-tour]'))
      .filter(function (el) { return el.offsetParent !== null || el.getClientRects().length; })
      .sort(function (a, b) { return a.dataset.tour - b.dataset.tour; });
  }

  function close(markSeen) {
    if (spot) spot.remove();
    if (card) card.remove();
    spot = card = null;
    document.removeEventListener('keydown', onKey);
    window.removeEventListener('resize', place);
    if (markSeen) remember();
  }

  function onKey(e) { if (e.key === 'Escape') close(true); }

  function place() {
    if (!steps.length || !spot) return;
    var el = steps[index];
    el.scrollIntoView({ block: 'nearest', inline: 'nearest' });
    var r = el.getBoundingClientRect(), pad = 6;
    spot.style.cssText = 'position:fixed;z-index:9998;pointer-events:none;border-radius:14px;transition:all .25s ease;' +
      'left:' + (r.left - pad) + 'px;top:' + (r.top - pad) + 'px;width:' + (r.width + pad * 2) + 'px;height:' + (r.height + pad * 2) + 'px;' +
      'box-shadow:0 0 0 9999px rgba(30,25,21,.55);outline:2px solid #C98B45;';
    var cw = Math.min(320, window.innerWidth - 24), ch = card.offsetHeight || 150;
    var left = r.right + 16, top = r.top;
    if (left + cw > window.innerWidth - 12) {
      left = Math.max(12, Math.min(r.left, window.innerWidth - cw - 12));
      top = r.bottom + 16;
    }
    if (top + ch > window.innerHeight - 12) top = Math.max(12, window.innerHeight - ch - 12);
    card.style.left = left + 'px';
    card.style.top = top + 'px';
    card.style.width = cw + 'px';
  }

  function render() {
    var el = steps[index], last = index === steps.length - 1;
    card.innerHTML = '';
    var title = document.createElement('div');
    title.style.cssText = 'font-weight:800;font-size:15px;color:#1E1915;margin-bottom:4px';
    title.textContent = el.dataset.tourTitle || '';
    var text = document.createElement('div');
    text.style.cssText = 'font-size:13px;line-height:1.45;color:#5b5148';
    text.textContent = el.dataset.tourText || '';
    var bar = document.createElement('div');
    bar.style.cssText = 'display:flex;align-items:center;justify-content:space-between;gap:8px;margin-top:14px';
    var count = document.createElement('span');
    count.style.cssText = 'font-size:12px;color:#7A6F63;font-weight:600';
    count.textContent = (index + 1) + ' ' + I18N.of + ' ' + steps.length;
    var skip = document.createElement('button');
    skip.type = 'button'; skip.className = 'btn btn-ghost btn-sm'; skip.textContent = I18N.skip;
    skip.onclick = function () { close(true); };
    var next = document.createElement('button');
    next.type = 'button'; next.className = 'btn btn-primary btn-sm'; next.textContent = last ? I18N.done : I18N.next;
    next.onclick = function () { if (last) { close(true); } else { index++; render(); } };
    var right = document.createElement('span');
    right.style.cssText = 'display:flex;gap:6px';
    if (!last) right.appendChild(skip);
    right.appendChild(next);
    bar.appendChild(count);
    bar.appendChild(right);
    card.appendChild(title);
    card.appendChild(text);
    card.appendChild(bar);
    place();
    next.focus();
  }

  window.startTour = function () {
    close(false);
    steps = collect();
    if (!steps.length) return;
    index = 0;
    spot = document.createElement('div');
    card = document.createElement('div');
    card.setAttribute('role', 'dialog');
    card.setAttribute('aria-live', 'polite');
    card.style.cssText = 'position:fixed;z-index:9999;background:#fff;border:1px solid #E8DFD2;border-radius:16px;padding:16px;box-shadow:0 18px 44px -14px rgba(60,36,18,.35)';
    document.body.appendChild(spot);
    document.body.appendChild(card);
    document.addEventListener('keydown', onKey);
    window.addEventListener('resize', place);
    render();
  };

  document.addEventListener('DOMContentLoaded', function () {
    if (!seen() && window.matchMedia('(min-width: 768px)').matches) setTimeout(window.startTour, 600);
  });
})();
