Review follow-up to the Imbue Studio prose sweep.

`test/e2e/fixtures.js` called the same running app "Imbue Studio.app" twice in
its header and then "a prior Minds" mid-sentence; it now reads Imbue Studio
throughout. This project's sweep entry also now accounts for that file and for
`scripts/build.js`'s module header, which it had left out.
