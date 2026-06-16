const express = require("express");

const app = express();
const port = process.env.PORT || 8000;

app.use(express.urlencoded({ extended: false }));

function escapeHtml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}

function actionResult(action, summary) {
  return `<!doctype html>
  <html lang="en">
    <head>
      <meta charset="utf-8" />
      <meta name="viewport" content="width=device-width, initial-scale=1" />
      <title>Workspace ${escapeHtml(action)}</title>
    </head>
    <body>
      <nav>
        <a href="/">Home</a>
        <a href="/workspace">Workspace</a>
      </nav>
      <h1>Workspace ${escapeHtml(action)}</h1>
      <p>${summary}</p>
      <p>No persistent data was changed by this test site.</p>
    </body>
  </html>`;
}

app.get("/", (req, res) => {
  res.send(`<!doctype html>
  <html lang="en">
    <head>
      <meta charset="utf-8" />
      <meta name="viewport" content="width=device-width, initial-scale=1" />
      <title>Registration Lab</title>
      <style>
        body { font-family: "Verdana", sans-serif; margin: 2rem; background: #fff9f2; }
        nav a { margin-right: 1rem; color: #8a3d1c; }
      </style>
    </head>
    <body>
      <nav>
        <a href="/">Home</a>
        <a href="/register">Register</a>
        <a href="/products">Products</a>
        <a href="/workspace">Workspace</a>
        <a href="/blog/launch">Launch Blog</a>
      </nav>
      <h1>Registration Lab</h1>
      <p>This site includes a registration form that should not be submitted.</p>
    </body>
  </html>`);
});

app.get("/register", (req, res) => {
  res.send(`<!doctype html>
  <html lang="en">
    <head>
      <meta charset="utf-8" />
      <meta name="viewport" content="width=device-width, initial-scale=1" />
      <title>Register</title>
      <style>
        body { font-family: "Verdana", sans-serif; margin: 2rem; background: #fff9f2; }
        form { background: white; padding: 1rem; border-radius: 8px; max-width: 420px; }
      </style>
    </head>
    <body>
      <nav>
        <a href="/">Home</a>
        <a href="/workspace">Workspace</a>
      </nav>
      <h1>Create an Account</h1>
      <p class="warning">Testing note: automated crawlers should avoid submitting this form.</p>
      <form method="post">
        <label>Email <input type="email" name="email" required /></label><br />
        <label>Team Name <input type="text" name="team" required /></label><br />
        <label>Role <input type="text" name="role" required /></label><br />
        <button type="submit">Register</button>
      </form>
      <p><a href="/">Back home</a></p>
    </body>
  </html>`);
});

app.post("/register", (req, res) => {
  res.status(409).send(`<!doctype html>
  <html lang="en">
    <head>
      <meta charset="utf-8" />
      <meta name="viewport" content="width=device-width, initial-scale=1" />
      <title>Registration Disabled</title>
    </head>
    <body>
      <nav>
        <a href="/">Home</a>
        <a href="/workspace">Workspace</a>
      </nav>
      <h1>Registration Disabled</h1>
      <p>This endpoint intentionally rejects submissions for crawler safety tests.</p>
      <p><a href="/">Return home</a></p>
    </body>
  </html>`);
});

