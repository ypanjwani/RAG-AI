// The explainer spec: the only thing the AI produces. Everything visual is
// rendered from it by fixed code in app/src/render/.
//
// These zod schemas double as the structured-output schemas sent to Claude, so
// they must stay within what structured outputs supports: every field required,
// no numeric/string length constraints, no recursion. Semantic checks (ranges,
// cross-references, expression syntax) live in validate.ts instead.

import { z } from "zod";
import { FONT_PAIRINGS, HEADING_STYLES, ICONS, LAYOUTS, LEVELS, THEMES, keys, type ColorMode, type Density, type Emphasis, type Extra, type Interactivity, type Level, type LevelChoice, type SectionTool, type TextSize, type ThemeName } from "./menus";

const md = z.string().describe("Short Markdown (bold, italics, inline code, lists, $inline math$). No HTML.");
const expr = z.string().describe("Expression in the formula language; may use declared variables, the block's defs, and view variables (x, y, t).");

/** Block-local derived variables, evaluated in order (later defs see earlier ones). */
const defs = z.array(z.object({
  name: z.string().describe("identifier"),
  expr,
})).optional().describe("Derived variables computed from sliders before drawing, e.g. [{name:'yhat', expr:'normal(mu, sigma, T)'}, {name:'m', expr:'mean(yhat)'}].");

// ---------- generic blocks ----------

const Text = z.object({ type: z.literal("text"), md }).describe("A paragraph or two of explanation.");

const Callout = z.object({
  type: z.literal("callout"),
  variant: z.enum(["info", "analogy", "warning", "key"]),
  title: z.string(),
  md,
}).describe("Highlighted box: info note, everyday analogy, common mistake (warning) or key takeaway.");

const Formula = z.object({
  type: z.literal("formula"),
  latex: z.string().describe("KaTeX display math, no surrounding $$."),
  caption: z.string(),
  terms: z.array(z.object({
    symbol: z.string().describe("KaTeX for the symbol"),
    meaning: z.string(),
    var: z.string().optional().describe("Slider/choice variable this symbol corresponds to; hovering links them."),
  })),
}).describe("Display equation with a plain-language legend of its symbols.");

const FlipCards = z.object({
  type: z.literal("flipcards"),
  cards: z.array(z.object({ front: z.string(), back: md })),
}).describe("2-6 cards; click to flip from a term/question to its explanation.");

const Reveal = z.object({
  type: z.literal("reveal"),
  prompt: z.string(),
  answer: md,
}).describe("A question the reader thinks about, then clicks to reveal the answer.");

const Tabs = z.object({
  type: z.literal("tabs"),
  tabs: z.array(z.object({ label: z.string(), md })),
}).describe("2-4 tabs showing alternative views (e.g. intuition / math / code).");

const Compare = z.object({
  type: z.literal("compare"),
  left: z.object({ title: z.string(), points: z.array(z.string()) }),
  right: z.object({ title: z.string(), points: z.array(z.string()) }),
}).describe("Side-by-side A vs B comparison.");

const Quiz = z.object({
  type: z.literal("quiz"),
  questions: z.array(z.object({
    q: z.string(),
    choices: z.array(z.string()).describe("3-4 choices"),
    answer: z.number().int().describe("0-based index of the correct choice"),
    explain: z.string(),
  })),
}).describe("1-3 multiple-choice questions checking this stage. Answering them completes the stage.");

const Predict = z.object({
  type: z.literal("predict"),
  question: z.string(),
  choices: z.array(z.string()),
  answer: z.number().int(),
  explain: md,
}).describe("Predict-then-reveal: reader commits to a guess before the explanation appears.");

// ---------- controls + live visuals (reactive variables) ----------

const Slider = z.object({
  type: z.literal("slider"),
  var: z.string().describe("Variable name (identifier) this slider sets; must be unique in the explainer."),
  label: z.string(),
  min: z.number(), max: z.number(), step: z.number(), value: z.number(),
  unit: z.string().describe("Unit suffix or empty string"),
}).describe("Slider that declares and sets a numeric variable used by plots/bars/readouts.");

const Choice = z.object({
  type: z.literal("choice"),
  var: z.string(),
  label: z.string(),
  options: z.array(z.object({ label: z.string(), value: z.number() })),
  value: z.number(),
}).describe("Segmented buttons that declare and set a variable to one of a few values (e.g. mode 0/1/2).");

const Plot = z.object({
  type: z.literal("plot"),
  title: z.string(),
  xLabel: z.string(), yLabel: z.string(),
  xMin: z.number(), xMax: z.number(),
  yMin: z.number(), yMax: z.number().describe("Set yMin = yMax to auto-scale."),
  defs,
  series: z.array(z.object({ label: z.string(), expr })),
  markerX: z.string().describe("Optional expression for a vertical marker line (e.g. a slider variable), or empty string."),
  shade: z.object({
    series: z.number().int().describe("0-based index of the series to shade under"),
    from: expr, to: expr,
    label: z.string().describe("e.g. 'Probability inside the interval'"),
  }).optional().describe("Shade the area under one series between two x values and show that area (e.g. probability mass)."),
  sweepVar: z.string().optional().describe("Plot against an existing slider variable instead of x: the x axis sweeps that variable over [xMin, xMax]; series may then use it by name."),
}).describe("Line chart of 1-3 functions of x that update live as variables change.");

const Bars = z.object({
  type: z.literal("bars"),
  title: z.string(),
  labels: z.array(z.string()),
  defs,
  values: z.array(expr).describe("One expression per label (constants allowed), OR a single expression returning a list (one bar per element, e.g. 'softmax(logits / T)')."),
  normalize: z.boolean().describe("Show as percentages of the total (e.g. probabilities)."),
}).describe("Bar chart whose bar heights are live expressions.");

const Readout = z.object({
  type: z.literal("readout"),
  defs,
  items: z.array(z.object({
    label: z.string(), expr, digits: z.number().int(),
    kind: z.enum(["number", "percent", "probability"]).optional().describe("probability = 0..1 shown as %; percent = already 0..100."),
  })),
}).describe("Big live numbers computed from variables, e.g. parameter counts or speed-ups.");

// ---------- equation views (pick by the equation's shape) ----------

