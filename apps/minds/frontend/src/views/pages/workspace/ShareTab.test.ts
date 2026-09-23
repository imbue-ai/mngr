import m from "mithril";
import { describe, expect, it } from "vitest";
import type { AnyVnode } from "../../../testing";
import {
  allText,
  attrsOf,
  classTokensOf,
  collectVnodes,
  renderRoot,
  shareModelOptions,
} from "../../../testing";
import type {
  MachineSharingResponse,
  ShareModelOptions,
} from "../../../models/workspaceOptions";
import { ShareModel } from "../../../models/workspaceOptions";
import { Icon16 } from "../../components/Icon";
import { ShareTab } from "./ShareTab";

const OWNER = "owner@example.com";
const OWNER_PICTURE = "https://pictures.example/owner.png";

/** A ready share model whose whole-machine scope carries one grantee of each
 * kind: an account with a record, an invited address, and a domain. The owner
 * has no name and no picture unless `owner` says otherwise. */
async function readyShareModel(
  owner: Partial<
    Pick<ShareModelOptions, "ownerDisplayName" | "ownerProfilePictureUrl">
  > = {},
): Promise<ShareModel> {
  const response: MachineSharingResponse = {
    enabled: true,
    url: "https://m.relay.example/",
    grants: {
      workspace: {
        users: ["user-2"],
        emails: [OWNER, "newcomer@example.com"],
        email_domains: ["example.org"],
      },
      services: {},
    },
    identities: {
      "user-2": {
        user_id: "user-2",
        email: "bob@example.com",
        display_name: "Bob",
        profile_picture_url: null,
      },
    },
  };
  const model = new ShareModel(
    shareModelOptions({
      ownerEmail: OWNER,
      appServices: [],
      serviceLabels: { system_interface: "shell-r4nd" },
      fetchJson: () =>
        Promise.resolve({ ok: true, status: 200, body: response }),
      ...owner,
    }),
  );
  await model.load();
  return model;
}

function renderTab(share: ShareModel): m.Vnode {
  return renderRoot(ShareTab, { share, workspaceName: "alpha" });
}

/** The keyed grantee rows of the editor, owner first, in render order. */
function granteeRows(root: m.Vnode): AnyVnode[] {
  const list = collectVnodes(root).find(
    (vnode) => attrsOf(vnode).id === "ws-share-emails",
  );
  expect(list).toBeDefined();
  return (list?.children as AnyVnode[]) ?? [];
}

function rowByText(root: m.Vnode, text: string): AnyVnode {
  const row = granteeRows(root).find((vnode) => allText(vnode).includes(text));
  expect(row, `no grantee row mentioning ${text}`).toBeDefined();
  return row as AnyVnode;
}

/** The 24 px lead box every grantee row starts with. */
function leadBox(row: AnyVnode): AnyVnode {
  const lead = collectVnodes(row).find((vnode) => {
    const tokens = classTokensOf(vnode);
    return (
      tokens.includes("h-6") &&
      tokens.includes("w-6") &&
      tokens.includes("rounded-full")
    );
  });
  expect(lead, "row has no 24 px lead box").toBeDefined();
  return lead as AnyVnode;
}

describe("ShareTab grantee rows", () => {
  it("renders the owner with their picture, name, email, and the (you) suffix", async () => {
    const share = await readyShareModel({
      ownerDisplayName: "Owner Person",
      ownerProfilePictureUrl: OWNER_PICTURE,
    });

    const row = rowByText(renderTab(share), "(you)");

    expect(allText(row)).toBe(`Owner Person  (${OWNER})  (you)`);
    const picture = collectVnodes(leadBox(row)).find(
      (vnode) => vnode.tag === "img",
    );
    expect(picture).toBeDefined();
    expect(attrsOf(picture as AnyVnode).src).toBe(OWNER_PICTURE);
  });

  it("falls back to the owner's monogram and email when the account has neither name nor picture", async () => {
    const share = await readyShareModel();

    const row = rowByText(renderTab(share), "(you)");

    expect(allText(row)).toContain(`${OWNER}  (you)`);
    expect(allText(row)).not.toContain(`(${OWNER})`);
    const lead = leadBox(row);
    expect(collectVnodes(lead).some((vnode) => vnode.tag === "img")).toBe(
      false,
    );
    expect(allText(lead)).toBe("O");
  });

  it("renders an account grantee with a monogram, name, and email", async () => {
    const share = await readyShareModel();

    const row = rowByText(renderTab(share), "Bob");

    expect(allText(row)).toContain("Bob  (bob@example.com)");
    expect(allText(leadBox(row))).toBe("B");
  });

  it("renders an invited address with an empty dashed placeholder and says they have not signed up", async () => {
    const share = await readyShareModel();

    const row = rowByText(renderTab(share), "newcomer@example.com");

    expect(allText(row)).toContain("(hasn't signed up yet)");
    expect(allText(row)).not.toContain("(invited)");
    const lead = leadBox(row);
    const tokens = classTokensOf(lead);
    expect(tokens).toContain("border");
    expect(tokens).toContain("border-dashed");
    expect(allText(lead)).toBe("");
  });

  it("renders a domain grantee with the globe glyph in the same lead box", async () => {
    const share = await readyShareModel();

    const row = rowByText(renderTab(share), "example.org");

    expect(allText(row)).toContain("(anyone at this domain)");
    const glyph = collectVnodes(leadBox(row)).find(
      (vnode) => vnode.tag === Icon16,
    );
    expect(glyph).toBeDefined();
    expect(attrsOf(glyph as AnyVnode).name).toBe("globe");
  });

  it("gives every grantee row the same lead box size so the text columns align", async () => {
    const share = await readyShareModel({
      ownerDisplayName: "Owner Person",
      ownerProfilePictureUrl: OWNER_PICTURE,
    });

    const rows = granteeRows(renderTab(share));

    expect(rows).toHaveLength(4);
    for (const row of rows) {
      const tokens = classTokensOf(leadBox(row));
      expect(tokens).toContain("shrink-0");
      expect(tokens).toContain("h-6");
      expect(tokens).toContain("w-6");
    }
  });
});
