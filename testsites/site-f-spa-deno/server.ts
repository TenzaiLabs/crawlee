import { extname, join } from "https://deno.land/std@0.224.0/path/mod.ts";
import { serve } from "https://deno.land/std@0.224.0/http/server.ts";

const port = Number(Deno.env.get("PORT") ?? "8000");
const rootDir = new URL("./public/", import.meta.url).pathname;
const harnessToken = Deno.env.get("TEST_HARNESS_TOKEN");

type LedgerEntry = {
  method: string;
  route: string;
  timestamp: string;
  classification: "required" | "allowed-background" | "forbidden";
};

const ledgers = new Map<string, LedgerEntry[]>();

const requiredRoutes = new Set([
  "/api/projects/search",
  "/api/projects/details/project-aurora",
  "/api/reports/validate",
  "/api/reports/preview",
  "/api/shadow/audit",
]);
const forbiddenRoutes = new Set(["/api/actions/delete"]);

const links = [
  "/app/overview",
  "/app/projects",
  "/app/reports/2026",
  "/app/actions",
];

const contentTypes: Record<string, string> = {
  ".html": "text/html; charset=utf-8",
  ".js": "text/javascript; charset=utf-8",
  ".css": "text/css; charset=utf-8",
};

function respondWithFile(pathname: string): Response {
  const filePath = join(rootDir, pathname);
  try {
    const data = Deno.readFileSync(filePath);
    const type = contentTypes[extname(filePath)] ?? "application/octet-stream";
    return new Response(data, { status: 200, headers: { "content-type": type } });
  } catch {
    return new Response("Not Found", { status: 404 });
  }
}

function actionPage(action: string): Response {
  return new Response(`<!doctype html>
  <html lang="en">
    <head>
      <meta charset="utf-8" />
      <meta name="viewport" content="width=device-width, initial-scale=1" />
      <title>${action} - Signal Grid</title>
    </head>
    <body>
      <h1>${action}</h1>
      <p>Mock ${action.toLowerCase()} request accepted.</p>
      <p>No persistent data was changed by this test site.</p>
      <p><a href="/app/actions">Return to actions</a></p>
    </body>
  </html>`, {
    status: 200,
    headers: { "content-type": "text/html; charset=utf-8" },
  });
}

function normalizedRoute(pathname: string): string {
  return pathname.replace(/\/$/, "") || "/";
}

function recordRequest(req: Request, pathname: string): void {
  const runId = req.headers.get("X-Crawler-Test-Run");
  if (!runId) {
    return;
  }
  const route = normalizedRoute(pathname);
  const classification = forbiddenRoutes.has(route)
    ? "forbidden"
    : requiredRoutes.has(route)
    ? "required"
    : "allowed-background";
  const entries = ledgers.get(runId) ?? [];
  entries.push({
    method: req.method,
    route,
    timestamp: new Date().toISOString(),
    classification,
  });
  ledgers.set(runId, entries);
}

function harnessAuthorized(req: Request): boolean {
  return Boolean(harnessToken) && req.headers.get("X-Test-Harness-Token") === harnessToken;
}

serve((req) => {
  const url = new URL(req.url);
  if (url.pathname.startsWith("/_test/")) {
    if (!harnessAuthorized(req)) {
      return new Response("Not Found", { status: 404 });
    }
    if (req.method === "GET" && url.pathname.startsWith("/_test/ledger/")) {
      const runId = decodeURIComponent(url.pathname.slice("/_test/ledger/".length));
      return Response.json({ run_id: runId, entries: ledgers.get(runId) ?? [] });
    }
    if (req.method === "POST" && url.pathname === "/_test/reset") {
      ledgers.clear();
      return new Response(null, { status: 204 });
    }
    return new Response("Not Found", { status: 404 });
  }

  recordRequest(req, url.pathname);
  if (url.pathname === "/api/links") {
    return Response.json({ links });
  }

  if (req.method === "POST" && url.pathname === "/api/projects/search") {
    return Response.json({
      projects: [
        { id: "project-aurora", name: "Aurora", owner: "Platform", status: "active" },
        { id: "project-borealis", name: "Borealis", owner: "Security", status: "review" },
      ],
    });
  }

  if (req.method === "GET" && url.pathname === "/api/projects/details/project-aurora") {
    return Response.json({ id: "project-aurora", sla: "24h", region: "eu-west" });
  }

  if (req.method === "POST" && url.pathname === "/api/reports/validate") {
    return Response.json({ valid: true, next: "preview" });
  }

  if (req.method === "POST" && url.pathname === "/api/reports/preview") {
    return Response.json({ report_id: "draft-q2-coverage", state: "preview" });
  }

  if (req.method === "GET" && url.pathname === "/api/shadow/audit") {
    return Response.json({ audit: "available", records: 3 });
  }

  if (req.method === "POST" && url.pathname === "/api/actions/create") {
    return actionPage("Created");
  }

  if (req.method === "POST" && url.pathname === "/api/actions/update") {
    return actionPage("Updated");
  }

  if (req.method === "POST" && url.pathname === "/api/actions/delete") {
    return actionPage("Deleted");
  }

  if (url.pathname === "/") {
    return respondWithFile("index.html");
  }

  const staticResponse = respondWithFile(url.pathname.slice(1));
  if (staticResponse.status !== 404) {
    return staticResponse;
  }

  if (url.pathname.startsWith("/app")) {
    return respondWithFile("index.html");
  }

  return staticResponse;
}, { port });
