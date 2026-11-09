// Citation Copy Handler for SpecSnap
(function () {
  "use strict";

  document.addEventListener("DOMContentLoaded", function () {
    document.querySelectorAll(".cite-copy-btn").forEach(function (btn) {
      btn.addEventListener("click", function () {
        const pre = btn.parentElement.querySelector("pre");
        if (!pre) return;
        navigator.clipboard.writeText(pre.textContent).then(function () {
          const orig = btn.textContent;
          btn.textContent = "Copied";
          setTimeout(function () { btn.textContent = orig; }, 1500);
        });
      });
    });
  });
})();