const Samples = z.object({
  type: z.literal("samples"),
  title: z.string(),
  defs,
  values: expr.describe("Expression returning the list of samples, usually a def like 'yhat'."),
  xLabel: z.string(),
  markers: z.array(z.object({ label: z.string(), expr })).describe("Vertical lines, e.g. mean, true value."),
  band: z.object({ label: z.string(), from: expr, to: expr }).optional().describe("Shaded interval, e.g. mean ± z·std."),
  stats: z.array(z.object({
    label: z.string(), expr, digits: z.number().int(),
    kind: z.enum(["number", "percent", "probability"]).optional(),
  })).describe("Live numbers under the chart (may use the defs)."),
  caption: z.string(),
}).describe("Sums/means/std over random draws (Monte Carlo, MC-Dropout, ensembles, bootstrap): histogram of samples + markers + band, with a Resample button.");

const Heatmap = z.object({
  type: z.literal("heatmap"),
  title: z.string(),
  defs,
  expr: expr.describe("z as a function of x and y, e.g. a loss surface or a classifier's probability."),
  xMin: z.number(), xMax: z.number(), yMin: z.number(), yMax: z.number(),
  xLabel: z.string(), yLabel: z.string(), zLabel: z.string(),
  diverging: z.boolean().describe("true when z has a meaningful zero/midpoint (two colours), false for a single scale."),
  marker: z.object({ x: expr, y: expr, label: z.string() }).optional().describe("A point drawn on the map, e.g. the current parameters."),
  caption: z.string(),
}).describe("z = f(x, y) as a colour map: loss surfaces, decision boundaries, two-parameter trade-offs.");

const Trajectory = z.object({
  type: z.literal("trajectory"),
  title: z.string(),
  defs,
  state: z.array(z.object({ name: z.string(), init: expr })).describe("State variables and their starting values."),
  update: z.array(z.object({ name: z.string(), expr })).describe("New value of each state variable from the current state and t (all updated at once)."),
  steps: expr.describe("Number of steps (≤ 2000), may be a slider variable."),
  mode: z.enum(["time", "path"]).describe("time: plot series against step t; path: draw the (x, y) path, optionally over a surface."),
  series: z.array(z.object({ label: z.string(), expr })).describe("time mode: values to plot each step (e.g. the loss). Ignored in path mode."),
  logY: z.boolean().describe("time mode: log-scale y axis (good for losses)."),
  path: z.object({
    x: expr, y: expr,
    xMin: z.number(), xMax: z.number(), yMin: z.number(), yMax: z.number(),
    xLabel: z.string(), yLabel: z.string(),
    surface: z.string().describe("Optional z = f(x, y) drawn underneath (e.g. the loss), or empty string."),
  }).optional().describe("path mode only."),
  caption: z.string(),
}).describe("Update rules x_{t+1} = f(x_t): gradient descent, momentum, Adam, EMA, diffusion steps, RNN recurrences.");

const Matrix = z.object({
  type: z.literal("matrix"),
  title: z.string(),
  defs,
  items: z.array(z.object({
    op: z.string().describe("Symbol shown before this matrix: '', '+', '×', '=', '→' ..."),
    label: z.string(), expr,
  })).describe("Matrices/vectors shown left to right, e.g. W, +, B, ×, A, =, W'."),
  digits: z.number().int(),
  caption: z.string(),
}).describe("Matrix operations with live, colour-coded cells and shapes: LoRA (W + BA), linear layers, attention scores.");

const Calculator = z.object({
  type: z.literal("calculator"),
  title: z.string(),
  defs,
  rows: z.array(z.object({
    latex: z.string().describe("The quantity or step in KaTeX, e.g. '\\frac{1}{n}\\sum_i x_i'"),
    expr, note: z.string(), digits: z.number().int(),
  })),
  caption: z.string(),
}).describe("Worked example: an equation evaluated step by step with live numbers. Works for any equation.");

// ---------- teaching tools ----------

const setList = z.array(z.object({ var: z.string().describe("a slider/choice/toggle variable"), value: z.number() }));

const Cards = z.object({
  type: z.literal("cards"),
  variant: z.enum(["definition", "rule", "tip", "mistake", "claim", "keynumber"]),
  title: z.string().describe("Heading above the cards, or empty string"),
  items: z.array(z.object({
    title: z.string(),
    md,
    breaksIf: z.string().optional().describe("rule only: when the rule stops holding"),
    evidence: z.string().optional().describe("claim only: the result that supports it and where it comes from, e.g. 'Table 2: +3.1 BLEU'"),
    value: z.string().optional().describe("keynumber only: the headline figure as written, e.g. '10,000×' or '71.2%'"),
  })).describe("2-4 cards"),
}).describe("Card grid with a teaching job: definition (a term), rule (a property, plus when it breaks), tip (how to read a chart or apply an idea), mistake (a misconception and the correction), claim (a paper claim and its evidence), keynumber (one headline figure in context).");

const Toggle = z.object({
  type: z.literal("toggle"),
  var: z.string().describe("Variable set to 1 (on) or 0 (off); unique across the explainer."),
  label: z.string(),
  on: z.string().describe("Label for the on state, e.g. 'With dropout'"),
  off: z.string().describe("Label for the off state, e.g. 'Without dropout'"),
  value: z.number().int().describe("Initial state: 1 or 0"),
}).describe("Two-state switch for an on/off design choice (a component on or off, a method vs. its baseline).");

const Presets = z.object({
  type: z.literal("presets"),
  label: z.string(),
  items: z.array(z.object({ label: z.string(), set: setList })).describe("2-4 buttons"),
}).describe("Buttons that set several variables at once, e.g. 'Paper's setting', 'Tiny model', 'Too-high learning rate'.");

const Experiment = z.object({
  type: z.literal("experiment"),
  title: z.string(),
  defs,
  trial: expr.describe("Outcome of ONE run. Must involve randomness (normal/uniform/bernoulli, directly or through defs). A number, or a category index 0..k-1 when outcomes are given."),
  outcomes: z.array(z.string()).optional().describe("Category names when trial returns an index, e.g. which token was sampled or which class was predicted."),
  valueLabel: z.string().describe("What one run measures, e.g. 'accuracy on the batch'"),
  repeats: z.array(z.number().int()).describe("Run buttons, e.g. [1] or [1, 10, 100]. Use repeats > 1 only when the lesson is about averages, variance or convergence."),
  truth: z.string().optional().describe("Expected value (or, with outcomes, a list of true probabilities) that a reveal toggle shows next to the running estimate."),
  caption: z.string(),
}).describe("Repeatable random experiment with a live tally and running estimate. Only for genuinely random processes (sampling, random seeds, dropout, noisy evaluation, stochastic environments).");

