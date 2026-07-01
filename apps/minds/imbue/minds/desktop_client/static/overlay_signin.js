// Sign-in overlay module. Registers the sign-in modal in the overlay host's
// registry (window.MINDS_OVERLAY_MODALS) so overlay.js renders it as in-page
// DOM: it fetches /auth/signin-modal?fragment=1, injects the panel, then calls
// this module's init(container). The host owns the backdrop dismiss / Escape;
// this module only wires the auth form and the post-auth navigation hooks that
// the sign-in modal's inline script used to set on the standalone page.
(function () {
  window.MINDS_OVERLAY_MODALS = window.MINDS_OVERLAY_MODALS || {};

  var teardownAuthForm = null;

  window.MINDS_OVERLAY_MODALS.signin = {
    // Full-window dim backdrop with a centered card (the fragment paints the
    // backdrop; the host wires click-outside-panel dismiss for this mode).
    positioning: 'backdrop',

    init: function (container) {
      // Post-auth navigations must land in the content view *behind* the overlay
      // (the create screen) and dismiss this modal -- not reload the overlay
      // host. Mirrors the standalone page's inline setup, but the standalone
      // page navigates itself instead.
      window.MINDS_AUTH_RETURN_TO = '/create';
      window.MINDS_AUTH_NAV = function (url) {
        if (window.minds && window.minds.navigateContent) window.minds.navigateContent(url);
        else window.location.href = url;
      };

      // The shared DialogCloseButton in the fragment calls dismissSigninModal();
      // route it through main so the overlay view is hidden and the DOM torn
      // down (host-owned close). Without the bridge (browser) fall back locally.
      window.dismissSigninModal = function () {
        if (window.minds && window.minds.closeModal) window.minds.closeModal();
        else window.location.href = '/create';
      };

      if (typeof window.initSigninAuthForm === 'function') {
        teardownAuthForm = window.initSigninAuthForm(container);
      }
    },

    destroy: function () {
      if (teardownAuthForm) {
        try { teardownAuthForm(); } catch (e) { /* noop */ }
        teardownAuthForm = null;
      }
    },
  };
})();
