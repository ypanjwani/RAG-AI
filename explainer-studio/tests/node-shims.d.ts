// The few Node-side modules tests/playground.test.ts uses. The project has no @types/node or @types/jsdom (its code
// runs in the browser), so these are declared loosely here.
declare module "jsdom";
declare module "node:module" {
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  export function createRequire(url: string): (id: string) => any;
}
