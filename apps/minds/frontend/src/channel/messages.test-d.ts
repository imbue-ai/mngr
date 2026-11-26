// Type-level tests for the frame the client sends. vitest transpiles
// TypeScript without typechecking it, so these run under `pnpm run check`:
// tsc fails on a `@ts-expect-error` whose line stops being an error.

import type { UiClientMessage } from "./messages";

// Declared rather than spelled out, so a field the server's model gains does
// not have to be repeated here.
declare const valid: UiClientMessage;

const { window_kind: _windowKind, ...withoutWindowKind } = valid;

export const rejectedFrames: UiClientMessage[] = [
  {
    ...valid,
    // @ts-expect-error -- the server's model has no such field
    not_a_field: true,
  },
  {
    ...valid,
    // @ts-expect-error -- not a UiWindowKind
    window_kind: "sidebar",
  },
  {
    ...valid,
    // @ts-expect-error -- has_focus is a boolean
    has_focus: "yes",
  },
  // @ts-expect-error -- the client sends every field, so none may be dropped
  withoutWindowKind,
];
