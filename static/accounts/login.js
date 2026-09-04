(function () {
  const form = document.getElementById("login-form");
  const errorEl = document.getElementById("error");
  const submit = document.getElementById("submit");
  const brand = document.getElementById("brand");
  if (window.PandaBrand && brand) {
    brand.innerHTML = window.PandaBrand.markHtml({ size: 40, title: "Panda" });
  }

  const messages = {
    INVALID_CREDENTIALS: "Неверное имя пользователя или пароль.",
    ACCOUNT_DISABLED: "Аккаунт отключён.",
    SESSION_EXPIRED: "Сессия истекла. Войдите снова.",
    RATE_LIMITED: "Слишком много попыток. Попробуйте позже.",
  };

  async function pandaLoginSubmit(e) {
    if (e) e.preventDefault();
    if (!form || !errorEl || !submit) return false;
    errorEl.hidden = true;
    submit.disabled = true;
    try {
      const usernameEl = document.getElementById("username");
      const passwordEl = document.getElementById("password");
      const res = await fetch("/api/accounts/login", {
        method: "POST",
        headers: { "Content-Type": "application/json", Accept: "application/json" },
        credentials: "same-origin",
        body: JSON.stringify({
          username: usernameEl ? usernameEl.value : "",
          password: passwordEl ? passwordEl.value : "",
        }),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) {
        const code = data.detail?.code || data.code || "INVALID_CREDENTIALS";
        errorEl.textContent = messages[code] || "Не удалось войти.";
        errorEl.hidden = false;
        return false;
      }
      window.location.href = "/app";
    } catch (_) {
      errorEl.textContent = "Не удалось войти.";
      errorEl.hidden = false;
    } finally {
      submit.disabled = false;
    }
    return false;
  }

  window.pandaLoginSubmit = pandaLoginSubmit;
  if (form) {
    form.addEventListener("submit", pandaLoginSubmit);
  }
})();
