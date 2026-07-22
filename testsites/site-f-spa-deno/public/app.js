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
    body: "Use the filter drawer to search the runtime project catalogue.",
  },
  "/app/reports/2026": {
    title: "Reports 2026",
    body: "Build a draft coverage report with the conditional report wizard.",
  },
  "/app/actions": {
    title: "Actions",
    body: "Create, update, and delete forms return mock confirmations in the SPA.",
  },
};

function apiPath(...parts) {
  return ["", "api", ...parts].join("/");
}

function normalizePath(pathname) {
  if (!pathname || pathname === "/") {
    return "/";
  }
  return pathname.endsWith("/") ? pathname.slice(0, -1) : pathname;
}

function button(label, onClick, className = "") {
  const control = document.createElement("button");
  control.type = "button";
  control.textContent = label;
  control.className = className;
  control.addEventListener("click", onClick);
  return control;
}

function statusMessage(text) {
  const message = document.createElement("p");
  message.className = "action-result";
  message.textContent = text;
  return message;
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

  if (normalizedPath === "/app/overview") {
    renderShadowAudit(panel);
  } else if (normalizedPath === "/app/projects") {
    renderProjects(panel);
  } else if (normalizedPath === "/app/reports/2026") {
    renderReportWizard(panel);
  } else if (normalizedPath === "/app/actions") {
    renderActionForms(panel);
  }

  document.querySelectorAll("#link-panel a").forEach((anchor) => {
    anchor.classList.toggle("active-link", normalizePath(anchor.pathname) === normalizedPath);
  });
}

function renderShadowAudit(panel) {
  const host = document.createElement("div");
  host.id = "audit-shadow-host";
  host.setAttribute("aria-label", "Audit tools");
  const shadow = host.attachShadow({ mode: "open" });
  const wrapper = document.createElement("section");
  const heading = document.createElement("h3");
  heading.textContent = "Audit snapshot";
  const output = document.createElement("p");
  output.setAttribute("aria-live", "polite");
  wrapper.append(
    heading,
    button("Load audit snapshot", async () => {
      const response = await fetch(apiPath("shadow", "audit"));
      const data = await response.json();
      output.textContent = `${data.records} audit records are ${data.audit}.`;
    }),
    output,
  );
  shadow.appendChild(wrapper);
  panel.appendChild(host);
}

function renderProjects(panel) {
  const workspace = document.createElement("section");
  workspace.className = "discovery-workspace";
  const results = document.createElement("div");
  results.setAttribute("aria-live", "polite");
  const drawer = document.createElement("form");
  drawer.className = "filter-drawer";
  drawer.hidden = true;

  const ownerLabel = document.createElement("label");
  ownerLabel.textContent = "Owner ";
  const owner = document.createElement("select");
  owner.name = "owner";
  for (const value of ["Any", "Platform", "Security"]) {
    owner.appendChild(new Option(value, value.toLowerCase()));
  }
  ownerLabel.appendChild(owner);

  const statusLabel = document.createElement("label");
  statusLabel.textContent = "Status ";
  const status = document.createElement("select");
  status.name = "status";
  for (const value of ["Active", "Review"]) {
    status.appendChild(new Option(value, value.toLowerCase()));
  }
  statusLabel.appendChild(status);

  const submit = document.createElement("button");
  submit.type = "submit";
  submit.textContent = "Search projects";
  drawer.append(ownerLabel, statusLabel, submit);

  drawer.addEventListener("submit", async (event) => {
    event.preventDefault();
    results.replaceChildren(statusMessage("Searching project catalogue…"));
    const response = await fetch(apiPath("projects", "search"), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(Object.fromEntries(new FormData(drawer))),
    });
    const data = await response.json();
    renderProjectRows(results, data.projects || []);
  });

  const toggle = button("Open project filters", () => {
    drawer.hidden = !drawer.hidden;
    toggle.textContent = drawer.hidden ? "Open project filters" : "Close project filters";
  });
  workspace.append(toggle, drawer, results);
  panel.appendChild(workspace);
}

function renderProjectRows(container, projects) {
  const table = document.createElement("table");
  const body = document.createElement("tbody");
  table.innerHTML = "<thead><tr><th>Project</th><th>Owner</th><th>Status</th><th>Details</th></tr></thead>";
  for (const project of projects) {
    const row = document.createElement("tr");
    row.innerHTML = `<td>${project.name}</td><td>${project.owner}</td><td>${project.status}</td>`;
    const action = document.createElement("td");
    action.appendChild(button(`Inspect ${project.name}`, () => openProjectModal(project)));
    row.appendChild(action);
    body.appendChild(row);
  }
  table.appendChild(body);
  container.replaceChildren(table);
}

