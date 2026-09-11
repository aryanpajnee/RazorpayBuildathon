// Integer paise -> "₹5,898.82".
//
// No float ever touches a money value. These functions only format numbers that
// already arrived from the server as integers; nothing here computes, sums, or
// converts a total. Every rendered figure is wrapped in a tabular-numeral class
// so columns of money line up digit-for-digit.
export function rupees(paise: number): string {
  if (!Number.isFinite(paise)) return "—";
  const int = Math.trunc(paise);
  const sign = int < 0 ? "-" : "";
  const abs = Math.abs(int);
  const whole = Math.trunc(abs / 100);
  const rem = abs % 100;
  return `${sign}₹${whole.toLocaleString("en-IN")}.${String(rem).padStart(2, "0")}`;
}

/** Whole rupees typed into the budget field -> paise. Integer in, integer out;
 * a non-integer input is rejected upstream rather than rounded here. */
export function rupeesToPaise(wholeRupees: number): number {
  return Math.trunc(wholeRupees) * 100;
}

export function shortId(value: string, n = 18): string {
  return value.length > n ? `${value.slice(0, n)}…` : value;
}

/** "web_search" -> "Web search". Tool and agent identifiers are snake_case on
 * the wire; the UI renders them as plain words. */
export function humanize(id: string): string {
  const words = id.replace(/[_-]+/g, " ").trim();
  return words.charAt(0).toUpperCase() + words.slice(1);
}

/** Unix seconds -> a short local time, for consent expiry. */
export function clockTime(unixSeconds: number): string {
  if (!Number.isFinite(unixSeconds)) return "—";
  return new Date(unixSeconds * 1000).toLocaleTimeString(undefined, {
    hour: "2-digit",
    minute: "2-digit",
  });
}

/** Minutes remaining until a unix-seconds deadline, floored at 0. */
export function minutesUntil(unixSeconds: number): number {
  const ms = unixSeconds * 1000 - Date.now();
  return Math.max(0, Math.round(ms / 60000));
}
