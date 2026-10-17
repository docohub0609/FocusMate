export const WEB_HEALTH = Object.freeze({
  service: "focusmate-web",
  status: "ok",
  version: "v1",
});

export function healthBody() {
  return JSON.stringify(WEB_HEALTH);
}
