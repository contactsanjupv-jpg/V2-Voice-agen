"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { Home, Mic, Phone, Settings, Users } from "lucide-react";
import { Organization } from "@/lib/api";

const NAV = [
  { href: "/dashboard", label: "Overview", icon: Home },
  { href: "/dashboard/calls", label: "Calls", icon: Phone },
  { href: "/dashboard/leads", label: "Leads", icon: Users },
  { href: "/dashboard/test", label: "Test it", icon: Mic },
  { href: "/dashboard/settings", label: "Settings", icon: Settings },
];

export function DashboardShell({
  org,
  children,
}: {
  org: Organization | null;
  children: React.ReactNode;
}) {
  const pathname = usePathname();

  return (
    <div className="flex min-h-screen">
      <aside className="hidden w-60 flex-shrink-0 flex-col border-r border-[var(--color-line)] bg-[var(--color-paper-raised)] md:flex">
        <div className="border-b border-[var(--color-line)] px-5 py-5">
          <Link href="/" className="font-[family-name:var(--font-display)] text-lg font-bold">
            Atla
          </Link>
          {org && <div className="mt-1 truncate text-[13px] text-[var(--color-ink-soft)]">{org.name}</div>}
        </div>

        <nav className="flex-1 space-y-1 px-3 py-4">
          {NAV.map(({ href, label, icon: Icon }) => {
            const active = pathname === href;
            return (
              <Link
                key={href}
                href={href}
                className={`flex items-center gap-2.5 rounded-lg px-3 py-2 text-[14px] font-medium transition-colors ${
                  active
                    ? "bg-[var(--color-ink)] text-[var(--color-paper)]"
                    : "text-[var(--color-ink-soft)] hover:bg-[var(--color-line)]"
                }`}
              >
                <Icon className="h-4 w-4" strokeWidth={1.75} />
                {label}
              </Link>
            );
          })}
        </nav>
      </aside>

      <main className="flex-1 overflow-y-auto px-6 py-8 md:px-10">{children}</main>
    </div>
  );
}

export function LoadingScreen() {
  return (
    <div className="flex min-h-screen items-center justify-center">
      <p className="text-[14px] text-[var(--color-ink-soft)]">Loading…</p>
    </div>
  );
}

export function ErrorScreen({ message }: { message: string }) {
  return (
    <div className="flex min-h-screen items-center justify-center px-6 text-center">
      <p className="text-[15px] text-[var(--color-ink-soft)]">{message}</p>
    </div>
  );
}