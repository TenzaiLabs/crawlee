import { extname, join } from "https://deno.land/std@0.224.0/path/mod.ts";
import { serve } from "https://deno.land/std@0.224.0/http/server.ts";

const port = Number(Deno.env.get("PORT") ?? "8000");
const rootDir = new URL("./public/", import.meta.url).pathname;

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

serve((req) => {
  const url = new URL(req.url);
  if (url.pathname === "/api/links") {
    return Response.json({ links });
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
