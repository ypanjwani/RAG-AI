// Fixed design menus. The AI (Haiku, or Jev in custom mode) may only pick names from these lists; the
// actual styling for each name lives in app/src/styles/. Descriptions are shown
// to the model so it can choose well.

export const THEMES = {
  ocean: "Calm blue/indigo. Safe default; suits retrieval, search, NLP, general ML.",
  sunset: "Warm orange/rose. Suits generative models, diffusion, creativity, images.",
  forest: "Green/teal. Suits training, optimization, RL, evolution, efficiency (e.g. LoRA).",
  graphite: "Neutral slate with violet accent. Academic, theory-heavy or math-heavy papers.",
  berry: "Fuchsia/plum. Playful and vivid; suits multimodal, vision-language, creative agents.",
  sand: "Warm neutral with a terracotta accent. Calm and editorial; suits ethics, history, evaluation.",
} as const;

export const FONT_PAIRINGS = {
  modern: "Inter + JetBrains Mono. Clean product feel; default for most topics.",
  editorial: "Fraunces serif headings + Inter body. Essay-like, good for theory and history.",
  technical: "Space Grotesk + IBM Plex Mono. Engineering/systems feel, architecture-heavy papers.",
  friendly: "Nunito + Fira Code. Rounded and approachable, best for absolute beginners.",
} as const;

export const HEADING_STYLES = {
  "gradient-banner": "Full-width gradient header per step. Bold, energetic.",
  "underline-accent": "Large heading with an accent underline. Calm, article-like.",
  "numbered-badge": "Circular step number badge beside the heading. Structured, tutorial-like.",
} as const;

export const LAYOUTS = {
  article: "DEFAULT. One scrolling page with a sticky stage nav; stages build on each other but the reader can jump around. Right for almost every paper and concept.",
  pipeline: "Overview flowchart + one locked page per step with Prev/Next. Only when the topic itself is a strict data-processing sequence the reader should walk in order.",
} as const;

export const LEVELS = {
  beginner: "No prior ML knowledge; everyday analogies, minimal math.",
  student: "Knows basic ML/linear algebra; some formulas with intuition.",
  practitioner: "Works with ML; precise terms, real numbers, trade-offs.",
} as const;

// Stage icons: the AI picks a name; code maps it to the emoji. (Emoji make
// poor enum values: models may drop or add the U+FE0F variation selector.)
export const ICONS = {
  document: "📄", scissors: "✂️", numbers: "🔢", abacus: "🧮", ruler: "📐", compass: "🧭", search: "🔍", target: "🎯",
  box: "📦", folders: "🗂️", brain: "🧠", robot: "🤖", chat: "💬", speaking: "🗣️", writing: "✍️", image: "🖼️",
  palette: "🎨", fog: "🌫️", audio: "🔊", video: "🎬", gear: "⚙️", wrench: "🔧", experiment: "🧪", bar_chart: "📊",
  trend_up: "📈", trend_down: "📉", training: "🏋️", dice: "🎲", thermometer: "🌡️", loop: "🔁", link: "🔗", network: "🕸️",
  puzzle: "🧩", ladder: "🪜", rocket: "🚀", lightning: "⚡", shield: "🛡️", balance: "⚖️", trophy: "🏆", check: "✅",
} as const;
export type IconName = keyof typeof ICONS;

export type ThemeName = keyof typeof THEMES;
export type FontPairing = keyof typeof FONT_PAIRINGS;
export type HeadingStyle = keyof typeof HEADING_STYLES;
export type LayoutName = keyof typeof LAYOUTS;
export type Level = keyof typeof LEVELS;
/** Light/dark preference saved with an explainer ("auto" follows the OS). */
export const COLOR_MODES = ["auto", "light", "dark"] as const;
export type ColorMode = (typeof COLOR_MODES)[number];

// The composer's "how hands-on" toggle (server/explainer/pipeline.py turns it into a planner hint).
// Descriptions are the buttons' tooltips.
export const INTERACTIVITY = {
  reading: "Mostly reading: clear explanation with a few visuals where they help most. Cheapest",
  balanced: "Balanced: the usual mix of explanation and interactive parts",
  "hands-on": "Hands-on: as many sliders, charts and experiments as the topic allows. Costs more",
} as const;

export type Interactivity = keyof typeof INTERACTIVITY;

// "What do you want to add to each section?" (Quality only): asked in the conversation after the reader sends a
// topic, before anything is planned. Everything chosen opens from a button on each section in a side panel, the
// way worked examples do; nothing is added to the section's own text. examples keeps the worked examples (without
// it they are removed); depth and rephrase are written with the page (server/explainer/notes.py); search runs
// only when its button is clicked (POST /api/section). auto, and None (an empty list), can't be combined with the
// others. The label is the text before the colon; the rest is its tooltip.
export const EXTRAS = {
  depth: "Deeper explanation (costs more): a longer explanation of each section, with the why, the intuition and the edge cases",
  examples: "Worked examples: step-by-step worked examples for the sections",
  rephrase: "Same explanation in different wording: each section explained again another way",
  search: "Search the web with AI: searches the web about a section when you click its button, with sources",
  auto: "Let this mode decide: worked examples where they help, plus the web search button",
} as const;

