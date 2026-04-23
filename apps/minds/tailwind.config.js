/** @type {import('tailwindcss').Config} */
// Tailwind scans the templates under desktop_client/templates/ and emits the
// utilities they reference -- including arbitrary values like h-[38px] and
// responsive / state variants like hover:bg-zinc-800. Output is small
// (~30-100KB) and the build finishes in 1-2s.
//
// We tried `safelist: [{ pattern: /./ }]` to emit the entire utility set
// without any content dependency, but that explodes into the cartesian
// product of every utility with every variant and takes 5+ minutes to
// compile. The content-scan-only approach is the standard Tailwind flow
// and is plenty fast; the tradeoff is that adding a class to a template
// requires a rebuild (run `just minds-tailwind` again).
module.exports = {
  content: ['./imbue/minds/desktop_client/templates/**/*.html'],
  theme: {
    extend: {},
  },
  plugins: [],
};
