// Playground mode's runtime (shared/playground, from the ai-playground experiment), as text to put into a widget frame.
// Its own module so the app loads it only when a page has a playground: widget.ts imports this lazily. The export
// viewer inlines it, so exported pages run playgrounds offline.
// Order matters: each file reads the ones before it from window; entry.js (XW.playground) goes last.
import stateJs from "../../../../shared/playground/state.cjs?raw";
import helpersJs from "../../../../shared/playground/helpers.cjs?raw";
import sandboxJs from "../../../../shared/playground/sandbox.cjs?raw";
import structuresJs from "../../../../shared/playground/structures.cjs?raw";
import componentsJs from "../../../../shared/playground/components.cjs?raw";
import rendererJs from "../../../../shared/playground/renderer.cjs?raw";
import entryJs from "../../../../shared/playground/entry.js?raw";
import css from "../../../../shared/playground/components.css?raw";

export const PLAYGROUND_SOURCES = [stateJs, helpersJs, sandboxJs, structuresJs, componentsJs, rendererJs, entryJs];
export const PLAYGROUND_CSS = css;
