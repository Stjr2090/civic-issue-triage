/* Reporta shared UI helpers: theme (light/dark) + toast notifications. */
(function () {
  var KEY = "reporta-theme";
  var root = document.documentElement;

  function stored() { try { return localStorage.getItem(KEY); } catch (e) { return null; } }
  function save(v) { try { localStorage.setItem(KEY, v); } catch (e) { /* private mode: ignore */ } }

  var initial = stored() ||
    (window.matchMedia && window.matchMedia("(prefers-color-scheme: light)").matches ? "light" : "dark");
  root.setAttribute("data-theme", initial);

  document.addEventListener("DOMContentLoaded", function () {
    var btn = document.getElementById("theme-toggle");
    if (btn) {
      var sync = function () {
        var light = root.getAttribute("data-theme") === "light";
        btn.setAttribute("aria-label", light ? "Switch to dark theme" : "Switch to light theme");
        btn.setAttribute("title", light ? "Switch to dark theme" : "Switch to light theme");
      };
      sync();
      btn.addEventListener("click", function () {
        var next = root.getAttribute("data-theme") === "light" ? "dark" : "light";
        root.setAttribute("data-theme", next);
        save(next);
        sync();
      });
    }
  });

  // window.reportaToast("Saved", "ok" | "err")
  window.reportaToast = function (text, kind) {
    var box = document.getElementById("toasts");
    if (!box) {
      box = document.createElement("div");
      box.id = "toasts";
      box.className = "toasts";
      box.setAttribute("role", "status");
      box.setAttribute("aria-live", "polite");
      document.body.appendChild(box);
    }
    var t = document.createElement("div");
    t.className = "toast" + (kind ? " " + kind : "");
    t.textContent = text;
    box.appendChild(t);
    setTimeout(function () { if (t.parentNode) t.parentNode.removeChild(t); }, 3800);
  };
})();
