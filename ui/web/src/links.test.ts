import { describe, expect, it } from "vitest";
import { safeExternalUrl } from "./links";

describe("external listing links", () => {
  it("allows plain web links and refuses script, document, and credential URLs", () => {
    expect(safeExternalUrl("https://example.com/item")).toBe("https://example.com/item");
    expect(safeExternalUrl("javascript:alert(1)")).toBeNull();
    expect(safeExternalUrl("data:text/html,hello")).toBeNull();
    expect(safeExternalUrl("blob:https://example.com/id")).toBeNull();
    expect(safeExternalUrl("https://user:pass@example.com/item")).toBeNull();
  });
});
