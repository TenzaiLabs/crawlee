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

  document.querySelectorAll("#link-panel a").forEach((anchor) => {
    anchor.classList.toggle("active-link", normalizePath(anchor.pathname) === normalizedPath);
  });
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
