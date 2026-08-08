/**
 * The header's server-version label (`versionLabel.ts`).
 *
 * The semantics under test are the two the header depends on: the bare semver
 * the health payload carries gets its `v` prefix, and anything else — an
 * absent field, a blank one — renders nothing rather than a dangling label.
 */
import { describe, expect, it } from "vitest";

import { versionLabel } from "./versionLabel";

describe("versionLabel", () => {
  it("prefixes the bare semver the server reports", () => {
    expect(versionLabel("0.15.0")).toBe("v0.15.0");
  });

  it("passes a pre-release build id through untouched", () => {
    expect(versionLabel("0.15.0-rc.1")).toBe("v0.15.0-rc.1");
  });

  it("renders nothing while no version is known", () => {
    expect(versionLabel(undefined)).toBeNull();
  });

  it("renders nothing for a blank version", () => {
    expect(versionLabel("")).toBeNull();
    expect(versionLabel("   ")).toBeNull();
  });
});
