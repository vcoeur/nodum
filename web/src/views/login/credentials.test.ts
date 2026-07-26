/**
 * The login form's presence checks (`credentials.ts`).
 *
 * Only *absence* is decided client-side: the server's 401 is deliberately
 * indistinguishable between a wrong name and a wrong password, so the form
 * must not pretend to know more than "both fields are filled in".
 */

import { describe, expect, it } from "vitest";
import { validateCredentials } from "./credentials";

describe("validateCredentials", () => {
  it("accepts a filled-in pair", () => {
    expect(validateCredentials("owner", "correct horse battery staple")).toBeNull();
  });

  it("refuses an empty or whitespace-only name before any network call", () => {
    expect(validateCredentials("", "secret")).toBe("Name is required.");
    expect(validateCredentials("   ", "secret")).toBe("Name is required.");
  });

  it("refuses an empty password", () => {
    expect(validateCredentials("owner", "")).toBe("Password is required.");
  });

  it("names the name first when both are missing", () => {
    expect(validateCredentials("", "")).toBe("Name is required.");
  });

  it("does not trim the password — spaces can be part of one", () => {
    expect(validateCredentials("owner", " ")).toBeNull();
  });
});