export type Extra = keyof typeof EXTRAS;
/** The on-demand section tools (buttons that call the server when clicked). */
export type SectionTool = "search";

// ---------- Page brief (Quality+/Lite+): decisions Jev makes before Haiku plans ----------
// server/explainer/brief.py asks Jev one choice question per menu; descriptions are what Jev judges.
// The first key of each menu is its default (used when the reader or Jev doesn't decide).

/** The composer's Level toggle. Labels differ from the LEVELS keys, which stay for old specs. */
export const LEVEL_CHOICES = {
  auto: "Auto: Jev picks the level from the topic",
  beginner: "Beginner: no prior ML knowledge",
  student: "Intermediate: knows basic ML and linear algebra",
  practitioner: "Advanced: works with ML; precise terms and real numbers",
} as const;
export type LevelChoice = keyof typeof LEVEL_CHOICES;

export const MATH_DEPTH = {
  intuition: "Key formulas, each with an intuitive reading of every symbol.",
  none: "Almost no formulas; ideas carried by words, pictures and numbers.",
  derivations: "Full formulas and the steps between them; for readers who want the maths.",
} as const;

export const APPROACH = {
  intuition: "Intuition first: the core idea in plain words, then the details.",
  example: "Example first: open each idea with a concrete case, then generalise.",
  problem: "Problem first: what goes wrong without the idea, then how it fixes it.",
  story: "Story: how the idea evolved, each step fixing the previous one's limits.",
  compare: "Compare: the idea set against the alternatives it competes with.",
} as const;

export const GOAL = {
  understand: "Understand how and why it works.",
  implement: "Implement or use it: concrete steps, settings and pitfalls.",
  judge: "Judge the claims: evidence, results, limitations.",
  revise: "Revise for an exam: definitions, key facts and practice questions.",
} as const;

export const ANALOGIES = {
  few: "A few everyday analogies where an idea is abstract.",
  none: "No analogies; the reader prefers the real mechanism.",
  many: "Many everyday analogies; the reader is new to the field.",
} as const;

export const TONE = {
  neutral: "Clear and neutral, like a good textbook.",
  friendly: "Warm and encouraging, like a patient tutor.",
  academic: "Precise and formal, like a survey paper.",
} as const;

export const TOPIC_AREA = {
  general: "General machine learning.",
  nlp: "Language models, text and tokens.",
  vision: "Images, video and vision models.",
  generative: "Generative models: diffusion, GANs, sampling.",
  rl: "Reinforcement learning, agents and rewards.",
  optimization: "Training, optimisers, efficiency and fine-tuning.",
  architecture: "Model architectures and their building blocks.",
  retrieval: "Search, retrieval and embeddings.",
  evaluation: "Evaluation, safety, ethics and societal impact.",
  theory: "Theory: learning theory, statistics, proofs.",
} as const;

export const TEXT_SIZES = {
  normal: "Normal text size.",
  small: "Smaller text for dense, technical pages.",
  large: "Larger text for beginners and easy reading.",
} as const;

export const DENSITY = {
  comfortable: "Comfortable spacing between blocks.",
  compact: "Compact spacing for long, dense pages.",
} as const;

export const EMPHASIS = {
  both: "Highlight key sentences and colour key terms.",
  bold: "Bold only; no highlights or coloured terms.",
  highlight: "Highlight key sentences only.",
  colour: "Colour key terms only.",
} as const;

export const EMPHASIS_AMOUNT = {
  normal: "Emphasis on the most important points of each stage.",
  sparing: "Very little emphasis: only the single most important point per stage.",
  heavy: "Frequent emphasis for skimming readers.",
} as const;

export type TextSize = keyof typeof TEXT_SIZES;
export type Density = keyof typeof DENSITY;
export type Emphasis = keyof typeof EMPHASIS;

export const DEFAULT_DESIGN = {
  theme: "ocean" as ThemeName,
  fontPairing: "modern" as FontPairing,
  headingStyle: "gradient-banner" as HeadingStyle,
};

export const keys = <T extends object>(o: T) => Object.keys(o) as [keyof T & string, ...(keyof T & string)[]];

/** Menu text for prompts: "- name: description" lines. */
export function menuText(menu: Record<string, string>): string {
  return Object.entries(menu).map(([k, v]) => `- ${k}: ${v}`).join("\n");
}