const Examples = z.object({
  type: z.literal("examples"),
  title: z.string(),
  items: z.array(z.object({
    title: z.string().describe("A concrete scenario, e.g. 'A 7B model with rank 8'"),
    set: setList.describe("Variable values that load this scenario into the stage's widgets"),
    focus: md.describe("One sentence: what the reader should notice"),
    steps: z.array(z.string()).optional().describe("Leave out; filled in automatically from computed numbers."),
  })).describe("2-4 worked examples"),
}).describe("Worked examples for this stage, each with a 'Try it' button that loads its settings into the stage's sliders and graphs.");

const Table = z.object({
  type: z.literal("table"),
  title: z.string(),
  columns: z.array(z.string()),
  rows: z.array(z.array(z.string())).describe("Cells are short Markdown. A cell starting with '=' is a live expression, e.g. '=2*d*r'."),
  digits: z.number().int().optional().describe("Decimals for live cells (default 2)"),
  highlight: z.object({ row: z.number().int(), col: z.number().int() }).optional().describe("0-based cell to emphasise (use -1 for a whole column or row)."),
  caption: z.string(),
}).describe("Table: the paper's results, method comparisons, a cheat sheet, or values that update with sliders.");

const ConceptMap = z.object({
  type: z.literal("conceptmap"),
  title: z.string(),
  nodes: z.array(z.object({
    id: z.string(), label: z.string(),
    sub: z.string().describe("Short subtitle, e.g. a formula or one-line role"),
    desc: z.string().describe("1-2 sentences"),
    stage: z.string().describe("Id of the stage that explains it, or empty string"),
  })).describe("4-9 key ideas"),
  edges: z.array(z.object({ from: z.string(), to: z.string(), label: z.string().describe("1-2 words") })),
}).describe("Clickable map of how the explainer's key ideas connect; usually in the last stage.");

// ---------- AI-specific components ----------

const Tokens = z.object({
  type: z.literal("tokens"),
  text: z.string(),
  tokens: z.array(z.string()).describe("Realistic subword tokens of text, in order (use '▁' or a leading space for word starts)."),
  caption: z.string(),
}).describe("Token viewer: text split into colored tokens with IDs.");

const Attention = z.object({
  type: z.literal("attention"),
  tokens: z.array(z.string()).describe("4-8 tokens"),
  weights: z.array(z.array(z.number())).describe("Square matrix (len(tokens)^2); row i = how much token i attends to each token. Raw scores; rows are softmaxed by the renderer."),
  caption: z.string(),
}).describe("Attention heatmap with a built-in temperature slider; hover a row to see one token's attention.");

const Sampling = z.object({
  type: z.literal("sampling"),
  context: z.string().describe("The text so far, e.g. 'The cat sat on the'"),
  candidates: z.array(z.object({ token: z.string(), logit: z.number() })).describe("5-8 candidate next tokens with raw logits"),
}).describe("Next-token sampling lab with built-in temperature, top-k and top-p controls.");

const Embedding = z.object({
  type: z.literal("embedding"),
  points: z.array(z.object({ label: z.string(), x: z.number(), y: z.number(), group: z.string() })).describe("6-20 points in a 2D map, coordinates roughly -1..1"),
  caption: z.string(),
}).describe("2D embedding map; click a point to see its nearest neighbours.");

const Architecture = z.object({
  type: z.literal("architecture"),
  nodes: z.array(z.object({ id: z.string(), label: z.string(), detail: md })),
  edges: z.array(z.object({ from: z.string(), to: z.string() })),
}).describe("Model architecture block diagram (DAG of 3-10 boxes); click a box to read what it does. At most once, and only for a real model architecture.");

const Pipeline = z.object({
  type: z.literal("pipeline"),
  steps: z.array(z.object({ label: z.string(), input: z.string(), output: z.string(), detail: md })),
}).describe("Step-through of a real data-transforming sequence (input -> output per step on one running example). At most once per explainer, and only when the method truly is such a sequence.");

const Training = z.object({
  type: z.literal("training"),
  title: z.string(),
  caption: z.string(),
  showValidation: z.boolean().describe("Also show a validation curve that overfits with too many epochs."),
}).describe("Training playground: built-in learning-rate and epochs sliders drive a simulated loss curve.");

/** The views a chart block can ask for; validate.ts checks the view fits the formula's shape. */
export const CHART_VIEWS = ["line", "lines", "heatmap", "histogram", "compare", "running", "bars", "scatter", "band", "box", "groupedbars"] as const;
export type ChartView = (typeof CHART_VIEWS)[number];

/**
 * A chart chosen by what it must teach: the AI gives the formula, which page
 * variables go on the axes and the lesson, plus a view from a fixed list; code
 * checks the view fits the formula and takes ranges and axis titles from the
 * sliders. Offered to the planner only behind the CHART_BLOCK switch
 * (PromptBlockChart), so it isn't in AI_BLOCKS / PromptBlock.
 */
export const Chart = z.object({
  type: z.literal("chart"),
  title: z.string(),
  lesson: z.string().describe("One sentence: what the reader should notice in this chart, e.g. 'The spread of the average shrinks as B grows.' Write it first, then choose the view that shows it."),
  defs,
  series: z.array(z.object({ label: z.string(), expr })).describe("1-3 quantities to show. Use the page's control variables (by name) and defs; never type in a computed number."),
  x: z.string().describe("A slider variable for the horizontal axis (line, lines, heatmap, band, and scatter against a setting): its slider gives the range and the axis title. Empty string otherwise."),
  by: z.string().describe("A second slider variable: one line per value (lines), the vertical axis (heatmap), the settings compared (compare), or one box per value (box). Empty string otherwise."),
  at: z.array(z.number()).describe("Values of `by` to show: 2-4 for lines and box; for compare, the first value is the fixed baseline (e.g. [1]) and the other histogram follows the slider. Empty array = the slider's min, middle and max."),
  view: z.enum(CHART_VIEWS).describe("line: each series against x (not random) · lines: the first series against x, one line per value of by · heatmap: the first series over x and by · histogram: many random draws at the current settings · compare: two histograms of the random series, one at the baseline value of by (the first value of at) and one at the slider's current value · running: the running average of a random series over repeated runs (only when the lesson is that an estimate settles down) · bars: a series that returns a list, one bar per element · scatter: random points; with x, draws of each random series at values across x's range (a non-random series is drawn as a line, e.g. the true curve); without x, the first two series give each point's horizontal and vertical position · band: the average of a random series against x with a shaded band of ± one standard deviation, so the reader sees the spread grow or shrink as x changes · box: one box plot of the random series per value of by (the at values plus the slider's current value), to compare spreads across several settings · groupedbars: 2-3 series that each return a list of the same length, drawn side by side per label (e.g. probabilities at two temperatures)"),
  labels: z.array(z.string()).describe("bars and groupedbars only: one label per list element. Empty array otherwise."),
  caption: z.string(),
}).describe("A chart chosen by what it must teach: write the lesson, the formula and which slider variables go on the axes, then the view that shows the lesson. Prefer it over plot, samples, heatmap and experiment.");

