(function () {
  var csrfMeta = document.querySelector("meta[name='csrf-token']");
  var csrfToken = csrfMeta ? csrfMeta.getAttribute("content") : "";

  if (csrfToken) {
    document.querySelectorAll("form[method='post'], form[method='POST']").forEach(function (form) {
      if (form.querySelector("input[name='csrf_token']")) {
        return;
      }
      var input = document.createElement("input");
      input.type = "hidden";
      input.name = "csrf_token";
      input.value = csrfToken;
      form.appendChild(input);
    });
  }

  function copyText(text, button) {
    if (!text || text === "-") {
      return;
    }
    navigator.clipboard.writeText(text).then(function () {
      var oldText = button.textContent;
      button.textContent = "已复制";
      button.disabled = true;
      setTimeout(function () {
        button.textContent = oldText;
        button.disabled = false;
      }, 900);
    }).catch(function () {
      var oldText = button.textContent;
      button.textContent = "复制失败";
      setTimeout(function () {
        button.textContent = oldText;
      }, 900);
    });
  }

  document.querySelectorAll("[data-copy]").forEach(function (button) {
    button.addEventListener("click", function () {
      copyText(button.getAttribute("data-copy"), button);
    });
  });

  document.querySelectorAll("[data-tenant-switch]").forEach(function (select) {
    select.addEventListener("change", function () {
      if (select.value) {
        var modulePath = select.getAttribute("data-module-path") || "instances";
        window.location.href = "/tenant/" + encodeURIComponent(select.value) + "/" + modulePath;
      }
    });
  });

  document.querySelectorAll("[data-confirm-value]").forEach(function (form) {
    form.addEventListener("submit", function (event) {
      var expected = form.getAttribute("data-confirm-value") || "";
      var label = form.getAttribute("data-confirm-label") || "名称";
      var input = form.querySelector("input[name='confirm_name']");
      var value = window.prompt("请输入" + label + "确认：" + expected);
      if (value !== expected) {
        event.preventDefault();
        return;
      }
      if (input) {
        input.value = value;
      }
    });
  });

  var autoRefresh = document.querySelector("[data-auto-refresh]");
  if (autoRefresh) {
    var seconds = Number(autoRefresh.getAttribute("data-auto-refresh")) || 15;
    setTimeout(function () {
      window.location.reload();
    }, seconds * 1000);
  }
})();
