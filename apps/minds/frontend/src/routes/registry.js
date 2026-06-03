import { WelcomeRoute } from './welcome.jsx';
import { LoginRoute } from './login.jsx';
import { AuthErrorRoute } from './auth_error.jsx';
import { LoginRedirectRoute } from './login_redirect.jsx';
import { CreateRoute } from './create.jsx';

// Maps a "route key" (the Python side picks the key by URL) to its Solid
// component. The key is passed verbatim through the SSR sidecar HTTP
// boundary, so it lives in one shared place.
//
// As pages migrate they get added to this map. Trying to render a key
// that isn't here is a hard error -- caught by the sidecar before any
// HTML is returned.
export const ROUTES = {
  welcome: WelcomeRoute,
  login: LoginRoute,
  auth_error: AuthErrorRoute,
  login_redirect: LoginRedirectRoute,
  create: CreateRoute,
};

export function getRouteComponent(key) {
  const Component = ROUTES[key];
  if (!Component) {
    throw new Error(`Unknown route key: ${key}`);
  }
  return Component;
}