/** What can appear in a scene; each kind is drawn by prebuilt code (render/components/scene.ts). */
export const SCENE_KINDS = ["slider", "button", "chart", "readout", "bars", "table", "text"] as const;
/** What can start a scene rule. */
export const SCENE_TRIGGERS = ["click", "change", "every", "start"] as const;

const SceneElement = z.object({
  id: z.string().describe("Unique identifier within the scene, e.g. 'play' or 'spread'"),
  kind: z.enum(SCENE_KINDS),
  visible: z.string().optional().describe("Formula: shown while it is non-zero (e.g. 'step >= 2'). Leave out to always show."),
  label: z.string().optional().describe("slider/button/readout/bars/table/chart: its label or title"),
  var: z.string().optional().describe("slider: the scene state variable it sets"),
  disabled: z.string().optional().describe("button: formula, greyed out while non-zero (e.g. 'running')"),
  series: z.array(z.object({ label: z.string(), expr })).optional().describe("chart: 1-3 formulas"),
  x: z.string().optional().describe("chart: variable on the x axis (a scene state variable or a page slider)"),
  by: z.string().optional().describe("chart: second variable (lines, heatmap, compare, box)"),
  at: z.array(z.number()).optional().describe("chart: values of by (lines, box), or the baseline first (compare)"),
  view: z.enum(CHART_VIEWS).optional().describe("chart: the view, as in the chart block"),
  expr: expr.optional().describe("readout: the number to show"),
  digits: z.number().int().optional().describe("readout: decimals (default 2)"),
  format: z.enum(["number", "percent", "probability"]).optional().describe("readout: probability = 0..1 shown as %"),
  labels: z.array(z.string()).optional().describe("bars: one label per element; chart (bars and groupedbars views): the same"),
  values: expr.optional().describe("bars: one formula returning a list (one bar per element)"),
  highlight: z.string().optional().describe("bars: formula giving the index to highlight; table: the row index"),
  columns: z.array(z.string()).optional().describe("table: column headers"),
  rows: z.array(z.array(z.string())).optional().describe("table: cells; a cell starting with '=' is a live formula"),
  md: z.string().optional().describe("text: short Markdown; {{formula}} is replaced by its live value"),
});

const SceneRule = z.object({
  when: z.enum(SCENE_TRIGGERS).describe("click: an element is clicked · change: a variable changes · every: a timer · start: when the page opens"),
  target: z.string().optional().describe("click: the element id; change: the variable name"),
  ms: z.number().optional().describe("every: milliseconds between ticks (30 or more)"),
  if: z.string().optional().describe("Formula: the rule runs only while it is non-zero, e.g. 'running * (B < 100)' (* is and; max(a, b) is or)"),
  set: z.array(z.object({ var: z.string().describe("a scene state variable"), expr })).describe("Assignments, all computed first, then applied. In a click rule, x (chart), i (bars) or row (table) is what was clicked."),
});

/**
 * Lab mode (scene-plus): one section's interaction designed as a whole after the content is written.
 * State variables, prebuilt elements whose properties can be live formulas, and rules
 * "when <trigger> [if <condition>], set <variable> = <formula>". Checked by shared/scene.ts.
 * Not offered to the Interactive planner (not in PromptBlock); the scene designer writes it.
 */
export const Scene = z.object({
  type: z.literal("scene"),
  title: z.string(),
  lesson: z.string().describe("The one thing this interaction should teach"),
  state: z.array(z.object({
    name: z.string().describe("identifier, not a page control's name"),
    label: z.string(),
    value: z.number(), min: z.number(), max: z.number(),
    step: z.number().describe("slider step; 1 for counters and switches"),
  })).describe("The scene's own variables (at most 8); rules may only set these"),
  defs,
  elements: z.array(SceneElement).describe("At most 10 elements, shown in order"),
  rules: z.array(SceneRule).describe("At most 12 rules, at most 2 'every' timers"),
  layout: z.enum(["stack", "grid"]),
  caption: z.string(),
}).describe("A connected interaction: state, elements whose properties may be formulas, and rules that set state when something happens.");

/**
 * Freeform mode only: an interactive widget whose HTML/CSS/JS Haiku wrote
 * itself (the one exception to "the AI never writes code"). It runs in a
 * sandboxed iframe with no network, storage or access to the app. Never shown
 * to the planner: it isn't in PromptBlock.
 */
export const Widget = z.object({
  type: z.literal("widget"),
  title: z.string(),
  html: z.string().describe("A self-contained HTML fragment with inline <style> and <script>"),
  plan: z.string().optional().describe("Code Lab only: the interaction plan (compact JSON) the widget was built from; never shown"),
});

const AI_BLOCKS = [
  Text, Callout, Formula, FlipCards, Reveal, Tabs, Compare, Quiz, Predict,
  Slider, Choice, Plot, Bars, Readout,
  Samples, Heatmap, Trajectory, Matrix, Calculator,
  Cards, Toggle, Presets, Experiment, Examples, Table, ConceptMap,
  Tokens, Attention, Sampling, Embedding, Architecture, Pipeline, Training,
] as const;
/** Blocks the planner may write: the schema embedded in its prompt. */
export const PromptBlock = z.union(AI_BLOCKS);
/** PromptBlock plus chart: the planner's blocks when the CHART_BLOCK test switch is on. */
export const PromptBlockChart = z.union([...AI_BLOCKS, Chart]);
/** Every block a spec can hold (PromptBlock plus chart and code-built ones like widget). */
export const Block = z.union([...AI_BLOCKS, Chart, Scene, Widget]);
export type Block = z.infer<typeof Block>;
export type BlockType = Block["type"];

