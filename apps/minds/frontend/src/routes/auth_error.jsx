// Failure-state cousin of LoginRoute. `message` is the human-readable
// description of why the login attempt failed (e.g. "This login code is
// invalid or has already been used.").
export function AuthErrorRoute(props) {
  return (
    <div class="bg-zinc-50 text-zinc-900 font-sans antialiased flex items-center justify-center min-h-screen">
      <div class="bg-white border border-zinc-200 rounded-xl shadow-sm p-8 max-w-[460px] w-full m-4">
        <h1 class="text-xl font-semibold text-zinc-900 leading-tight">Authentication Failed</h1>
        <p class="mt-2 text-zinc-600">{props.message}</p>
        <p class="mt-2 text-zinc-600">
          Each login URL can only be used once. Please use the login URL printed in the terminal
          where the server is running, or restart the server to generate a new one.
        </p>
      </div>
    </div>
  );
}
