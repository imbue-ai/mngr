// Bundle entry. esbuild compiles this tree to a single IIFE classic script
// (static/dist/chrome.bundle.js) exposing the window.MindsUI namespace of
// mount functions. IIFE + classic script tags is deliberate: the chrome.js
// swap engine re-executes page scripts by re-creating tags, and classic
// scripts have synchronous, ordered execution semantics there; ES modules do
// not. The bundle itself is loaded once per document from the SHELL scripts
// section (never from #local-page-scripts, which would re-run it per swap);
// the per-page mount calls live in #local-page-scripts.
import { setAccentScopeAgentId, setContentUrl, setDisplayedWorkspaceAgentId } from "./store";
import { mountAuthError, mountLoginPrompt } from "./views/AuthTextPages";
import { mountConsent } from "./views/ConsentPage";
import { mountCreating } from "./views/CreatingPage";
import { mountDestroying } from "./views/DestroyingPage";
import { mountInboxList } from "./views/InboxList";
import { mountLanding } from "./views/LandingPage";
import { adoptParentModalBridge, mountModalHost } from "./views/ModalHost";
import { mountSharingEditor } from "./views/SharingEditor";
import { mountStyleguidePrimitives, mountStyleguideWorkspaceRows } from "./views/StyleguideRows";
import { mountStyleguideSmoke } from "./views/StyleguideSmoke";
import { mountTitleBar } from "./views/TitleBar";
import { mountWelcome } from "./views/WelcomePage";
import { mountWorkspaceMenu } from "./views/WorkspaceMenu";

export interface MindsUINamespace {
  mountAuthError: typeof mountAuthError;
  mountConsent: typeof mountConsent;
  mountCreating: typeof mountCreating;
  mountDestroying: typeof mountDestroying;
  mountInboxList: typeof mountInboxList;
  mountLanding: typeof mountLanding;
  mountModalHost: typeof mountModalHost;
  mountSharingEditor: typeof mountSharingEditor;
  mountStyleguideSmoke: typeof mountStyleguideSmoke;
  mountStyleguidePrimitives: typeof mountStyleguidePrimitives;
  mountStyleguideWorkspaceRows: typeof mountStyleguideWorkspaceRows;
  mountLoginPrompt: typeof mountLoginPrompt;
  mountTitleBar: typeof mountTitleBar;
  mountWelcome: typeof mountWelcome;
  mountWorkspaceMenu: typeof mountWorkspaceMenu;
  // chrome.js's browser-mode pushes: the content URL (crumb derivation), the
  // accent-scope workspace (accent + menu highlight), and the displayed
  // workspace (help-button assist gating).
  setContentUrl: typeof setContentUrl;
  setAccentScopeAgent: typeof setAccentScopeAgentId;
  setDisplayedWorkspaceAgent: typeof setDisplayedWorkspaceAgentId;
}

declare global {
  interface Window {
    MindsUI: MindsUINamespace;
  }
}

// When this bundle loads inside a browser-mode modal-host iframe, adopt the
// parent's window.minds-compatible bridge BEFORE any page script runs, so the
// modal pages' Electron code paths drive the in-document modal layer.
adoptParentModalBridge();

window.MindsUI = {
  mountAuthError,
  mountConsent,
  mountCreating,
  mountDestroying,
  mountInboxList,
  mountLanding,
  mountModalHost,
  mountSharingEditor,
  mountStyleguideSmoke,
  mountStyleguidePrimitives,
  mountStyleguideWorkspaceRows,
  mountLoginPrompt,
  mountTitleBar,
  mountWelcome,
  mountWorkspaceMenu,
  setContentUrl,
  setAccentScopeAgent: setAccentScopeAgentId,
  setDisplayedWorkspaceAgent: setDisplayedWorkspaceAgentId,
};