/** Each block's schema by type (lab mode sends only the chosen component's schema to the filler). */
export const BlockSchemas = {
  text: Text, callout: Callout, formula: Formula, flipcards: FlipCards, reveal: Reveal, tabs: Tabs, compare: Compare, quiz: Quiz, predict: Predict,
  slider: Slider, choice: Choice, plot: Plot, bars: Bars, readout: Readout,
  samples: Samples, heatmap: Heatmap, trajectory: Trajectory, matrix: Matrix, calculator: Calculator,
  cards: Cards, toggle: Toggle, presets: Presets, experiment: Experiment, examples: Examples, table: Table, conceptmap: ConceptMap,
  tokens: Tokens, attention: Attention, sampling: Sampling, embedding: Embedding, architecture: Architecture, pipeline: Pipeline, training: Training,
  chart: Chart, scene: Scene, widget: Widget,
} satisfies Record<BlockType, z.ZodTypeAny>;
export type BlockOf<T extends BlockType> = Extract<Block, { type: T }>;

// ---------- Planner output: the teaching outline ----------

export const Stage = z.object({
  id: z.string().describe("kebab-case, unique"),
  label: z.string().describe("2-4 word stage name"),
  phase: z.string().describe("id of one of the phases"),
  blurb: z.string().describe("One sentence shown on the overview card."),
  blocks: z.array(Block),
});
export type Stage = z.infer<typeof Stage>;

export const Outline = z.object({
  title: z.string(),
  subtitle: z.string(),
  level: z.enum(keys(LEVELS)),
  layout: z.enum(keys(LAYOUTS)),
  keyIntuition: z.string().describe("The one sentence a reader should remember."),
  phases: z.array(z.object({ id: z.string(), label: z.string() })).describe("1-3 groups of stages"),
  stages: z.array(Stage).describe("4-8 stages in teaching order, or the number the request asks for"),
  glossary: z.array(z.object({ term: z.string(), definition: z.string() })),
});
export type Outline = z.infer<typeof Outline>;

/** The outline schema shown to the planner in quality mode (blocks limited to PromptBlock). */
export const PromptOutline = Outline.extend({
  stages: z.array(Stage.extend({ blocks: z.array(PromptBlock) })).describe("4-8 stages in teaching order, or the number the request asks for"),
});

/** PromptOutline with the chart block: the planner's schema when the CHART_BLOCK test switch is on. */
export const PromptOutlineChart = Outline.extend({
  stages: z.array(Stage.extend({ blocks: z.array(PromptBlockChart) })).describe("4-8 stages in teaching order, or the number the request asks for"),
});

/**
 * The outline as parsed from the planner's reply: blocks are checked one by one
 * (see parseOutline) so one bad block is dropped instead of failing everything.
 * The planner doesn't use structured outputs because the full Block union compiles
 * to a grammar the API rejects as too large; the schema goes in the prompt.
 */
export const LooseOutline = Outline.extend({
  stages: z.array(Stage.extend({ blocks: z.array(z.unknown()) })),
});
export type LooseOutline = z.infer<typeof LooseOutline>;

const str = (v: unknown): string | undefined => (typeof v === "string" && v.trim() ? v.trim() : undefined);

/**
 * Accept the planner's outline with small slips instead of rejecting it (a
 * rejection costs a full rewrite): missing or invalid top-level fields get
 * defaults, a broken stage is dropped, each repair is reported as a warning.
 * Only an outline with no usable stage is an error. assembleSpec later fixes
 * stage ids and phases.
 */
export function coerceOutline(raw: unknown): { data: LooseOutline; warnings: string[] } | { error: string } {
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) return { error: "the reply is not a JSON object" };
  const o = raw as Record<string, unknown>;
  const warnings: string[] = [];
  const fix = (field: string, used: string) => warnings.push(`Outline field "${field}" was missing or invalid; used ${used}.`);

  const stages: LooseOutline["stages"] = [];
  (Array.isArray(o.stages) ? o.stages : []).forEach((s, i) => {
    const st = s && typeof s === "object" && !Array.isArray(s) ? (s as Record<string, unknown>) : null;
    const label = str(st?.label) ?? str(st?.id);
    if (!st || !label) { warnings.push(`Dropped stage ${i + 1}: it had no label.`); return; }
    if (!Array.isArray(st.blocks)) warnings.push(`Stage "${label}" had no block list.`);
    stages.push({
      id: str(st.id) ?? label,
      label,
      phase: str(st.phase) ?? "",
      blurb: str(st.blurb) ?? "",
      blocks: Array.isArray(st.blocks) ? st.blocks : [],
    });
  });
  if (!stages.length) return { error: "no usable stages" };

  const title = str(o.title);
  if (!title) fix("title", '"Untitled explainer"');
  const subtitle = str(o.subtitle) ?? "";
  const level = Object.keys(LEVELS).includes(o.level as string) ? (o.level as LooseOutline["level"]) : (fix("level", '"student"'), "student" as const);
  const layout = Object.keys(LAYOUTS).includes(o.layout as string) ? (o.layout as LooseOutline["layout"]) : (fix("layout", '"article"'), "article" as const);
  const keyIntuition = str(o.keyIntuition) ?? (fix("keyIntuition", "the subtitle"), subtitle);
  const phases = (Array.isArray(o.phases) ? o.phases : []).flatMap((p) => {
    const id = str((p as Record<string, unknown>)?.id), label = str((p as Record<string, unknown>)?.label);
    return id && label ? [{ id, label }] : [];
  });
  const glossary = (Array.isArray(o.glossary) ? o.glossary : []).flatMap((g) => {
    const term = str((g as Record<string, unknown>)?.term), definition = str((g as Record<string, unknown>)?.definition);
    return term && definition ? [{ term, definition }] : [];
  });
  return { data: { title: title ?? "Untitled explainer", subtitle, level, layout, keyIntuition, phases, stages, glossary }, warnings };
}

