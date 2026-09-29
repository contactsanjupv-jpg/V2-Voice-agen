"use client";

import { motion } from "framer-motion";
import { CallWave } from "./CallWave";

const TRANSCRIPT = [
  { from: "caller", text: "Hi, do you have anything open tomorrow afternoon?" },
  { from: "atla", text: "Let me check — 2:30 or 4:00 are both open tomorrow. Which works better?" },
  { from: "caller", text: "4 o'clock is perfect." },
  { from: "atla", text: "You're booked for 4:00 tomorrow. I'll text you a reminder an hour before." },
];

export function CallDemoCard() {
  return (
    <motion.div
      initial={{ opacity: 0, y: 24 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.7, ease: [0.16, 1, 0.3, 1] }}
      className="w-full max-w-sm rounded-2xl border border-[var(--color-line-dark)] bg-[var(--color-surface-dark)] p-5 shadow-2xl"
    >
      <div className="flex items-center justify-between border-b border-[var(--color-line-dark)] pb-4">
        <div className="flex items-center gap-2.5">
          <span className="h-2 w-2 rounded-full bg-[var(--color-ring)]" />
          <span className="font-[family-name:var(--font-display)] text-sm font-semibold text-[var(--color-paper)]">
            Incoming call
          </span>
        </div>
        <CallWave className="h-6" />
      </div>

      <div className="mt-4 space-y-3">
        {TRANSCRIPT.map((line, i) => (
          <motion.div
            key={i}
            initial={{ opacity: 0, x: line.from === "atla" ? 8 : -8 }}
            animate={{ opacity: 1, x: 0 }}
            transition={{ duration: 0.5, delay: 0.6 + i * 0.55, ease: "easeOut" }}
            className={`flex ${line.from === "atla" ? "justify-end" : "justify-start"}`}
          >
            <div
              className={`max-w-[85%] rounded-xl px-3.5 py-2.5 text-[13px] leading-snug ${
                line.from === "atla"
                  ? "bg-[var(--color-ring)] text-white"
                  : "bg-[#2c2f37] text-[var(--color-paper)]"
              }`}
            >
              {line.text}
            </div>
          </motion.div>
        ))}
      </div>

      <motion.div
        initial={{ opacity: 0, scale: 0.9 }}
        animate={{ opacity: 1, scale: 1 }}
        transition={{ duration: 0.4, delay: 0.6 + TRANSCRIPT.length * 0.55 + 0.3 }}
        className="mt-4 flex items-center gap-2 rounded-lg bg-[var(--color-ok)]/15 px-3 py-2 text-[13px] font-medium text-[#8fd4ac]"
      >
        <span className="h-1.5 w-1.5 rounded-full bg-[#8fd4ac]" />
        Appointment booked — added to calendar
      </motion.div>
    </motion.div>
  );
}
