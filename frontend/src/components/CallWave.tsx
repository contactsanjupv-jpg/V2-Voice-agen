"use client";

import { motion } from "framer-motion";

/**
 * The one recurring visual signature: a waveform that reads as "a call is
 * live, someone's listening." Grounded directly in the product (an AI
 * answering phone calls) rather than a generic decorative gradient blob.
 * Bar heights are fixed (not random per-render) so server/client markup
 * matches and the motion is a deliberate, tuned shape rather than noise.
 */
const BAR_HEIGHTS = [10, 22, 14, 30, 18, 26, 12, 34, 16, 24, 10, 28, 20, 14, 32, 18];

export function CallWave({
  color = "var(--color-ring)",
  className = "",
}: {
  color?: string;
  className?: string;
}) {
  return (
    <div className={`flex items-end gap-[3px] ${className}`} aria-hidden="true">
      {BAR_HEIGHTS.map((h, i) => (
        <motion.span
          key={i}
          className="w-[3px] rounded-full"
          style={{ backgroundColor: color }}
          initial={{ height: h * 0.3 }}
          animate={{ height: [h * 0.3, h, h * 0.5, h * 0.9, h * 0.3] }}
          transition={{
            duration: 1.6 + (i % 5) * 0.15,
            repeat: Infinity,
            ease: "easeInOut",
            delay: i * 0.04,
          }}
        />
      ))}
    </div>
  );
}
