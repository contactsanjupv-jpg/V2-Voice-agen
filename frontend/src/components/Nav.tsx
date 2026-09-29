"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";

const LINKS = [
  { href: "/how-it-works", label: "How it works" },
  { href: "/industries", label: "Industries" },
  { href: "/pricing", label: "Pricing" },
];

export function Nav() {
  const pathname = usePathname();

  return (
    <header className="sticky top-0 z-50 border-b border-[var(--color-line)] bg-[var(--color-paper)]/90 backdrop-blur">
      <div className="mx-auto flex max-w-6xl items-center justify-between px-6 py-4">
        <Link href="/" className="font-[family-name:var(--font-display)] text-xl font-bold tracking-tight">
          Atla
        </Link>

        <nav className="hidden items-center gap-8 md:flex">
          {LINKS.map((link) => {
            const active = pathname === link.href;
            return (
              <Link
                key={link.href}
                href={link.href}
                className={`relative text-[15px] transition-colors ${
                  active ? "text-[var(--color-ink)]" : "text-[var(--color-ink-soft)] hover:text-[var(--color-ink)]"
                }`}
              >
                {link.label}
                {active && (
                  <span className="absolute -bottom-[17px] left-0 right-0 h-[2px] bg-[var(--color-ring)]" />
                )}
              </Link>
            );
          })}
        </nav>

        <Link
          href="/get-started"
          className="rounded-full bg-[var(--color-ink)] px-5 py-2.5 text-[14px] font-medium text-[var(--color-paper)] transition-transform hover:scale-[1.03] active:scale-[0.98]"
        >
          Get started
        </Link>
      </div>
    </header>
  );
}
