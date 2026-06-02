import { onMount } from 'solid-js';

// JS-only redirect to /authenticate. We delay the navigation until
// after hydration so Chromium's pre-render heuristics don't consume the
// one-time code before the user actually clicks the link.
//
// The one_time_code originates as an unvalidated query parameter, so it
// MUST be passed in as a Solid prop (not interpolated into a string
// literal) and encoded with encodeURIComponent at navigation time.
export function LoginRedirectRoute(props) {
  onMount(() => {
    const code = props.one_time_code;
    if (typeof code !== 'string') return;
    window.location.href = '/authenticate?one_time_code=' + encodeURIComponent(code);
  });
  // The surrounding document <body> (with its Tailwind background/typography
  // classes) is supplied by the SSR shell in server.jsx / the
  // _client_render_shell in Python; the route only owns the in-app content.
  return (
    <div>
      <p>Authenticating...</p>
    </div>
  );
}
