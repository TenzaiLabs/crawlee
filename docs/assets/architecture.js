window.addEventListener("DOMContentLoaded", async () => {
  if (!window.mermaid) {
    return;
  }

  window.mermaid.initialize({
    startOnLoad: false,
    securityLevel: "strict",
    theme: "base",
    themeVariables: {
      background: "transparent",
      primaryColor: "#d7f4f7",
      primaryTextColor: "#202426",
      primaryBorderColor: "#00a8bf",
      lineColor: "#00a8bf",
      secondaryColor: "#ffffff",
      tertiaryColor: "#eef5f6",
      fontFamily:
        'Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif',
    },
  });

  await window.mermaid.run({
    querySelector: ".mermaid",
  });
});
