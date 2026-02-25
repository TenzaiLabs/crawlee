const express = require("express");

const app = express();
const port = process.env.PORT || 8000;

app.use(express.urlencoded({ extended: false }));

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
      <h1>Registration Disabled</h1>
      <p>This endpoint intentionally rejects submissions for crawler safety tests.</p>
      <p><a href="/">Return home</a></p>
    </body>
  </html>`);
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
      <h1>Launch FAQ</h1>
      <p>See the <a href="/register">registration page</a> to create an account (do not submit).</p>
    </body>
  </html>`);
});

app.listen(port, "0.0.0.0", () => {
  console.log(`Registration lab listening on ${port}`);
});
