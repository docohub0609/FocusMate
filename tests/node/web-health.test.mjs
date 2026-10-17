import assert from "node:assert/strict";
import { after, before, test } from "node:test";

import { createWebServer } from "../../apps/web/server.mjs";

let server;
let baseUrl;

before(async () => {
  server = await createWebServer({test:true});
  await new Promise((resolve, reject) => {
    server.once("error", reject);
    server.listen(0, "127.0.0.1", resolve);
  });
  const address = server.address();
  baseUrl = `http://127.0.0.1:${address.port}`;
});

after(async () => {
  await new Promise((resolve, reject) => server.close((error) => (error ? reject(error) : resolve())));
});

test("web shell exposes a stable health contract", async () => {
  const response = await fetch(`${baseUrl}/health`);
  assert.equal(response.status, 200);
  assert.deepEqual(await response.json(), {
    service: "focusmate-web",
    status: "ok",
    version: "v1",
  });
});

test("web serves the intake application and direct problem links", async () => {
  const response = await fetch(baseUrl);
  assert.equal(response.status, 200);
  assert.match(await response.text(), /src\/main.tsx/);
  const deep = await fetch(`${baseUrl}/problems/example/versions/1`);
  assert.equal(deep.status, 200);
  assert.match(await deep.text(), /src\/main.tsx/);
});
