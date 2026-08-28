// Integer paise -> "₹5,898.82". No float touches a money value.
export function rupees(paise: number): string {
  const whole = Math.trunc(paise / 100);
  const rem = Math.abs(paise % 100);
  return `₹${whole.toLocaleString("en-IN")}.${String(rem).padStart(2, "0")}`;
}

export function shortHash(hash: string, n = 16): string {
  return hash.length > n ? `${hash.slice(0, n)}…` : hash;
}
