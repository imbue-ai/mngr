import m from "mithril";
import DOMPurify from "dompurify";
import { Marked } from "marked";

const marked = new Marked({
  breaks: true,
  gfm: true,
});

const TOOL_CALL_PREFIX = "Tool call: ";

export function renderMarkdown(source: string): string {
  const rawHtml = marked.parse(source) as string;
  return DOMPurify.sanitize(rawHtml);
}

function hasToolCallLine(textContent: string): boolean {
  for (const line of textContent.split("\n")) {
    if (line.trim().startsWith(TOOL_CALL_PREFIX)) {
      return true;
    }
  }
  return false;
}

/*
 * On the backend, we use the --td argument to llm and include
 * the debug output in the stream. For each tool call, the debug
 * output starts with a line that starts with "Tool call: ".
 */
function wrapToolCallBlocks(container: HTMLElement): void {
  for (const preElement of Array.from(container.querySelectorAll("pre"))) {
    if (preElement.parentElement?.classList.contains("tool-call-block")) {
      continue;
    }
    const codeElement = preElement.querySelector("code");
    if (codeElement === null) {
      continue;
    }
    if (!hasToolCallLine(codeElement.textContent ?? "")) {
      continue;
    }

    codeElement.textContent = (codeElement.textContent ?? "").replace(/^\s*\n/, "");

    const wrapper = document.createElement("div");
    wrapper.className = "tool-call-block";
    wrapper.addEventListener("click", () => {
      wrapper.classList.toggle("tool-call-block--expanded");
    });

    preElement.replaceWith(wrapper);
    wrapper.appendChild(preElement);
  }
}

function saveExpandedState(container: HTMLElement): Set<number> {
  const expanded = new Set<number>();
  const blocks = container.querySelectorAll(".tool-call-block");
  blocks.forEach((block, index) => {
    if (block.classList.contains("tool-call-block--expanded")) {
      expanded.add(index);
    }
  });
  return expanded;
}

function restoreExpandedState(container: HTMLElement, expanded: Set<number>): void {
  const blocks = container.querySelectorAll(".tool-call-block");
  blocks.forEach((block, index) => {
    if (expanded.has(index)) {
      block.classList.add("tool-call-block--expanded");
    }
  });
}

export const MarkdownContent: m.Component<{ content: string }> = {
  oncreate(vnode) {
    const element = vnode.dom as HTMLElement;
    element.innerHTML = renderMarkdown(vnode.attrs.content);
    wrapToolCallBlocks(element);
  },
  onupdate(vnode) {
    const element = vnode.dom as HTMLElement;
    const expanded = saveExpandedState(element);
    element.innerHTML = renderMarkdown(vnode.attrs.content);
    wrapToolCallBlocks(element);
    restoreExpandedState(element, expanded);
  },
  view() {
    return m("div", { class: "message-content markdown-content" });
  },
};
