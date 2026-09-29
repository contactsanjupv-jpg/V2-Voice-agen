import Link from "next/link";

export function Footer() {
  return (
    <footer className="border-t border-[var(--color-line)] bg-[var(--color-paper)]">
      <div className="mx-auto max-w-6xl px-6 py-12">
        <div className="flex flex-col gap-8 md:flex-row md:items-start md:justify-between">
          <div>
            <div className="font-[family-name:var(--font-display)] text-lg font-bold">Atla</div>
            <p className="mt-2 max-w-xs text-sm text-[var(--color-ink-soft)]">
              An AI receptionist that answers every call, books appointments, and never puts a caller on hold.
            </p>
          </div>

          <div className="flex gap-16">
            <div>
              <div className="mb-3 text-sm font-medium">Product</div>
              <ul className="space-y-2 text-sm text-[var(--color-ink-soft)]">
                <li><Link href="/how-it-works" className="hover:text-[var(--color-ink)]">How it works</Link></li>
                <li><Link href="/pricing" className="hover:text-[var(--color-ink)]">Pricing</Link></li>
                <li><Link href="/industries" className="hover:text-[var(--color-ink)]">Industries</Link></li>
              </ul>
            </div>
            <div>
              <div className="mb-3 text-sm font-medium">Get started</div>
              <ul className="space-y-2 text-sm text-[var(--color-ink-soft)]">
                <li><Link href="/get-started" className="hover:text-[var(--color-ink)]">Create account</Link></li>
              </ul>
            </div>
          </div>
        </div>

        <div className="mt-12 border-t border-[var(--color-line)] pt-6 text-xs text-[var(--color-ink-soft)]">
          © {new Date().getFullYear()} Atla.
        </div>
      </div>
    </footer>
  );
}
