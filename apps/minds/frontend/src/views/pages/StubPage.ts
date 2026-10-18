// Placeholder page used while a route's real Mithril port lands (each page
// tranche replaces its stubs). Renders the page title and an honest notice.

import m from "mithril";
import { PageContainer } from "../components/Layout";
import { Notice } from "../components/Notice";

export function makeStubPage(title: string): m.ComponentTypes {
  return {
    view() {
      return m(
        PageContainer,
        m("div", { class: "flex flex-col gap-4 pt-10" }, [
          m("h1", { class: "type-heading-lg" }, title),
          m(Notice, { variant: "info" }, "This page is being rebuilt and is not available yet."),
        ]),
      );
    },
  };
}
