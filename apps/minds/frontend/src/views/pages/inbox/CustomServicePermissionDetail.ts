// Custom-service permission dialog: store credentials for a domain securely,
// and let this machine use them, in one decision.
//
// The simplest of the kinds. No account picker -- the workspace's machine has
// no account for the service yet, whether it is being created or this computer
// already has it -- and no permission editor, since the grant is all-or-nothing
// on a scope already pinned to a single domain. Everything
// shown as fact is the domain, a URL derived from it, or fixed copy; the only
// agent-authored text is the rationale, which the shell renders under Reason.

import m from "mithril";
import type { CustomServicePermissionDetail as Detail, InboxModel } from "../../../models/inbox";
import { Disclosure } from "../../components/Disclosure";
import { Icon16 } from "../../components/Icon";
import { Notice } from "../../components/Notice";
import { PermissionsShell } from "./PermissionsShell";

/** The header line as the user reads it: the placeholder becomes the thing they paste. */
function describeCredentialHeader(header: string): string {
  return header.replaceAll("{token}", "<the token you paste>");
}

/** The one line each kind of doubtful domain gets. Written as "make sure
 * you know what this is", not "this is dangerous": a private network calls
 * its services what it likes, and a warning that fires on every legitimate
 * intranet name is one people learn to skip. */
function domainWarningText(detail: Detail): string | null {
  switch (detail.domain_warning) {
    case "unreachable":
      return `${detail.domain} is a reserved name that nothing answers to, so the agent may have the wrong address.`;
    case "local_name":
      return `${detail.domain} is a local or private name, resolved by the workspace machine's own network, so it may reach a different host on each machine. Approve only if you know what answers to it there.`;
    case "ip_address":
      return `${detail.domain} is an IP address rather than a name: whatever machine holds that address will receive the credentials.`;
    case "lookalike":
      return `${detail.domain} contains internationalized characters (the xn-- label) and may look like a different site than it is.`;
    case null:
      return null;
  }
}

export interface CustomServicePermissionDetailAttrs {
  model: InboxModel;
  detail: Detail;
}

/** The one technical detail worth a look before pasting a key: which header
 * carries it. Collapsed by default so the ordinary case stays two sentences;
 * a service that named its own header is exactly the one whose user should
 * be able to check it against the provider's docs. */
function renderHeaderDisclosure(detail: Detail, isOpen: boolean, onToggle: () => void): m.Children {
  if (detail.credential_header === null) return null;
  return m(
    Disclosure,
    { isOpen, onToggle, summary: "How this will be sent" },
    m("p", { class: "type-body text-secondary" }, [
      `Every request to ${detail.base_api_url} will carry the header `,
      m(
        "code",
        { class: "custom-service-header font-mono text-primary" },
        describeCredentialHeader(detail.credential_header),
      ),
      ".",
    ]),
  );
}

/** What is being asked and what approving does, said plainly. Framed as
 * storing credentials rather than "connecting", because that is what actually
 * happens: nothing is reached until an agent later sends a request. The origin
 * is repeated rather than referred to, so the first sentence stands on its own
 * if it is all that is read. */
function summary(detail: Detail, isHeaderOpen: boolean, onToggleHeader: () => void): m.Children {
  const lines: m.Children[] = [
    m("p", { class: "type-body text-primary" }, `The agent wants to store credentials for ${detail.base_api_url}.`),
  ];
  if (detail.is_already_registered) {
    // Another workspace connected this origin already; what is new is only
    // this workspace's own copy of the credentials.
    lines.push(
      m(
        "p",
        { class: "type-body text-secondary" },
        `This computer already has a connection to ${detail.base_api_url}; approving stores credentials for this workspace's machine as well.`,
      ),
    );
  }
  lines.push(
    m(
      "p",
      { class: "type-body text-primary" },
      "Approving this request will keep the credentials in Mind's encrypted credential store without exposing them " +
        "to the agent directly. You can always revoke the agent's access.",
    ),
  );
  // Warnings get the shell's warn callout rather than a quieter line: they
  // are the two things on this dialog a user might regret not having read.
  const warning = domainWarningText(detail);
  if (warning !== null) {
    lines.push(m(Notice, { variant: "warn", extra: "my-0" }, warning));
  }
  if (detail.base_api_url.startsWith("http:")) {
    // The one thing the scheme changes for the user: an http service receives
    // the stored credentials unencrypted, so say so where the decision is made.
    lines.push(
      m(
        Notice,
        { variant: "warn", extra: "my-0" },
        `${detail.base_api_url} is a plain http address, so the credentials will be sent to it unencrypted.`,
      ),
    );
  }
  if (detail.login_url !== null) {
    // Naming the destination matters: Approve opens a browser, and where it
    // goes should never be a surprise.
    lines.push(
      m("p", { class: "type-body text-secondary" }, [
        "You will be sent to ",
        m("span", { class: "text-primary break-all" }, detail.login_url),
        " to sign in.",
      ]),
    );
  }
  lines.push(renderHeaderDisclosure(detail, isHeaderOpen, onToggleHeader));
  return m("div", { class: "flex flex-col gap-2" }, lines);
}

export function CustomServicePermissionDetailView(): m.Component<CustomServicePermissionDetailAttrs> {
  // The request the disclosure was opened on. RequestDetail mounts this view
  // unkeyed, so one instance serves every custom-service request the user moves
  // between; a disclosure opened on one must not render open on the next.
  let openHeaderRequestId: string | null = null;
  return {
    view(vnode) {
      const { model, detail } = vnode.attrs;
      const isHeaderOpen = openHeaderRequestId === detail.request_id;
      return m(PermissionsShell, {
        model,
        // The origin is the label here: the domain everywhere else, and the
        // scheme too where the decision is made, since it is the difference
        // between credentials sent encrypted and in the clear. Neither can
        // misdescribe what the connection reaches.
        headerLabel: `Storing credentials for ${detail.base_api_url}`,
        mark: m(Icon16, { name: "globe", extra: "text-primary" }),
        rationale: detail.rationale,
        approveLabel: detail.login_url === null ? "Approve" : "Sign in & approve",
        progressLabel:
          detail.login_url === null
            ? `Storing the credentials for ${detail.domain}…`
            : `Opening a browser window for you to sign in to ${detail.domain}…`,
        body: summary(detail, isHeaderOpen, () => {
          openHeaderRequestId = isHeaderOpen ? null : detail.request_id;
        }),
      });
    },
  };
}
