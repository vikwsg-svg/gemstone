// GemstoneAI Enterprise — minimal site behaviour (no dependencies).

(function () {
  // Mobile navigation
  var toggle = document.querySelector(".nav-toggle");
  var links = document.getElementById("nav-links");
  if (toggle && links) {
    toggle.addEventListener("click", function () {
      var open = links.classList.toggle("open");
      toggle.setAttribute("aria-expanded", open ? "true" : "false");
      toggle.textContent = open ? "Close" : "Menu";
    });
  }

  // Pilot request form: opens the visitor's email client with the request filled in.
  // Replace data-contact on the <form> (or swap this for your CRM/form endpoint)
  // before publishing.
  var form = document.getElementById("pilot-form");
  if (!form) return;
  form.addEventListener("submit", function (event) {
    event.preventDefault();
    if (!form.reportValidity()) return;
    var data = new FormData(form);
    var products = data.getAll("products").join(", ") || "Not sure yet";
    var lines = [
      "Name: " + (data.get("name") || ""),
      "Work email: " + (data.get("email") || ""),
      "Company: " + (data.get("company") || ""),
      "Role: " + (data.get("role") || ""),
      "Segment: " + (data.get("segment") || ""),
      "Products of interest: " + products,
      "Monthly volume: " + (data.get("volume") || ""),
      "",
      (data.get("message") || "").toString()
    ];
    var subject = "Pilot request: " + (data.get("company") || "GemstoneAI Enterprise");
    var href = "mailto:" + form.getAttribute("data-contact") +
      "?subject=" + encodeURIComponent(subject) +
      "&body=" + encodeURIComponent(lines.join("\n"));
    window.location.href = href;
    var status = document.getElementById("form-status");
    if (status) status.classList.add("show");
  });
})();
