// Static placeholder page shown when the user lands without a valid
// session cookie. Mirrors templates/login.html: tells the user to use
// the login URL printed in their terminal.
export function LoginRoute() {
  return (
    <div class="bg-zinc-50 text-zinc-900 font-sans antialiased flex items-center justify-center min-h-screen">
      <div class="bg-white border border-zinc-200 rounded-xl shadow-sm p-8 max-w-[420px] w-full m-4">
        <h1 class="text-xl font-semibold text-zinc-900 leading-tight">Sign in to Minds</h1>
        <p class="text-xs text-zinc-400 mt-1.5 mb-5">
          Use the login URL printed in the terminal.
        </p>
        <p class="text-zinc-600 leading-relaxed">
          Each login URL can only be used once. If you've already used yours, restart the server to
          generate a new one.
        </p>
      </div>
    </div>
  );
}
