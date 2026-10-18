/**
 * The two notices the create surfaces share: the alpha callout for the cloud
 * connection dialog, and the slow-start warning for any compute that boots a
 * machine from scratch.
 */
import m from "mithril";
import { Link } from "../../components/Link";
import { Notice } from "../../components/Notice";

export const COMMUNITY_DISCORD_URL = "https://discord.com/invite/pynvT9nJm";

export const ALPHA_FEATURE_TITLE = "This is an alpha feature";

export const SLOW_START_WARNING =
  "It can take a while for the workspace to start on this provider; be prepared to wait at least 10 minutes.";

/** The alpha callout at the top of the connection dialog. */
export function alphaFeatureNotice(id: string): m.Children {
  return m(Notice, { variant: "info", id, extra: "mt-3" }, [
    m("p", { class: "type-label text-primary" }, ALPHA_FEATURE_TITLE),
    m("p", { class: "mt-1 type-helper text-primary" }, [
      "We are still testing launching remote machines on these cloud providers. If you try it, please tell us how it went, good or bad, on ",
      m(Link, { href: COMMUNITY_DISCORD_URL, target: "_blank", rel: "noopener" }, "our Discord"),
      ".",
    ]),
  ]);
}

/** The slow-start warning under a create form whose compute is not a leased Imbue Cloud host. */
export function slowStartNotice(id: string, isShown: boolean): m.Children {
  if (!isShown) return null;
  return m(Notice, { variant: "warn", id }, SLOW_START_WARNING);
}
