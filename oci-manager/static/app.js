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
    var done = function () {
      var oldText = button.textContent;
      button.textContent = "已复制";
      button.disabled = true;
      setTimeout(function () {
        button.textContent = oldText;
        button.disabled = false;
      }, 900);
    };
    var fail = function () {
      var oldText = button.textContent;
      button.textContent = "复制失败";
      setTimeout(function () {
        button.textContent = oldText;
      }, 900);
    };
    // navigator.clipboard 只在 https / localhost 存在；NAS 的 http 访问必须降级到 execCommand
    if (navigator.clipboard && window.isSecureContext) {
      navigator.clipboard.writeText(text).then(done, fail);
      return;
    }
    var helper = document.createElement("textarea");
    helper.value = text;
    helper.setAttribute("readonly", "");
    helper.style.position = "fixed";
    helper.style.opacity = "0";
    document.body.appendChild(helper);
    helper.select();
    var ok = false;
    try {
      ok = document.execCommand("copy");
    } catch (error) {
      ok = false;
    }
    document.body.removeChild(helper);
    if (ok) {
      done();
    } else {
      fail();
    }
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

  // 确认文案经 data-confirm 属性传入（纯文本上下文），不要拼进内联 onsubmit 的 JS 字符串
  document.querySelectorAll("form[data-confirm]").forEach(function (form) {
    form.addEventListener("submit", function (event) {
      if (!window.confirm(form.getAttribute("data-confirm") || "")) {
        event.preventDefault();
      }
    });
  });

  // 提交后禁用按钮，避免 OCI 慢调用期间重复提交；成功后整页跳转会自然重置。
  // 确认型处理器（data-confirm / data-confirm-value）先运行，被取消时这里跳过。
  document.addEventListener("submit", function (event) {
    var form = event.target;
    if (event.defaultPrevented || !(form instanceof HTMLFormElement)) {
      return;
    }
    window.setTimeout(function () {
      form.querySelectorAll("button[type='submit']").forEach(function (button) {
        button.disabled = true;
      });
    }, 0);
  });

  // 浏览器后退从 bfcache 恢复页面时，重新启用被禁用的提交按钮
  window.addEventListener("pageshow", function (event) {
    if (event.persisted) {
      document.querySelectorAll("button[type='submit']:disabled").forEach(function (button) {
        button.disabled = false;
      });
    }
  });

  var autoRefresh = document.querySelector("[data-auto-refresh]");
  if (autoRefresh) {
    var seconds = Number(autoRefresh.getAttribute("data-auto-refresh")) || 15;
    setInterval(function () {
      // 有弹窗 / 展开的任务日志 / 菜单打开时跳过本轮，避免整页刷新冲掉现场
      if (document.querySelector("dialog[open], details.menu[open], details.fold[open]")) {
        return;
      }
      window.location.reload();
    }, seconds * 1000);
  }

  document.querySelectorAll("[data-dialog-open]").forEach(function (button) {
    button.addEventListener("click", function () {
      var dialog = document.getElementById(button.getAttribute("data-dialog-open"));
      if (!dialog) {
        return;
      }
      if (typeof dialog.showModal === "function") {
        dialog.showModal();
      } else {
        dialog.setAttribute("open", "");
      }
    });
  });

  document.querySelectorAll("[data-dialog-close]").forEach(function (button) {
    button.addEventListener("click", function () {
      var dialog = button.closest("dialog");
      if (!dialog) {
        return;
      }
      if (typeof dialog.close === "function") {
        dialog.close();
      } else {
        dialog.removeAttribute("open");
      }
    });
  });

  document.querySelectorAll("dialog.modal").forEach(function (dialog) {
    dialog.addEventListener("click", function (event) {
      if (event.target === dialog && typeof dialog.close === "function") {
        dialog.close();
      }
    });
  });

  var menus = Array.prototype.slice.call(document.querySelectorAll(".menu"));

  function closeMenus(except) {
    menus.forEach(function (menu) {
      if (menu !== except && menu.hasAttribute("open")) {
        menu.removeAttribute("open");
      }
    });
  }

  menus.forEach(function (menu) {
    var panel = menu.querySelector(".menu-panel");
    menu.addEventListener("toggle", function () {
      if (!menu.hasAttribute("open") || !panel) {
        return;
      }
      closeMenus(menu);
      var rect = menu.getBoundingClientRect();
      var width = panel.offsetWidth;
      var height = panel.offsetHeight;
      var left = Math.max(8, Math.min(rect.left, window.innerWidth - width - 8));
      var top = rect.bottom + 4;
      if (top + height > window.innerHeight - 8) {
        top = Math.max(8, rect.top - height - 4);
      }
      panel.style.position = "fixed";
      panel.style.margin = "0";
      panel.style.top = top + "px";
      panel.style.left = left + "px";
    });
  });

  document.addEventListener("click", function (event) {
    if (!event.target.closest || !event.target.closest(".menu")) {
      closeMenus(null);
    }
  });

  window.addEventListener("scroll", function () { closeMenus(null); }, true);
})();