async function openProjectModal(project) {
  const modal = document.getElementById("project-modal");
  const title = modal.querySelector("h2");
  const body = modal.querySelector("p");
  title.textContent = project.name;
  body.textContent = "Loading runtime details…";
  modal.showModal();
  if (project.id === "project-aurora") {
    const response = await fetch(apiPath("projects", "details", project.id));
    const data = await response.json();
    body.textContent = `SLA ${data.sla}; region ${data.region}.`;
  } else {
    body.textContent = "Details are available only for the primary fixture project.";
  }
}

function renderReportWizard(panel) {
  const container = document.createElement("section");
  container.className = "wizard";
  const launch = button("Build coverage report", () => renderScopeStep(container));
  container.appendChild(launch);
  panel.appendChild(container);
}

function renderScopeStep(container) {
  const form = document.createElement("form");
  form.innerHTML = `
    <h3>Step 1 of 2: report scope</h3>
    <label>Report type
      <select name="report_type"><option value="coverage">Coverage</option></select>
    </label>
    <label>Include audit details <input type="checkbox" name="audit" checked /></label>
    <button type="submit">Validate report scope</button>
  `;
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const response = await fetch(apiPath("reports", "validate"), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(Object.fromEntries(new FormData(form))),
    });
    const result = await response.json();
    if (result.valid) {
      renderPreviewStep(container);
    }
  });
  container.replaceChildren(form);
}

function renderPreviewStep(container) {
  const section = document.createElement("section");
  const heading = document.createElement("h3");
  heading.textContent = "Step 2 of 2: draft preview";
  const summary = document.createElement("p");
  summary.textContent = "Coverage report with audit details. Previewing does not publish it.";
  const output = document.createElement("div");
  section.append(
    heading,
    summary,
    button("Preview draft report", async () => {
      const response = await fetch(apiPath("reports", "preview"), { method: "POST" });
      const data = await response.json();
      output.replaceChildren(statusMessage(`Draft ${data.report_id} is in ${data.state} state.`));
    }),
    output,
  );
  container.replaceChildren(section);
}

function actionForm(action, fields) {
  const form = document.createElement("form");
  form.className = "action-form";
  form.dataset.action = action;
  form.method = "post";
  form.action = apiPath("actions", action.toLowerCase());

  fields.forEach((field) => {
    const label = document.createElement("label");
    label.textContent = `${field.label} `;
    let input;
    if (field.options) {
      input = document.createElement("select");
      field.options.forEach((option) => input.appendChild(new Option(option, option)));
    } else {
      input = document.createElement("input");
      input.value = field.value;
    }
    input.name = field.name;
    label.appendChild(input);
    form.appendChild(label);
  });

  const submit = document.createElement("button");
  submit.type = "submit";
  submit.textContent = `${action} entry`;
  form.appendChild(submit);
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
    const entryId = new FormData(form).get("entry_id") || "entry";
    section.appendChild(statusMessage(`${form.dataset.action} accepted for ${entryId}.`));
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
  if (nextPath !== normalizePath(window.location.pathname)) {
    window.history.pushState({}, "", nextPath);
    renderRoute(nextPath);
  }
}

async function renderLinks() {
  const panel = document.getElementById("link-panel");
  try {
    const response = await fetch(apiPath("links"));
    const data = await response.json();
    panel.innerHTML = "<h2>Navigation</h2>";
    const list = document.createElement("ul");
    list.addEventListener("click", onNavClick);
    for (const href of data.links || []) {
      const item = document.createElement("li");
      const anchor = document.createElement("a");
      anchor.href = href;
      anchor.textContent = href.replace("/app/", "").replace("/", " ");
      item.appendChild(anchor);
      list.appendChild(item);
    }
    panel.appendChild(list);
    renderRoute(window.location.pathname);
  } catch (error) {
    panel.innerHTML = "<h2>Navigation failed to load</h2>";
    console.error(error);
  }
}

document.getElementById("project-modal-close").addEventListener("click", () => {
  document.getElementById("project-modal").close();
});
window.addEventListener("popstate", () => renderRoute(window.location.pathname));
renderLinks();
