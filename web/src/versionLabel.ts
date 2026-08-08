/**
 * The header's label for the server version the health poll learned.
 *
 * `/healthz` reports the bare `0.15.0`; the header renders it `v0.15.0`, and
 * renders nothing until the server has actually named one — a payload without
 * a `version` field is a perfectly good answer and must not become a dangling
 * `v`.
 *
 * @param version The `version` the health payload carried, if it carried one.
 * @returns The label (`v0.15.0`), or null while none is known.
 */
export function versionLabel(version: string | undefined): string | null {
  const trimmed = version?.trim();
  if (!trimmed) return null;
  return `v${trimmed}`;
}
