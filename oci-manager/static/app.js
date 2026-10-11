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
        var baseUrl = select.getAttribute("data-tenant-base");
        if (baseUrl) {
          // 全局页（/cost、/checks/idle 等）：租户走 ?tenant= 参数
          window.location.href = baseUrl + "?tenant=" + encodeURIComponent(select.value);
          return;
        }
        var modulePath = select.getAttribute("data-module-path");
        if (modulePath === null) {
          modulePath = "instances";
        }
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

  function showToast(message, ok) {
    var stack = document.querySelector(".flash-stack");
    if (!stack) {
      stack = document.createElement("div");
      stack.className = "flash-stack";
      var shell = document.querySelector(".shell");
      if (shell) {
        shell.insertBefore(stack, shell.firstChild);
      } else {
        document.body.appendChild(stack);
      }
    }
    var item = document.createElement("div");
    item.className = "flash " + (ok ? "success" : "error");
    item.setAttribute("role", "alert");
    item.textContent = message;
    stack.appendChild(item);
    setTimeout(function () {
      item.remove();
    }, 6000);
  }

  // data-ajax 表单：fetch 提交（后端识别 X-Requested-With 返回 JSON），成功后局部更新
  document.querySelectorAll("form[data-ajax]").forEach(function (form) {
    form.addEventListener("submit", function (event) {
      if (event.defaultPrevented) {
        return;
      }
      event.preventDefault();
      var button = form.querySelector("button[type='submit']");
      if (button) {
        button.disabled = true;
      }
      fetch(form.action, {
        method: "POST",
        headers: { "X-CSRF-Token": csrfToken, "X-Requested-With": "fetch" },
        body: new FormData(form),
      }).then(function (resp) {
        return resp.json();
      }).then(function (data) {
        showToast(data.message || (data.ok ? "操作已提交。" : "操作失败。"), !!data.ok);
        if (!data.ok && button) {
          button.disabled = false;
        }
        if (data.ok) {
          var menu = form.closest("details");
          if (menu) {
            menu.removeAttribute("open");
          }
          if (data.state) {
            var row = form.closest("tr");
            var badge = row ? row.querySelector(".badge-state") : null;
            if (badge) {
              badge.textContent = data.state;
              badge.className = "badge badge-state " + (data.state === "RUNNING" ? "badge-good" : data.state === "STOPPED" || data.state === "TERMINATED" ? "badge-bad" : "badge-mid");
            }
          }
        }
      }).catch(function () {
        showToast("网络错误，操作未确认，请稍后重试。", false);
        if (button) {
          button.disabled = false;
        }
      });
    });
  });

  // 实例表筛选：文本匹配整行内容，状态下拉从行内状态徽章自动收集
  document.querySelectorAll("[data-table-filter]").forEach(function (toolbar) {
    var wrap = toolbar.parentElement ? toolbar.parentElement.querySelector(".table-wrap") : null;
    var table = wrap ? wrap.querySelector("table") : null;
    var textInput = toolbar.querySelector("[data-filter-text]");
    var stateSelect = toolbar.querySelector("[data-filter-state]");
    if (!table || !textInput || !stateSelect) {
      return;
    }
    var rows = Array.prototype.slice.call(table.querySelectorAll("tbody tr"));
    var states = {};
    rows.forEach(function (row) {
      var badge = row.querySelector(".badge-state");
      if (badge) {
        states[badge.textContent.trim()] = true;
      }
    });
    Object.keys(states).sort().forEach(function (state) {
      var option = document.createElement("option");
      option.value = state;
      option.textContent = state;
      stateSelect.appendChild(option);
    });
    function applyFilter() {
      var text = (textInput.value || "").trim().toLowerCase();
      var state = stateSelect.value;
      rows.forEach(function (row) {
        var matchText = !text || row.textContent.toLowerCase().indexOf(text) !== -1;
        var badge = row.querySelector(".badge-state");
        var matchState = !state || (badge && badge.textContent.trim() === state);
        row.hidden = !(matchText && matchState);
      });
    }
    textInput.addEventListener("input", applyFilter);
    stateSelect.addEventListener("change", applyFilter);
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
      // 实例表筛选正在输入或有内容时同样跳过
      var filter = document.querySelector("[data-filter-text]");
      if (filter && (filter.value || document.activeElement === filter)) {
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
