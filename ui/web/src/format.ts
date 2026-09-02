// Integer paise -> "₹5,898.82". No float touches a money value — this module
// only ever formats numbers that already arrived as integers; it never
// computes a total.
export function rupees(paise: number): string {
  const whole = Math.trunc(paise / 100);
  const rem = Math.abs(paise % 100);
  return `₹${whole.toLocaleString("en-IN")}.${String(rem).padStart(2, "0")}`;
}

export function shortHash(hash: string, n = 16): string {
  return hash.length > n ? `${hash.slice(0, n)}…` : hash;
}

/** "web_search" -> "Web search". Tool/agent identifiers are snake_case on the
 * wire; the console renders them as plain words. */
export function humanize(id: string): string {
  const words = id.replace(/[_-]+/g, " ").trim();
  return words.charAt(0).toUpperCase() + words.slice(1);
}

export function timeOf(tsSeconds: number): string {
  const d = new Date(tsSeconds * 1000);
  return d.toLocaleTimeString("en-IN", { hour12: false });
}

/** Masthead dateline — "TUESDAY · 2 SEPTEMBER 2026". Purely cosmetic (page
 * furniture), never used for anything the money path depends on. */
export function dateline(): string {
  const d = new Date();
  const weekday = d.toLocaleDateString("en-IN", { weekday: "long" }).toUpperCase();
  const day = d.getDate();
  const month = d.toLocaleDateString("en-IN", { month: "long" }).toUpperCase();
  const year = d.getFullYear();
  return `${weekday} · ${day} ${month} ${year}`;
}

/** A deterministic-looking "edition" tag derived from the day of year — pure
 * flavour text for the masthead, not a real record of anything. */
export function editionNumber(): string {
  const d = new Date();
  const start = new Date(d.getFullYear(), 0, 0);
  const diff = d.getTime() - start.getTime();
  const dayOfYear = Math.floor(diff / 86_400_000);
  return `VOL. I — NO. ${String(dayOfYear).padStart(3, "0")}`;
}
