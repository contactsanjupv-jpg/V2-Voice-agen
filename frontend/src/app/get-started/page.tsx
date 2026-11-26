import { Nav } from "@/components/Nav";
import { Footer } from "@/components/Footer";
import { AuthForm } from "@/components/AuthForm";
import { googleErrorMessage } from "@/lib/googleAuth";

export default async function GetStarted({ searchParams }: { searchParams: Promise<{ google_error?: string | string[] }> }) {
  const googleError = googleErrorMessage((await searchParams).google_error);
  return (
    <div className="flex min-h-screen flex-col">
      <Nav />
      <main className="flex flex-1 items-center justify-center px-6 py-16">
        <div className="w-full max-w-sm">
          <h1 className="mb-1 font-[family-name:var(--font-display)] text-[26px] font-bold tracking-tight">
            Set up your receptionist
          </h1>
          <p className="mb-8 text-[14.5px] text-[var(--color-ink-soft)]">
            Takes about ten minutes, starting with your website.
          </p>
          <AuthForm googleError={googleError} />
        </div>
      </main>
      <Footer />
    </div>
  );
}
