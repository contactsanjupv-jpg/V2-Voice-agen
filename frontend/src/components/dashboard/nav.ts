import { AudioLines, BookOpen, ChartColumn, Home, Phone, Settings, Users, type LucideIcon } from "lucide-react";

export interface NavItem {
  href: string;
  label: string;
  icon: LucideIcon;
  /**
   * An item is shown only once its page exists. Receptionist, Knowledge and
   * Analytics are built in later levels; each of those flips its own flag to
   * true — until then there is no link to a page that isn't there.
   */
  enabled: boolean;
  mobileTab: boolean; // true: bottom tab bar on phones; false: lives in the menu sheet
  exact?: boolean;
}

export const NAV: NavItem[] = [
  { href: "/dashboard", label: "Home", icon: Home, enabled: true, mobileTab: true, exact: true },
  { href: "/dashboard/receptionist", label: "Receptionist", icon: AudioLines, enabled: false, mobileTab: true },
  { href: "/dashboard/calls", label: "Calls", icon: Phone, enabled: true, mobileTab: true },
  { href: "/dashboard/leads", label: "Leads", icon: Users, enabled: true, mobileTab: true },
  { href: "/dashboard/knowledge", label: "Knowledge", icon: BookOpen, enabled: false, mobileTab: false },
  { href: "/dashboard/analytics", label: "Analytics", icon: ChartColumn, enabled: false, mobileTab: false },
  { href: "/dashboard/settings", label: "Settings", icon: Settings, enabled: true, mobileTab: true },
];

export function isActive(item: NavItem, pathname: string): boolean {
  return item.exact ? pathname === item.href : pathname === item.href || pathname.startsWith(`${item.href}/`);
}