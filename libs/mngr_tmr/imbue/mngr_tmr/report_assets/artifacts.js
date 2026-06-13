(function() {
  var overlay = document.getElementById('artifacts-overlay');
  function closePanel() {
    document.querySelectorAll('.artifacts-panel.open').forEach(function(p) { p.classList.remove('open'); });
    if (overlay) overlay.classList.remove('open');
  }
  document.querySelectorAll('.artifacts-btn').forEach(function(btn) {
    btn.addEventListener('click', function() {
      closePanel();
      var agent = btn.getAttribute('data-agent');
      var panel = document.getElementById('panel-' + agent);
      if (panel) panel.classList.add('open');
      if (overlay) overlay.classList.add('open');
    });
  });
  if (overlay) overlay.addEventListener('click', closePanel);
  document.querySelectorAll('.artifacts-close').forEach(function(btn) {
    btn.addEventListener('click', closePanel);
  });
  document.querySelectorAll('.run-tab').forEach(function(tab) {
    tab.addEventListener('click', function() {
      var agent = tab.getAttribute('data-agent');
      var run = tab.getAttribute('data-run');
      tab.closest('.run-tabs').querySelectorAll('.run-tab').forEach(function(t) { t.classList.remove('active'); });
      tab.classList.add('active');
      tab.closest('.artifacts-panel').querySelectorAll('.run-content').forEach(function(c) {
        c.style.display = (c.getAttribute('data-run') === run) ? '' : 'none';
      });
    });
  });
})();
