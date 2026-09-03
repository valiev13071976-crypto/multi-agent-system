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

  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    errorEl.hidden = true;
    submit.disabled = true;
    try {
      const res = await fetch("/api/accounts/login", {
        method: "POST",
        headers: { "Content-Type": "application/json", Accept: "application/json" },
        credentials: "same-origin",
        body: JSON.stringify({
          username: document.getElementById("username").value,
          password: document.getElementById("password").value,
        }),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) {
        const code = data.detail?.code || data.code || "INVALID_CREDENTIALS";
        errorEl.textContent = messages[code] || "Не удалось войти.";
        errorEl.hidden = false;
        return;
      }
      window.location.href = "/";
    } catch (_) {
      errorEl.textContent = "Не удалось войти.";
      errorEl.hidden = false;
    } finally {
      submit.disabled = false;
    }
  });
})();
