import m from "mithril";

export function getSelectedAgentId(): string | null {
  const attrs = m.route.param("agentId");
  return attrs ?? null;
}

export function selectAgent(agentId: string): void {
  m.route.set("/agents/:agentId", { agentId });
}
