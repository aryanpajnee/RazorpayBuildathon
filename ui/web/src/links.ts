// Product links come from web search results, which are untrusted input. A URL
// is rendered or followed only if it survives this.
//
// The refusal list matters: `javascript:` executes in our origin, `data:` and
// `blob:` can carry a whole attacker-authored document, and embedded
// credentials (`https://user:pass@host`) are a phishing shape. Anything that is
// not plain http/https is dropped and the UI shows the item without a link
// rather than rendering something it cannot vouch for.
export function safeExternalUrl(value?: string | null): string | null {
  if (typeof value !== "string" || value.trim() === "") return null;
  let parsed: URL;
  try {
    parsed = new URL(value);
  } catch {
    return null;
  }
  if (parsed.protocol !== "https:" && parsed.protocol !== "http:") return null;
  if (parsed.username || parsed.password) return null;
  return parsed.href;
}

/** "https://www.example.com/x" -> "example.com", for showing where a link goes
 * without making the reader parse a URL. */
export function hostLabel(href: string): string | null {
  try {
    return new URL(href).hostname.replace(/^www\./, "");
  } catch {
    return null;
  }
}
