"use strict";

// Sign-in page. POST the password to /auth/login, which verifies it and sets the
// HttpOnly session cookie; then navigate to the app. The token is never read or
// stored by JavaScript (the cookie is invisible to it).

const form = document.getElementById("login-form");
const passwordInput = document.getElementById("login-password");
const loginStatus = document.getElementById("login-status");

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  loginStatus.textContent = "Signing in…";
  try {
    const response = await fetch("/auth/login", {
      method: "POST",
      headers: { "Content-Type": "application/json", Accept: "application/json" },
      body: JSON.stringify({ password: passwordInput.value }),
    });
    if (response.ok) {
      window.location = "/";
      return;
    }
    let detail = `Sign-in failed (${response.status}).`;
    try {
      const body = await response.json();
      if (body && body.detail) detail = body.detail;
    } catch {
      // keep the default message
    }
    loginStatus.textContent = detail;
    passwordInput.value = "";
    passwordInput.focus();
  } catch (error) {
    loginStatus.textContent = error.message;
  }
});
