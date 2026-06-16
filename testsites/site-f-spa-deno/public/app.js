const pageCopy = {
  "/": {
    title: "Welcome",
    body: "Choose an app route from the navigation to simulate SPA route changes.",
  },
  "/app/overview": {
    title: "Overview",
    body: "Overview aggregates health checks, crawl freshness, and incident status.",
  },
  "/app/projects": {
    title: "Projects",
    body: "Projects contains portfolio-level crawl targets grouped by owner and SLA.",
  },
  "/app/reports/2026": {
    title: "Reports 2026",
    body: "Reports 2026 contains monthly coverage exports and audit-ready snapshots.",
  },
  "/app/actions": {
    title: "Actions",
    body: "Create, update, and delete forms return mock confirmations in the SPA.",
  },
};

function normalizePath(pathname) {
  if (!pathname || pathname === "/") {
    return "/";
  }
  return pathname.endsWith("/") ? pathname.slice(0, -1) : pathname;
}

function renderRoute(pathname) {
  const panel = document.getElementById("route-panel");
  const normalizedPath = normalizePath(pathname);
  const route = pageCopy[normalizedPath] ?? {
    title: "Unknown page",
    body: `No view data exists for ${normalizedPath}.`,
  };

  panel.innerHTML = "";
  const title = document.createElement("h2");
  title.textContent = route.title;
  const body = document.createElement("p");
  body.textContent = route.body;
  const meta = document.createElement("p");
  meta.className = "route-path";
  meta.textContent = `Current route: ${normalizedPath}`;

  panel.append(title, body, meta);
  if (normalizedPath === "/app/actions") {
    renderActionForms(panel);
  }

  document.querySelectorAll("#link-panel a").forEach((anchor) => {
    anchor.classList.toggle("active-link", normalizePath(anchor.pathname) === normalizedPath);
  });
}

function actionForm(action, fields) {
  const form = document.createElement("form");
  form.className = "action-form";
  form.dataset.action = action;
  form.method = "post";
  form.action = `/api/actions/${action.toLowerCase()}`;

  fields.forEach((field) => {
    const label = document.createElement("label");
    label.textContent = `${field.label} `;
    let input;
    if (field.options) {
      input = document.createElement("select");
      field.options.forEach((option) => {
        const item = document.createElement("option");
        item.textContent = option;
        input.appendChild(item);
      });
    } else {
      input = document.createElement("input");
      input.value = field.value;
    }
    input.name = field.name;
    label.appendChild(input);
    form.appendChild(label);
  });

  const button = document.createElement("button");
  button.type = "submit";
  button.textContent = `${action} entry`;
  form.appendChild(button);
  return form;
}

function renderActionForms(panel) {
  const section = document.createElement("section");
  section.setAttribute("aria-label", "Workspace actions");
  section.appendChild(actionForm("Create", [
    { label: "Title", name: "title", value: "New signal note" },
    { label: "Owner", name: "owner", value: "ops@example.test" },
  ]));
  section.appendChild(actionForm("Update", [
    { label: "Entry ID", name: "entry_id", value: "signal-001" },
    { label: "Status", name: "status", options: ["Active", "Paused", "Needs review"] },
  ]));
  section.appendChild(actionForm("Delete", [
    { label: "Entry ID", name: "entry_id", value: "signal-001" },
  ]));
  section.addEventListener("submit", (event) => {
    event.preventDefault();
    const form = event.target;
    if (!(form instanceof HTMLFormElement)) {
      return;
    }
    const data = new FormData(form);
    const entryId = data.get("entry_id") || data.get("title") || "entry";
    const result = document.createElement("p");
    result.className = "action-result";
    result.textContent = `${form.dataset.action} accepted for ${entryId}. No persistent data was changed.`;
    section.appendChild(result);
  });
  panel.appendChild(section);
}

function onNavClick(event) {
  if (!(event.target instanceof HTMLAnchorElement)) {
    return;
  }
  if (event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) {
    return;
  }

  const destination = new URL(event.target.href);
  if (destination.origin !== window.location.origin) {
    return;
  }

  event.preventDefault();
  const nextPath = normalizePath(destination.pathname);
  if (nextPath === normalizePath(window.location.pathname)) {
    return;
  }
  window.history.pushState({}, "", nextPath);
  renderRoute(nextPath);
}

async function renderLinks() {
  const panel = document.getElementById("link-panel");
  try {
    const response = await fetch("/api/links");
    const data = await response.json();
    const links = data.links || [];

    panel.innerHTML = "<h2>Navigation</h2>";
    const list = document.createElement("ul");
    list.addEventListener("click", onNavClick);

    links.forEach((href) => {
      const item = document.createElement("li");
      const anchor = document.createElement("a");
      anchor.href = href;
      anchor.textContent = href.replace("/app/", "").replace("/", " ");
      item.appendChild(anchor);
      list.appendChild(item);
    });

    panel.appendChild(list);
    renderRoute(window.location.pathname);
  } catch (error) {
    panel.innerHTML = "<h2>Navigation failed to load</h2>";
    console.error(error);
  }
}

window.addEventListener("popstate", () => {
  renderRoute(window.location.pathname);
});

renderLinks();
