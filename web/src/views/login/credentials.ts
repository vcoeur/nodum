/**
 * The login form's client-side checks.
 *
 * Client-side validation here is only about not sending a request whose answer
 * is known in advance — the server refuses an empty name or password with the
 * same 401 as a wrong one (constant-time, indistinguishable), so the round
 * trip would buy nothing. The real verification is the server's argon2id
 * check; this module never learns whether credentials are *correct*, only
 * whether they are *present*.
 */

/**
 * What is wrong with the credentials, if anything.
 *
 * @param name The account name as typed.
 * @param password The password as typed.
 * @returns A message for the form, or null when the pair is worth sending.
 */
export function validateCredentials(name: string, password: string): string | null {
  if (name.trim() === "") return "Name is required.";
  if (password === "") return "Password is required.";
  return null;
}
