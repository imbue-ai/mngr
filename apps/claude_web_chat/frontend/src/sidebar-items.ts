/**
 * Sidebar item registration. Plugins call $llm.registerSidebarItem() to add
 * navigation items between the branding row and the "New conversation" row.
 */

export interface SidebarItemDefinition {
  readonly name: string;
  readonly icon: string;
  readonly route: string;
}

const registeredSidebarItems: SidebarItemDefinition[] = [];

export function registerSidebarItem(definition: SidebarItemDefinition): void {
  registeredSidebarItems.push(definition);
}

export function getSidebarItems(): readonly SidebarItemDefinition[] {
  return registeredSidebarItems;
}
