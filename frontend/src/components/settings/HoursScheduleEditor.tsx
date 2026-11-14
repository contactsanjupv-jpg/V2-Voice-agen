"use client";

import { useState } from "react";
import {
  DAYS, DAY_LABEL, MAX_WINDOWS_PER_DAY, defaultSchedule, scheduleProblem, setDayWindows, setException,
  type Day, type HoursSchedule, type Window,
} from "@/lib/hoursSchedule";
import { BTN_GHOST, BTN_LINK, INPUT } from "./ui";

function WindowRow({ w, onChange, onRemove }: { w: Window; onChange: (w: Window) => void; onRemove: () => void }) {
  return (
    <div className="flex items-center gap-2">
      <input type="time" className={INPUT} value={w[0]} onChange={(e) => onChange([e.target.value, w[1]])} aria-label="Opens" />
      <span className="text-sm">to</span>
      <input type="time" className={INPUT} value={w[1]} onChange={(e) => onChange([w[0], e.target.value])} aria-label="Closes" />
      <button type="button" className={BTN_LINK} onClick={onRemove}>Remove</button>
    </div>
  );
}

function Windows({ ws, onChange }: { ws: Window[]; onChange: (ws: Window[]) => void }) {
  return (
    <div className="space-y-2">
      {ws.map((w, i) => (
        <WindowRow key={i} w={w} onChange={(n) => onChange(ws.map((x, j) => (j === i ? n : x)))} onRemove={() => onChange(ws.filter((_, j) => j !== i))} />
      ))}
      {ws.length < MAX_WINDOWS_PER_DAY && (
        <button type="button" className={BTN_LINK} onClick={() => onChange([...ws, ["09:00", "17:00"]])}>
          Add hours
        </button>
      )}
    </div>
  );
}

/** Weekly opening hours plus special dates. Closed or unclear = the receptionist cannot transfer calls. */
export function HoursScheduleEditor({ value, onChange }: { value: HoursSchedule | null; onChange: (v: HoursSchedule | null) => void }) {
  const [newDate, setNewDate] = useState("");
  const on = !!value?.enabled;
  const problem = scheduleProblem(value);

  return (
    <div className="space-y-4">
      <label className="flex items-start gap-2 text-sm">
        <input
          type="checkbox"
          checked={on}
          onChange={(e) => onChange(e.target.checked ? { ...(value ?? defaultSchedule()), enabled: true } : value ? { ...value, enabled: false } : null)}
        />
        <span>
          Handle calls differently outside my opening hours. After hours (or if hours can&apos;t be confirmed) the receptionist
          takes a message and never transfers the call. Your timezone above is used.
        </span>
      </label>

      {value && on && (
        <>
          <div className="space-y-3">
            {DAYS.map((d: Day) => (
              <div key={d} className="grid grid-cols-1 gap-1 sm:grid-cols-[8rem_1fr]">
                <div className="text-sm font-medium">{DAY_LABEL[d]}</div>
                <div>
                  {(value.weekly[d] ?? []).length === 0 && <p className="text-sm text-[var(--color-muted)]">Closed</p>}
                  <Windows ws={value.weekly[d] ?? []} onChange={(ws) => onChange(setDayWindows(value, d, ws))} />
                </div>
              </div>
            ))}
          </div>

          <div className="space-y-2">
            <div className="text-sm font-medium">Holidays &amp; special days</div>
            {Object.entries(value.exceptions).sort().map(([date, ws]) => (
              <div key={date} className="grid grid-cols-1 gap-1 sm:grid-cols-[8rem_1fr]">
                <div className="text-sm">{date}</div>
                <div className="space-y-1">
                  {ws.length === 0 && <p className="text-sm text-[var(--color-muted)]">Closed all day</p>}
                  <Windows ws={ws} onChange={(n) => onChange(setException(value, date, n))} />
                  <button type="button" className={BTN_LINK} onClick={() => onChange(setException(value, date, null))}>Remove this date</button>
                </div>
              </div>
            ))}
            <div className="flex items-center gap-2">
              <input type="date" className={INPUT} value={newDate} onChange={(e) => setNewDate(e.target.value)} aria-label="Date" />
              <button
                type="button"
                className={BTN_GHOST}
                disabled={!newDate || newDate in value.exceptions}
                onClick={() => { onChange(setException(value, newDate, [])); setNewDate(""); }}
              >
                Close this day
              </button>
            </div>
          </div>
        </>
      )}
      {problem && <p role="alert" className="text-sm text-[var(--color-danger,#b42318)]">{problem}</p>}
    </div>
  );
}
