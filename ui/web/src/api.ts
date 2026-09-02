// The ONLY interface to the backend (scratchpad/day3/EVENT_SCHEMA.md). We POST
// (not EventSource, since the body carries the request) and read the response
// as a raw byte stream: buffer chunks, split on the blank-line frame delimiter,
// strip the "data: " prefix, JSON.parse each frame into one event.
import type { AppEvent, RunMode } from "./types";

export interface RunRequest {
  request: string;
  budget_rupees: number;
  mode: RunMode;
}

export interface StreamHandlers {
  onEvent(event: AppEvent): void;
  onError(message: string): void;
  onDone(): void;
}

const FRAME_PREFIX = "data: ";

/** Split a growing text buffer on the SSE frame delimiter and parse each
 * complete frame. Returns the leftover (possibly-partial) tail. */
function drainFrames(buffer: string, onEvent: (event: AppEvent) => void): string {
  const frames = buffer.split("\n\n");
  const tail = frames.pop() ?? "";
  for (const frame of frames) {
    const line = frame.trim();
    if (!line) continue;
    const payload = line.startsWith(FRAME_PREFIX) ? line.slice(FRAME_PREFIX.length) : line;
    if (!payload) continue;
    try {
      onEvent(JSON.parse(payload) as AppEvent);
    } catch {
      // A malformed frame is a backend bug, not a reason to kill the whole
      // stream — skip it and keep reading.
    }
  }
  return tail;
}

export async function runAgent(body: RunRequest, handlers: StreamHandlers): Promise<void> {
  let res: Response;
  try {
    res = await fetch("/api/run", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
  } catch {
    handlers.onError("Could not reach the server. Check the connection and try again.");
    return;
  }

  if (!res.ok || !res.body) {
    handlers.onError(`Server responded with ${res.status}.`);
    return;
  }

  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  try {
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      buffer = drainFrames(buffer, handlers.onEvent);
    }
    buffer += decoder.decode();
    drainFrames(buffer + "\n\n", handlers.onEvent);
    handlers.onDone();
  } catch {
    handlers.onError("The stream dropped before the run finished.");
  }
}

export async function resetLedger(): Promise<boolean> {
  try {
    const res = await fetch("/api/reset", { method: "POST" });
    const data = await res.json();
    return Boolean(data.ok);
  } catch {
    return false;
  }
}
