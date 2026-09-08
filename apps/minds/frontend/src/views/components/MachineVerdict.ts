import m from "mithril";

/** A machine's verbatim refusal, rendered under the sentence that introduces it.
 *
 * Set apart from the surrounding copy on purpose: this is the machine's own
 * output rather than ours, and it is the part worth copying into a bug report.
 * Keeps its line breaks (an mngr verdict carries its hint on the lines after
 * it) and breaks anywhere, because a config key or a path offers no break
 * opportunity and would otherwise size the box past its container.
 *
 * Renders nothing without a verdict, so callers can pass one unconditionally
 * rather than guarding at every site.
 */
export function machineVerdict(detail: string): m.Children {
  if (!detail) return null;
  return m(
    "pre",
    {
      class:
        "mt-2 type-helper font-mono bg-fill-hover rounded-md p-2 max-h-48 " +
        "overflow-y-auto whitespace-pre-wrap wrap-anywhere",
    },
    detail,
  );
}
