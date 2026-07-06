// Sign-in modal adapter, single-sourced for both contexts (auth.js holds the
// shared form logic and auto-runs on any page carrying the form):
//   * Electron overlay: registered in window.MINDS_OVERLAY_MODALS so overlay.js
//     injects the ?fragment=1 markup and calls init(container), which sets the
//     post-auth nav hooks and initializes the auth form. The host owns the
//     backdrop click-outside dismiss.
//   * Standalone browser page (/auth/signin-modal): auth.js already auto-runs the
//     form; this file (also loaded there) auto-sets the same nav hooks and wires
//     its own backdrop dismiss, since there is no overlay host.
//
// The nav hooks tell auth.js where to land after sign-in: the create screen,
// reached via the content view behind the overlay (Electron) or a normal
// navigation (browser). The shared DialogCloseButton calls dismissSigninModal().
(function () {
  window.MINDS_OVERLAY_MODALS = window.MINDS_OVERLAY_MODALS || {};

  function setAuthNavHooks() {
    window.MINDS_AUTH_RETURN_TO = '/create';
    window.MINDS_AUTH_NAV = function (url) {
      if (window.minds && window.minds.navigateContent) window.minds.navigateContent(url);
      else window.location.href = url;
    };
    window.dismissSigninModal = function () {
      if (window.minds && window.minds.closeModal) window.minds.closeModal();
      else window.location.href = '/create';
    };
  }
  function clearAuthNavHooks() {
    delete window.MINDS_AUTH_RETURN_TO;
    delete window.MINDS_AUTH_NAV;
    delete window.dismissSigninModal;
  }

  // Electron overlay registration.
  var teardown = null;
  window.MINDS_OVERLAY_MODALS.signin = {
    positioning: 'backdrop',
    init: function (container) {
      setAuthNavHooks();
      if (typeof window.initSigninAuthForm === 'function') teardown = window.initSigninAuthForm(container);
    },
    destroy: function () {
      if (teardown) { try { teardown(); } catch (error) { /* noop */ } teardown = null; }
      clearAuthNavHooks();
    },
  };

  // Standalone browser page: auth.js already auto-ran the form, so only add the
  // modal's nav hooks + its own backdrop dismiss (no overlay host to own it).
  // No-op in the overlay host: the backdrop isn't present at host load.
  var backdrop = document.getElementById('signin-modal-backdrop');
  if (backdrop) {
    setAuthNavHooks();
    backdrop.addEventListener('click', function (event) {
      if (event.target === backdrop) window.dismissSigninModal();
    });
  }
})();
