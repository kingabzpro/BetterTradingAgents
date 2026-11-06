// Owner-gate login (P2.2): swap the owner token for the session cookie,
// then go to where the user was headed. The ?next= value is only honored
// when it is a local path, so the gate cannot become an open redirect.
(function () {
  "use strict";
  const form = document.getElementById("login-form");
  const input = document.getElementById("owner-token");
  const error = document.getElementById("login-error");
  const button = form.querySelector("button");

  function nextPath() {
    const next = new URLSearchParams(window.location.search).get("next") || "/";
    return next.startsWith("/") && !next.startsWith("//") ? next : "/";
  }

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    button.disabled = true;
    error.textContent = "";
    try {
      const response = await fetch("/api/owner/login", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ token: input.value }),
      });
      if (response.ok) {
        window.location.assign(nextPath());
        return;
      }
      if (response.status === 400) {
        error.textContent = "No owner token is configured for this app.";
      } else if (response.status === 401) {
        error.textContent = "Wrong owner token.";
      } else {
        error.textContent = "Sign-in failed (" + response.status + ").";
      }
    } catch (err) {
      error.textContent = "Sign-in request failed; is the server running?";
    } finally {
      button.disabled = false;
    }
  });
})();
