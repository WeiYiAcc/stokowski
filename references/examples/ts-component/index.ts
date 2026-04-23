/**
 * greeter — minimal Polylith component example (TypeScript/Nx).
 *
 * Interface: re-export public API only.
 * Bases import from here via path alias: @project/greeter
 */

export { greet, greetAll } from "./core.js";
export type { GreetResult } from "./core.js";
