"use client";

import { useEffect, useRef, useState } from "react";
import { Pause, Play, Search } from "lucide-react";
import { api, Voice } from "@/lib/api";
import { WizardActions, ErrorBanner } from "./WizardShell";

export function StepVoice({
  onSelected,
  onBack,
}: {
  onSelected: (voiceId: string) => void;
  onBack: () => void;
}) {
  const [voices, setVoices] = useState<Voice[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [search, setSearch] = useState("");
  const [selected, setSelected] = useState<string | null>(null);
  const [playingId, setPlayingId] = useState<string | null>(null);

  // Keep exactly one audio element for the entire component.
  const audioRef = useRef<HTMLAudioElement | null>(null);

  useEffect(() => {
    api
      .listVoices()
      .then(setVoices)
      .catch(() => setError("We couldn't load the voices just now. Please refresh in a moment."))
      .finally(() => setLoading(false));

    // Stop and clean up any preview when leaving this step.
    return () => {
      const audio = audioRef.current;

      if (audio) {
        audio.pause();
        audio.removeAttribute("src");
        audio.load();
      }

      audioRef.current = null;
    };
  }, []);

  const filtered = voices.filter((v) =>
    v.name.toLowerCase().includes(search.toLowerCase())
  );

  function getSafePreviewUrl(previewUrl: string): string | null {
    try {
      const url = new URL(previewUrl);

      // Only allow HTTPS preview URLs.
      // This is a client-side safety check, not a substitute for
      // backend validation of the voice catalog.
      if (url.protocol !== "https:") {
        return null;
      }

      return url.toString();
    } catch {
      return null;
    }
  }

  function stopCurrentAudio() {
    const audio = audioRef.current;

    if (!audio) {
      return;
    }

    audio.pause();
    audio.currentTime = 0;
    audio.removeAttribute("src");
    audio.load();

    audioRef.current = null;
    setPlayingId(null);
  }

  function preview(voice: Voice) {
    if (!voice.preview_url) {
      return;
    }

    const previewUrl = getSafePreviewUrl(voice.preview_url);

    if (!previewUrl) {
      return;
    }

    const voiceId = voice.retell_voice_id;

    // Clicking the currently playing voice pauses/stops it.
    if (playingId === voiceId && audioRef.current) {
      stopCurrentAudio();
      return;
    }

    // Always stop the previous preview before starting another.
    stopCurrentAudio();

    const audio = new Audio();

    // Do not allow an old audio instance to affect the current one.
    audioRef.current = audio;

    audio.preload = "auto";
    audio.src = previewUrl;

    audio.onplay = () => {
      if (audioRef.current === audio) {
        setPlayingId(voiceId);
      }
    };

    audio.onended = () => {
      if (audioRef.current === audio) {
        audioRef.current = null;
        setPlayingId(null);
      }
    };

    audio.onerror = () => {
      if (audioRef.current === audio) {
        audioRef.current = null;
        setPlayingId(null);
      }
    };

    void audio.play().catch(() => {
      if (audioRef.current === audio) {
        audioRef.current = null;
        setPlayingId(null);
      }
    });
  }

  return (
    <div>
      <h1 className="font-[family-name:var(--font-display)] text-[26px] font-bold tracking-tight">Choose a voice</h1>
      <div className="mt-1.5 inline-flex items-center gap-1.5 rounded-full bg-[var(--color-line)] px-2.5 py-1 text-[11.5px] font-medium text-[var(--color-ink-soft)]">
        Voices powered by Retell AI
      </div>
      <p className="mt-3 text-[15px] text-[var(--color-ink-soft)]">
        Preview a few and pick the one that fits your business. Previews are generic sample clips —
        your actual receptionist will introduce itself using your business name.
      </p>

      <div className="mt-6">
        {error && <ErrorBanner message={error} />}

        <div className="mb-4 flex items-center gap-2 rounded-lg border border-[var(--color-line)] bg-[var(--color-paper-raised)] px-3.5 py-2.5">
          <Search className="h-4 w-4 text-[var(--color-ink-soft)]" />

          <input
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            placeholder="Search voices…"
            className="w-full bg-transparent text-[14px] outline-none"
          />
        </div>

        {loading && (
          <p className="text-[14px] text-[var(--color-ink-soft)]">
            Loading voices…
          </p>
        )}

        {!loading && !error && filtered.length === 0 && (
          <p className="text-[14px] text-[var(--color-ink-soft)]">
            No voices in the catalog yet.
          </p>
        )}

        <div className="max-h-[360px] space-y-2 overflow-y-auto">
          {filtered.map((voice) => {
            const isPlaying = playingId === voice.retell_voice_id;

            return (
              <button
                key={voice.id}
                type="button"
                onClick={() => setSelected(voice.id)}
                className={`flex w-full items-center justify-between rounded-xl border px-4 py-3 text-left transition-colors ${
                  selected === voice.id
                    ? "border-[var(--color-ink)] bg-[var(--color-paper-raised)]"
                    : "border-[var(--color-line)] hover:bg-[var(--color-paper-raised)]"
                }`}
              >
                <div>
                  <div className="text-[14.5px] font-medium">
                    {voice.name}
                  </div>

                  <div className="text-[13px] text-[var(--color-ink-soft)]">
                    {[voice.accent, voice.gender, voice.age_style]
                      .filter(Boolean)
                      .join(" · ")}
                  </div>
                </div>

                {voice.preview_url && (
                  <span
                    role="button"
                    tabIndex={0}
                    aria-label={
                      isPlaying
                        ? `Pause ${voice.name} preview`
                        : `Play ${voice.name} preview`
                    }
                    onClick={(e) => {
                      e.stopPropagation();
                      preview(voice);
                    }}
                    onKeyDown={(e) => {
                      if (e.key === "Enter" || e.key === " ") {
                        e.preventDefault();
                        e.stopPropagation();
                        preview(voice);
                      }
                    }}
                    className="flex h-8 w-8 shrink-0 items-center justify-center rounded-full bg-[var(--color-line)] hover:bg-[var(--color-ink)] hover:text-[var(--color-paper)]"
                  >
                    {isPlaying ? (
                      <Pause className="h-3.5 w-3.5" />
                    ) : (
                      <Play className="h-3.5 w-3.5" />
                    )}
                  </span>
                )}
              </button>
            );
          })}
        </div>
      </div>

      <WizardActions
        onBack={onBack}
        onNext={() => selected && onSelected(selected)}
        nextDisabled={!selected}
      />
    </div>
  );
}