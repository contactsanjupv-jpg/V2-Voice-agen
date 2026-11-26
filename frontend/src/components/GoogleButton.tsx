"use client";

import { useEffect, useState } from "react";
import { Loader2 } from "lucide-react";
import { API_BASE, api } from "@/lib/api";
import { googleStartUrl } from "@/lib/googleAuth";

/**
 * "Continue with Google". Rendered only when the backend says Google sign-in is configured, so there is never a
 * button that can't work. (Fetch failure = backend down = no button; email and password still work.)
 * No Google "G" logo is drawn here: add Google's official asset from their branding guidelines if you want it.
 */
export function GoogleButton() {
  const [available, setAvailable] = useState<boolean | null>(null);
  const [redirecting, setRedirecting] = useState(false);

  useEffect(() => {
    let live = true;
    api
      .authProviders()
      .then((p) => live && setAvailable(p.google))
      .catch(() => live && setAvailable(false));
    // Coming back with the browser's Back button restores this page from cache with the spinner still on.
    const reset = (e: PageTransitionEvent) => e.persisted && setRedirecting(false);
    window.addEventListener("pageshow", reset);
    return () => {
      live = false;
      window.removeEventListener("pageshow", reset);
    };
  }, []);

  if (available === false) return null;
  if (available === null) return <div className="h-[46px] w-full animate-pulse rounded-full bg-[var(--color-line)]" aria-hidden />;

  return (
    <>
      <a
        href={googleStartUrl(API_BASE)}
        onClick={() => setRedirecting(true)}
        aria-busy={redirecting}
        className="flex w-full items-center justify-center gap-2 rounded-full border border-[var(--color-line)] bg-[var(--color-paper-raised)] py-3 text-[15px] font-medium transition-transform hover:scale-[1.01] active:scale-[0.99]"
      >
        {redirecting && <Loader2 className="h-4 w-4 animate-spin" />}
        {redirecting ? "Taking you to Google…" : "Continue with Google"}
      </a>
      <div className="my-5 flex items-center gap-3 text-[12.5px] text-[var(--color-ink-soft)]" aria-hidden>
        <span className="h-px flex-1 bg-[var(--color-line)]" />
        or
        <span className="h-px flex-1 bg-[var(--color-line)]" />
      </div>
    </>
  );
}
