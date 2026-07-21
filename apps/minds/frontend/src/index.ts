// Bundle entry. esbuild compiles this tree to a single IIFE classic script
// (static/dist/chrome.bundle.js) exposing the window.MindsUI namespace of
// mount functions. IIFE + classic script tags is deliberate: the chrome.js
// swap engine re-executes page scripts by re-creating tags, and classic
// scripts have synchronous, ordered execution semantics there; ES modules do
// not. The bundle itself is loaded once per document from the SHELL scripts
// section (never from #local-page-scripts, which would re-run it per swap);
// the per-page mount calls live in #local-page-scripts.
import { mountLanding } from "./views/LandingPage";
import { mountStyleguidePrimitives, mountStyleguideWorkspaceRows } from "./views/StyleguideRows";
import { mountStyleguideSmoke } from "./views/StyleguideSmoke";
import { mountWorkspaceMenu, setWorkspaceMenuCurrentAgent } from "./views/WorkspaceMenu";

export interface MindsUINamespace {
  mountLanding: typeof mountLanding;
  mountStyleguideSmoke: typeof mountStyleguideSmoke;
  mountStyleguidePrimitives: typeof mountStyleguidePrimitives;
  mountStyleguideWorkspaceRows: typeof mountStyleguideWorkspaceRows;
  mountWorkspaceMenu: typeof mountWorkspaceMenu;
  setWorkspaceMenuCurrentAgent: typeof setWorkspaceMenuCurrentAgent;
}

declare global {
  interface Window {
    MindsUI: MindsUINamespace;
  }
}

window.MindsUI = {
  mountLanding,
  mountStyleguideSmoke,
  mountStyleguidePrimitives,
  mountStyleguideWorkspaceRows,
  mountWorkspaceMenu,
  setWorkspaceMenuCurrentAgent,
};
