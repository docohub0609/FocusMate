import { createServer } from "node:http";
import { pathToFileURL, fileURLToPath } from "node:url";
import { createServer as createViteServer } from "vite";
export async function createWebServer() {
  const server = createServer((request, response) =>
    vite.middlewares(request, response),
  );
  const vite = await createViteServer({
    configFile: fileURLToPath(new URL("./vite.config.mjs", import.meta.url)),
    server: { middlewareMode: true, ws: { server } },
  });
  const close = server.close.bind(server);
  server.close = (callback) => {
    vite.close().then(() => close(callback), callback);
    return server;
  };
  return server;
}
if (
  process.argv[1] &&
  import.meta.url === pathToFileURL(process.argv[1]).href
) {
  const server = await createWebServer();
  const port = Number(process.env.FOCUSMATE_WEB_PORT || 5173);
  server.listen(port, process.env.FOCUSMATE_WEB_HOST || "127.0.0.1", () =>
    console.log(`FocusMate http://127.0.0.1:${port}`),
  );
  for (const signal of ["SIGINT", "SIGTERM"])
    process.on(signal, () => server.close(() => process.exit()));
}
