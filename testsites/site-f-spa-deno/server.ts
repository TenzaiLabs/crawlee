import { extname, join } from "https://deno.land/std@0.224.0/path/mod.ts";
import { serve } from "https://deno.land/std@0.224.0/http/server.ts";

const port = Number(Deno.env.get("PORT") ?? "8000");
const rootDir = new URL("./public/", import.meta.url).pathname;

const links = [
  "/app/overview",
  "/app/projects",
  "/app/reports/2026",
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

serve((req) => {
  const url = new URL(req.url);
  if (url.pathname === "/api/links") {
    return Response.json({ links });
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