const CARD_VARIANTS = ["definition", "rule", "tip", "mistake", "claim", "keynumber"];

/**
 * Fix a common slip: a card variant used as the block type, e.g.
 * {type:"tip", title, md} instead of {type:"cards", variant:"tip", items:[...]}.
 */
export function normalizeBlock(b: unknown): unknown {
  const o = b as Record<string, unknown> | null;
  if (!o || typeof o.type !== "string" || !CARD_VARIANTS.includes(o.type)) return b;
  const { type, title, items, ...item } = o;
  return {
    type: "cards",
    variant: type,
    title: Array.isArray(items) ? (title ?? "") : "",
    items: Array.isArray(items) ? items : [{ title: title ?? "", md: item.md ?? item.text ?? "", ...item }],
  };
}

/** Validate a raw outline; returns the outline plus warnings for dropped blocks. */
export function parseOutline(raw: unknown): { outline: Outline; warnings: string[] } | { error: string } {
  const loose = coerceOutline(raw);
  if ("error" in loose) return loose;
  const warnings = loose.warnings;
  const stages = loose.data.stages.map((st) => ({
    ...st,
    blocks: st.blocks.flatMap((b, i) => {
      const r = Block.safeParse(normalizeBlock(b));
      if (r.success) return [r.data];
      const type = (b as { type?: unknown })?.type;
      warnings.push(`Dropped block ${i + 1} ("${typeof type === "string" ? type : "unknown"}") in "${st.label}": ${r.error.issues[0]?.message ?? "invalid"}.`);
      return [];
    }),
  }));
  return { outline: { ...loose.data, stages }, warnings };
}

// ---------- Haiku output: design + chatbot ----------

export const Design = z.object({
  theme: z.enum(keys(THEMES)),
  fontPairing: z.enum(keys(FONT_PAIRINGS)),
  headingStyle: z.enum(keys(HEADING_STYLES)),
  stageIcons: z.array(z.object({ stageId: z.string(), icon: z.enum(keys(ICONS)) })),
  chatbot: z.object({
    systemPrompt: z.string(),
    starters: z.array(z.string()).describe("3 short starter questions"),
  }),
});
export type Design = z.infer<typeof Design>;

// ---------- Assembled spec (what the app stores and renders) ----------

export interface ExplainerSpec {
  version: 1;
  title: string;
  subtitle: string;
  level: Outline["level"];
  layout: Outline["layout"];
  keyIntuition: string;
  design: {
    theme: Design["theme"] | "custom";
    fontPairing: Design["fontPairing"];
    headingStyle: Design["headingStyle"];
    /** The reader's own accent (#rrggbb) when theme is "custom" (set in Edit chat; kept by revisions). */
    accent?: string;
    /** Light/dark fixed for this page (absent = follow the app setting). */
    mode?: ColorMode;
    /** Page brief (Quality+/Lite+): look-and-feel picks. Absent = normal / comfortable / both. */
    textSize?: TextSize;
    density?: Density;
    emphasis?: Emphasis;
  };
  phases: Outline["phases"];
  stages: (Stage & { icon: string })[];
  glossary: Outline["glossary"];
  chatbot: Design["chatbot"] & { context: string };
  /** Lite+: no "Ask about this" chatbot (chatbot.context is still kept for revisions). Absent = shown. */
  noAsk?: true;
  /** Lite+: controls aren't rendered; visuals stay at the controls' default values. Absent = shown. */
  noControls?: true;
  /** Typed concept with no source text: claim and keynumber cards are marked as from the AI's memory. Absent = not marked. */
  fromMemory?: true;
  /** Quality+/Freeform+ fact check (server/explainer/factcheck.py): statements checked, flagged by Jev, fixed by Haiku. Absent = not checked. */
  factCheck?: { checked: number; flagged: number; fixed: number };
  /** Quality+/Freeform+ clarity check (server/explainer/clarity.py): stages judged by Jev, flagged, blocks added and texts edited by Haiku, still flagged after. Absent = not checked. */
  clarity?: { stages: number; flagged: number; added: number; edited: number; remaining: number };
  /** Quality+ "Add to each section": buttons on every section that call the server when clicked (POST /api/section; app only, never in exports). Absent = none. */
  sectionTools?: SectionTool[];
  /** Quality+ "Add to each section": a deeper explanation and/or the same explanation in other words, per stage id, written with the page and opened from the section's buttons. Absent = none. */
  sectionNotes?: Record<string, SectionNotes>;
  /** A Quality+ revision made as changes (server/explainer/revise.py): stages added, rewritten, removed and kept as they were. Absent = written whole. */
  revision?: { added: number; replaced: number; removed: number; kept: number; charts?: number };
  /** Smart mode (server/explainer/smart.py): sections written as quick, interactive and creative, custom widgets built, and how many sections Jev judged (the rest fell back to Interactive). Absent = not Smart. */
  smart?: { quick: number; interactive: number; creative: number; custom: number; byJev: number };
  /** "Go deeper" on formula blocks (POST /api/formula): explanations written when a reader first clicks, saved here so
   * reopening and exports show them. Keyed by formulaKey(latex). Absent = none opened yet. */
  formulaNotes?: Record<string, FormulaNote>;
  /** "Add section" (server/explainer/addsection.py): where Jev placed the new section ("after “X”"), its probability, "end" when it fell back to the end, and a stage id that may already cover it. */
  addition?: { after: string; p: number; placed: "jev" | "end"; covered?: string };
  warnings: string[];
}

// ---------- API wire types ----------

export interface PaperSection { heading: string; text: string }

/**
 * Haiku always plans. quality-plus ("Quality+", default): the planner writes and chooses everything,
 * then Haiku picks the design; a clarity check (Jev judges every stage for ideas used before they are
 * explained or too hard for the level, Haiku adds or simplifies what it flags) runs before the fact check.
 * freeform-plus ("Freeform+"): the planner writes briefs for interactive
 * parts and Haiku writes each one as its own HTML/JS widget (sandboxed). lite-plus ("Lite+"): a static
 * quality-plus page: the same visuals drawn at fixed values, with no controls, quizzes, questions, worked
 * examples, click-to-use widgets or Ask chatbot (LITE_DROPPED; the spec gets noAsk and noControls).
 * Older records may carry retired modes (quality, quality-fixed, custom, lab, cheap, economy, compact-plus,
 * quality-2 (Quality 2.0, merged into quality-plus in Oct 2026), freeform); the server maps them to
 * quality-plus or freeform-plus.
 * smart (server/explainer/smart.py): Haiku lists the sections, Jev picks each one's writing style (quick | interactive |
 * creative) and visual (none | built-in | custom widget), and the Quality+ planner writes the page to match; widgets
 * are written only where Jev picked them. No review rounds; a revision of a Smart page runs as quality-plus.
 * quality-deep (the composer's "Review rounds", shown as Interactive; server/explainer/deepen.py): a quality-plus page, then up to
 * reviewRounds (default 3) rounds of
 * verify (code, Jev and Haiku checks per section) -> audit (Haiku decides what is real and what to do) -> improve
 * (a revision applying only the audit). Only the final page is shown.
 */