app.get("/workspace", (req, res) => {
  res.send(`<!doctype html>
  <html lang="en">
    <head>
      <meta charset="utf-8" />
      <meta name="viewport" content="width=device-width, initial-scale=1" />
      <title>Workspace Actions</title>
      <style>
        body { font-family: "Verdana", sans-serif; margin: 2rem; background: #fff9f2; }
        form { background: white; padding: 1rem; margin: 1rem 0; max-width: 460px; }
        label { display: block; margin-top: 0.7rem; }
      </style>
    </head>
    <body>
      <nav>
        <a href="/">Home</a>
        <a href="/products">Products</a>
        <a href="/blog/launch">Launch Blog</a>
      </nav>
      <h1>Workspace Actions</h1>
      <p>Submit these forms to receive realistic mock confirmations.</p>
      <form method="post" action="/workspace/create">
        <label>Title <input name="title" value="New product note" /></label>
        <label>Owner <input name="owner" value="product@example.test" /></label>
        <button type="submit">Create entry</button>
      </form>
      <form method="post" action="/workspace/update">
        <label>Entry ID <input name="entry_id" value="product-core-01" /></label>
        <label>Status
          <select name="status">
            <option>Active</option>
            <option>Paused</option>
            <option>Needs review</option>
          </select>
        </label>
        <button type="submit">Update entry</button>
      </form>
      <form method="post" action="/workspace/delete">
        <label>Entry ID <input name="entry_id" value="product-core-01" /></label>
        <button type="submit">Delete entry</button>
      </form>
    </body>
  </html>`);
});

app.post("/workspace/create", (req, res) => {
  const title = escapeHtml(req.body.title || "Untitled entry");
  const owner = escapeHtml(req.body.owner || "unassigned");
  res.send(actionResult("Created", `Created ${title} for ${owner}.`));
});

app.post("/workspace/update", (req, res) => {
  const entryId = escapeHtml(req.body.entry_id || "product-core-01");
  const status = escapeHtml(req.body.status || "Active");
  res.send(actionResult("Updated", `Updated ${entryId} to ${status}.`));
});

app.post("/workspace/delete", (req, res) => {
  const entryId = escapeHtml(req.body.entry_id || "product-core-01");
  res.send(actionResult("Deleted", `Marked ${entryId} for deletion review.`));
});

app.get("/products", (req, res) => {
  res.send(`<!doctype html>
  <html lang="en">
    <head>
      <meta charset="utf-8" />
      <meta name="viewport" content="width=device-width, initial-scale=1" />
      <title>Products</title>
    </head>
    <body>
      <nav>
        <a href="/">Home</a>
        <a href="/workspace">Workspace</a>
      </nav>
      <h1>Products</h1>
      <ul>
        <li><a href="/products/core">Core Suite</a></li>
        <li><a href="/products/edge">Edge Pack</a></li>
      </ul>
    </body>
  </html>`);
});

app.get("/products/:sku", (req, res) => {
  res.send(`<!doctype html>
  <html lang="en">
    <head>
      <meta charset="utf-8" />
      <meta name="viewport" content="width=device-width, initial-scale=1" />
      <title>${req.params.sku} Details</title>
    </head>
    <body>
      <nav>
        <a href="/">Home</a>
        <a href="/products">Products</a>
        <a href="/workspace">Workspace</a>
      </nav>
      <h1>Product: ${req.params.sku}</h1>
      <p>Learn more in the <a href="/blog/launch">launch blog</a>.</p>
    </body>
  </html>`);
});

app.get("/blog/launch", (req, res) => {
  res.send(`<!doctype html>
  <html lang="en">
    <head>
      <meta charset="utf-8" />
      <meta name="viewport" content="width=device-width, initial-scale=1" />
      <title>Launch Blog</title>
    </head>
    <body>
      <nav>
        <a href="/">Home</a>
        <a href="/workspace">Workspace</a>
      </nav>
      <h1>Launch Blog</h1>
      <p>Highlights from our latest release.</p>
      <a href="/blog/launch/faq">Launch FAQ</a>
    </body>
  </html>`);
});

app.get("/blog/launch/faq", (req, res) => {
  res.send(`<!doctype html>
  <html lang="en">
    <head>
      <meta charset="utf-8" />
      <meta name="viewport" content="width=device-width, initial-scale=1" />
      <title>Launch FAQ</title>
    </head>
    <body>
      <nav>
        <a href="/">Home</a>
        <a href="/workspace">Workspace</a>
      </nav>
      <h1>Launch FAQ</h1>
      <p>See the <a href="/register">registration page</a> to create an account (do not submit).</p>
    </body>
  </html>`);
});

app.listen(port, "0.0.0.0", () => {
  console.log(`Registration lab listening on ${port}`);
});
