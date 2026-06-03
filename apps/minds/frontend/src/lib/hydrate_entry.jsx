import { hydrate } from 'solid-js/web';

// Shared client hydration entrypoint for the three Vite bundle entries
// (app / chrome / sidebar). Each bundle's `*.entry.jsx` calls
// `hydrateRouteFromBoot(getRouteComponent)` with the matching
// per-bundle registry resolver; this helper owns the rest:
//   1. Read the `__route__` JSON payload the SSR shim inlines.
//   2. Resolve the route component via the supplied registry resolver.
//   3. Hydrate into `#app` once the DOM is ready.
//
// The function name carries "FromBoot" because the contract is "boot
// from the inlined route payload" -- not just any hydration.
export function hydrateRouteFromBoot(getRouteComponent) {
  const readBoot = () => {
    const node = document.getElementById('__route__');
    if (!node || !node.textContent) {
      throw new Error('Missing #__route__ JSON payload on page');
    }
    return JSON.parse(node.textContent);
  };
  const mount = () => {
    const { route, props } = readBoot();
    const Component = getRouteComponent(route);
    const target = document.getElementById('app');
    if (!target) {
      throw new Error('Missing #app mount target');
    }
    hydrate(() => <Component {...props} />, target);
  };
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', mount, { once: true });
  } else {
    mount();
  }
}