/** scene-plus ("Lab", beta): an Interactive page, then a second pass designs each section's interaction as a scene (server/explainer/scenes.py). */
/** code-lab ("Code Lab", beta): Haiku writes the prose, then designs and writes each section's interactive code from scratch; every widget is checked by running it (server/explainer/codelab.py).
 * code-lab-2 ("Code Lab 2.0", beta): the same, with the page's one Effort for every Haiku step, and chart-like sections drawn as Vega-Lite charts.
 * code-lab-3 ("Code Lab 3.0", beta): Code Lab 2.0 with a chart-only plan (fewer fields; controls read from the chart), so it costs less.
 * code-lab-4 ("Code Lab 4.0", beta): Code Lab 3.0, but the page writer picks the few sections that get a chart and Jev checks the page for repeats.
 * code-lab-5 ("Code Lab 5.0", beta): Code Lab 4.0's writer picks and repeat check, but each picked section's interactive part is Haiku's own code (no Vega), at the page's Effort.
 * code-lab-6 ("Code Lab 6.0", beta): a deeper page; the writer picks the sections and Haiku plans and builds each interactive part, with no widget test run, repairs or Jev review (only free static checks). Jev still fact-checks and clarity-checks the text.
 * code-lab-7 ("Code Lab 7.0", beta): Code Lab 5.0 with Creative's freedom for the builder: the plan fixes what each interactive part shows, Haiku designs how it looks (kit optional, ~300 lines); same checks, repairs and review.
 * playground ("Playground", beta): Code Lab 5.0's writer marks and Jev checks, but each marked section gets a playground spec (shared/playground, from the ai-playground experiment: fixed components plus short snippets, with dragging, clicking and play), dry-run by scripts/check-playground.mjs and repaired from its errors. */
export type Quality = "quality-plus" | "quality-deep" | "freeform-plus" | "lite-plus" | "smart" | "scene-plus" | "code-lab" | "code-lab-2" | "code-lab-3" | "code-lab-4" | "code-lab-5" | "code-lab-6" | "code-lab-7" | "playground";

/** Quality, Quality 2.0 and Lab share the page, the extras question and the Add section button. */
export const isQuality = (q: Quality | undefined) => q === undefined || q === "quality-plus" || q === "quality-deep" || q === "scene-plus" || q === "code-lab" || q === "code-lab-2" || q === "code-lab-3" || q === "code-lab-4" || q === "code-lab-5" || q === "code-lab-6" || q === "code-lab-7" || q === "playground";

/**
 * Lite+: block types the planner is told not to write and code strips if it does: questions, and
 * widgets that show nothing until used (flip cards hide their text, experiment/sampling/training need runs).
 * Controls are not stripped (charts read their values); a noControls page hides them instead.
 */
export const LITE_DROPPED = new Set<string>(["quiz", "reveal", "predict", "examples", "flipcards", "experiment", "sampling", "training"]);
/** Control blocks: the planner is told to write none in Lite+, and a noControls page doesn't render them. */
export const CONTROL_TYPES = new Set<string>(["slider", "choice", "toggle", "presets"]);

/** Modes where Jev first skips side-material sections of a PDF (server/explainer/filter.py). */
export const filtersSections = (q: Quality | undefined) => q === "quality-plus" || q === "quality-deep" || q === "freeform-plus" || q === "lite-plus" || q === "smart" || q === "scene-plus" || q === "code-lab" || q === "code-lab-2" || q === "code-lab-3" || q === "code-lab-4" || q === "code-lab-5" || q === "code-lab-6" || q === "code-lab-7" || q === "playground";

/** Modes where Jev answers the page brief (level, length, approach, look...) before the plan (server/explainer/brief.py). */
export const usesBrief = (q: Quality | undefined) => q === "quality-plus" || q === "quality-deep" || q === "lite-plus" || q === "smart" || q === "scene-plus" || q === "code-lab" || q === "code-lab-2" || q === "code-lab-3" || q === "code-lab-4" || q === "code-lab-5" || q === "code-lab-6" || q === "code-lab-7" || q === "playground";

/** Thinking effort for planning and widget writing (composer toggle). High raises planning only; widgets are written at medium. */
export type GenEffort = "low" | "medium" | "high";

/** A palette and light/dark stamped onto a spec (revisions keep the reader's custom accent). accent (#rrggbb) is required when palette is "custom". */
export interface DesignPrefs { palette: ThemeName | "custom"; accent?: string; colorMode: ColorMode }

/**
 * Fields every request may carry. interactivity: the composer's Mostly reading | Balanced | Hands-on toggle (absent = balanced).
 * level: the composer's Level toggle (absent or "auto" = Jev picks it in the brief modes).
 * extras: the answer to "What do you want to add to each section?" (Quality+ only). Absent = not asked (the planner's
 * usual worked examples); empty = None (no worked examples).
 * reviewRounds: the most review rounds after the page, not on lite-plus (1-10; the composer's "Until clean" sends 10;
 * absent = none, except quality-deep, which then has 3). Interactive with rounds is sent as quality-deep.
 * planThinking: Code Lab 2.0, 3.0 and 4.0 only; false plans, repairs and backs up its charts with Haiku's thinking off (absent = on).
 * colorMode: the composer's Page colours; stamped as spec.design.mode, so the page opens and exports in it (absent = follow the reader).
 */
interface RequestOptions { quality?: Quality; effort?: GenEffort; interactivity?: Interactivity; level?: LevelChoice; extras?: Extra[]; reviewRounds?: number; planThinking?: boolean; colorMode?: Exclude<ColorMode, "auto">; followUp?: FollowUp }

/**
 * A follow-up typed under an explainer in the same conversation (shared/followup.ts builds it).
 * revise: plan a new version from the previous one (its outline as JSON), keeping its design.
 * new: a related explainer that knows what the conversation already covered.
 * source: the earlier explainer's source excerpt (paper summaries), so papers aren't re-read.
 * kept: what the previous version had already written for its stages (worked-example steps keyed
 * "<stage id>:<block>:<item>", section extras by stage id); a revision that only changes some stages
 * reuses them for the stages it keeps instead of writing them again.
 * widgets: the previous version's widget blocks by stage id, in page order. The outline only holds a placeholder
 * for each ("(interactive widget: <title>)"), and the server puts the widget back where its placeholder survives.
 * add: "Add section" (Quality+): only add new stages, placed by Jev; nothing on the page changes (server/explainer/addsection.py).
 */
/** One part of the page a revision changes (server/explainer/targets.py): a section's widget, its text, the whole section, or a new section after `after`. */
export interface FollowUpTarget { part: "widget" | "text" | "section" | "new-section"; stage: string; after: string; what: string }
/** Feedback v2 (server/explainer/feedback.py): the change the reader confirmed. Only these parts change; pageWide = the whole page. */
export interface FeedbackRun { id: string; targets: FollowUpTarget[]; keep: string[]; pageWide: boolean }

export type FollowUp =
  | { kind: "revise"; outline: string; design: Design; accent?: string; colorMode?: ColorMode; source?: string; kept?: KeptParts; widgets?: Record<string, Extract<Block, { type: "widget" }>[]>; add?: true; feedback?: FeedbackRun }
  | { kind: "new"; previous: string; source?: string };

/** POST /api/route: does a follow-up change the explainer above, or ask for a new one? */
export interface RouteRequest { text: string; title: string; stages: string[] }
export interface RouteResponse { kind: "revise" | "new" | "unclear"; usd: number }

export type GenerateRequest =
  | ({ mode: "concept"; text: string } & RequestOptions)
  | ({ mode: "paper"; title: string; note: string; sections: PaperSection[] } & RequestOptions);

export type ProgressStep = "read" | "summarize" | "plan" | "components" | "interaction" | "design" | "build" | "examples" | "notes" | "clarity" | "check" | "improve";

/** Token usage of one Claude call, priced by server/explainer/pricing.py. */
export interface UsageStep {
  step: string;
  model: string;
  input: number;
  output: number;
  cacheRead: number;
  cacheWrite: number;
  usd: number;
}
/** quality is a string: old records hold retired mode names, shown by their label. */
export interface Usage { quality: string; effort?: GenEffort; steps: UsageStep[]; totalUsd: number; seconds: number }

export type GenerateEvent =
  | { type: "step"; step: ProgressStep; status: "running" | "done"; detail?: string }
  | { type: "done"; spec: ExplainerSpec; usage?: Usage }
  | { type: "error"; message: string };

/**
 * Edit chat (POST /api/edit): one Haiku turn. Haiku only chooses tools; the
 * browser runs them (shared/edit.ts) and sends the results back in the next
 * request, so the server stays stateless. `context` describes the selected part.
 */
export type EditContent =
  | { type: "text"; text: string }
  | { type: "tool_use"; id: string; name: string; input: Record<string, unknown> }
  | { type: "tool_result"; tool_use_id: string; content: string; is_error?: boolean };
export interface EditMessage { role: "user" | "assistant"; content: string | EditContent[] }
export interface EditRequest { context: string; messages: EditMessage[] }
export interface EditResponse { content: EditContent[]; usd: number }

/**
 * A section tool (POST /api/section), run when the reader clicks its button: one Haiku call that searches the web
 * about the section. text is the section's prose; previous holds earlier answers for the same section, so a repeat
 * says something new.
 */
export interface SectionNotes { deeper?: string; another?: string }

/** A revision's reuse of the previous version (FollowUp.kept). */
export interface KeptParts { steps?: Record<string, string[]>; notes?: Record<string, SectionNotes> }

export interface SectionRequest { kind: SectionTool; title: string; level: Level; stage: string; text: string; previous?: string[] }
export interface SectionSource { title: string; url: string }
export interface SectionResponse { md: string; sources: SectionSource[]; searches: number; usd: number }

/** "Go deeper" on a formula (FORMULA_DEEPER_SYSTEM): every term, where it comes from, why it looks like this, a tiny
 * worked example and a common misreading. Text fields are Markdown with $inline math$. */
export interface FormulaNote {
  idea: string;
  terms: { symbol: string; name: string; meaning: string; role: string }[];
  origin: string[];
  shape: string;
  example: string[];
  pitfall: string;
}
export interface FormulaRequest {
  title: string;
  level: Level;
  stage: string;
  latex: string;
  caption: string;
  terms: { symbol: string; meaning: string }[];
  /** The section's prose, for context (sectionText). */
  text: string;
}
export interface FormulaResponse { note: FormulaNote; usd: number }

/** The key a formula's saved note is stored under (spec.formulaNotes): its LaTeX with whitespace collapsed. */
export const formulaKey = (latex: string): string => latex.replace(/\s+/g, " ").trim();

/** The chatbot's system prompt is built on the server (CHAT_SYSTEM); the client sends only the context. */
export interface ChatRequest {
  context: string;
  messages: { role: "user" | "assistant"; content: string }[];
}

export const LIMITS = {
  conceptChars: 4000,
  paperChars: 300_000,
  sections: 80,
  chatMessages: 20,
  chatChars: 40_000,
  /** Edit chat: largest request (context + conversation, characters). */
  editChars: 60_000,
  editMessages: 40,
  /** Follow-ups: the previous explainer's outline, summary and source excerpt together (characters). */
  followUpChars: 90_000,
  /** Revisions: the previous version's written worked-example steps and section extras, kept for unchanged stages (characters). */
  keptChars: 60_000,
  /** Revisions: the previous version's widgets, kept as they were (characters of HTML, all widgets together). */
  widgetChars: 200_000,
  /** Section tools: the section text sent, and all earlier answers together (characters). */
  sectionChars: 8000,
  sectionPrevious: 12_000,
  /** "Go deeper" on a formula: its LaTeX (characters). */
  formulaLatex: 2000,
};
