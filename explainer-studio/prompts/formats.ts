// Structured-output formats and tool definitions the server sends to Claude.
// Like prompts.ts, this is source only: `npm run export:server` writes the
// built JSON into server/explainer/generated/, which the Flask server reads.
// Keep key order stable: tools and formats are part of the cached request.

import { zodOutputFormat } from "@anthropic-ai/sdk/helpers/zod";
import { z } from "zod";
import { Design } from "../shared/spec";

/** Code Lab mode, step 2: one section's interaction plan, written before the code (CODE_LAB_PLAN_SYSTEM, server/explainer/codelab.py). */
const CODE_LAB_PLAN = {
  type: "object", additionalProperties: false,
  required: ["build", "lesson", "readerDoes", "title", "afterBlock", "controls", "visuals", "expects", "contrast", "backup", "checks"],
  properties: {
    build: { type: "boolean" },
    lesson: { type: "string" },
    readerDoes: { type: "string" },
    title: { type: "string" },
    afterBlock: { type: "integer" },
    backup: { type: "string" },
    checks: {
      type: "array",
      items: {
        type: "object", additionalProperties: false, required: ["set", "readout", "expr"],
        properties: {
          set: {
            type: "array",
            items: { type: "object", additionalProperties: false, required: ["control", "value"], properties: { control: { type: "string" }, value: { type: "number" } } },
          },
          readout: { type: "string" },
          expr: { type: "string" },
        },
      },
    },
    controls: {
      type: "array",
      items: {
        type: "object", additionalProperties: false, required: ["id", "kind", "label", "min", "max", "step", "value"],
        properties: {
          id: { type: "string" }, kind: { type: "string", enum: ["slider", "button", "select"] }, label: { type: "string" },
          min: { type: "number" }, max: { type: "number" }, step: { type: "number" }, value: { type: "number" },
        },
      },
    },
    visuals: {
      type: "array",
      items: {
        type: "object", additionalProperties: false, required: ["id", "kind", "what"],
        properties: { id: { type: "string" }, kind: { type: "string", enum: ["curve", "bars", "scatter", "grid", "diagram", "animation", "readout"] }, what: { type: "string" } },
      },
    },
    expects: {
      type: "array",
      items: { type: "object", additionalProperties: false, required: ["control", "changes"], properties: { control: { type: "string" }, changes: { type: "array", items: { type: "string" } } } },
    },
    contrast: {
      type: "object", additionalProperties: false, required: ["items", "part"],
      properties: { items: { type: "array", items: { type: "string" } }, part: { type: "string" } },
    },
  },
};

/** Code Lab 4.0: the lesson's "as X changes, Y moves" statements, tested in code by moving X (scripts/check-widget.mjs). */
const CLAIMS = {
  type: "array",
  items: {
    type: "object", additionalProperties: false, required: ["move", "from", "to", "field", "at", "expect"],
    properties: {
      move: { type: "string" },
      from: { type: "number" },
      to: { type: "number" },
      field: { type: "string" },
      at: {
        type: "array",
        items: { type: "object", additionalProperties: false, required: ["control", "value"], properties: { control: { type: "string" }, value: { type: "number" } } },
      },
      expect: { type: "string", enum: ["rises", "falls", "same"] },
    },
  },
} as const;
/** Code Lab 4.0: what a computed field must always be (a probability, a share adding up to 1...), tested in code. */
const SANITY = {
  type: "array",
  items: {
    type: "object", additionalProperties: false, required: ["field", "kind", "by"],
    properties: {
      field: { type: "string" },
      kind: { type: "string", enum: ["probability", "percent", "nonnegative", "sums_to_one", "sums_to_100"] },
      by: { type: "string" },
    },
  },
} as const;

const StepsOut = z.object({ examples: z.array(z.object({ key: z.string(), steps: z.array(z.string()) })) });
const BriefTextOut = z.object({ items: z.array(z.object({ key: z.string(), md: z.string() })) });

/** json_schema output formats (pipeline: design, worked-example steps, briefs to text). */
export const FORMATS = {
  design: zodOutputFormat(Design).schema,
  steps: zodOutputFormat(StepsOut).schema,
  briefText: zodOutputFormat(BriefTextOut).schema,
  /** POST /api/route: does a follow-up revise the explainer above or ask for a new one? */
  route: {
    type: "object",
    properties: { kind: { type: "string", enum: ["revise", "new", "unclear"] } },
    required: ["kind"],
    additionalProperties: false,
  },
  /** POST /api/feedback (FEEDBACK_SYSTEM): the reader's feedback as a plan for this page (changes, parts to keep, or a question) and/or a saved preference. */
  feedback: {
    type: "object",
    properties: {
      changes: {
        type: "array",
        items: {
          type: "object",
          properties: {
            part: { type: "string", enum: ["widget", "text", "section", "new-section"] },
            stage: { type: "string" },
            after: { type: "string" },
            what: { type: "string" },
            quote: { type: "string" },
          },
          required: ["part", "stage", "after", "what", "quote"],
          additionalProperties: false,
        },
      },
      keep: { type: "array", items: { type: "string" } },
      pageWide: { type: "boolean" },
      question: {
        type: "object",
        properties: {
          text: { type: "string" },
          options: {
            type: "array",
            items: {
              type: "object",
              properties: {
                label: { type: "string" },
                part: { type: "string", enum: ["widget", "text", "section", "new-section", "page"] },
                stage: { type: "string" },
                after: { type: "string" },
                what: { type: "string" },
              },
              required: ["label", "part", "stage", "after", "what"],
              additionalProperties: false,
            },
          },
        },
        required: ["text", "options"],
        additionalProperties: false,
      },
      preference: { type: "string" },
      topic: { type: "string", enum: ["visuals", "controls", "level", "length", "tone", "other"] },
    },
    required: ["changes", "keep", "pageWide", "question", "preference", "topic"],
    additionalProperties: false,
  },
  /** Feedback step 1 (FEEDBACK_PICK_SYSTEM): which sections the feedback asks to change. */
  feedbackPick: {
    type: "object",
    properties: {
      sections: { type: "array", items: { type: "string" } },
      unsure: { type: "array", items: { type: "string" } },
      kind: { type: "string", enum: ["parts", "page", "new", "none"] },
      why: { type: "string" },
    },
    required: ["sections", "unsure", "kind", "why"],
    additionalProperties: false,
  },
  /** Follow-up targets (TARGET_SYSTEM): which parts of the page a revise request is about. Unused ids are "". */
  targets: {
    type: "object",
    properties: {
      targets: {
        type: "array",
        items: {
          type: "object",
          properties: {
            part: { type: "string", enum: ["widget", "text", "section", "new-section"] },
            stage: { type: "string" },
            after: { type: "string" },
            what: { type: "string" },
          },
          required: ["part", "stage", "after", "what"],
          additionalProperties: false,
        },
      },
      pageWide: { type: "boolean" },
    },
    required: ["targets", "pageWide"],
    additionalProperties: false,
  },
  /** Fact check: Haiku's verdict and fixes for flagged claims. */
  repair: {
    type: "object", additionalProperties: false, required: ["results"],
    properties: {
      results: {
        type: "array",
        items: {
          type: "object", additionalProperties: false, required: ["id", "verdict", "edits", "answer"],
          properties: {
            id: { type: "string" },
            verdict: { type: "string", enum: ["correct", "fix"] },
            edits: { type: "array", items: { type: "object", additionalProperties: false, required: ["find", "replace"], properties: { find: { type: "string" }, replace: { type: "string" } } } },
            answer: { type: "integer" },
          },
        },
      },
    },
  },
  /** Clarity check (Quality+, Freeform+): the ideas each stage relies on (CLARITY_TOPICS_SYSTEM). */
  clarityTopics: {
    type: "object", additionalProperties: false, required: ["stages"],
    properties: {
      stages: {
        type: "array",
        items: {
          type: "object", additionalProperties: false, required: ["index", "relies_on"],
          properties: { index: { type: "integer" }, relies_on: { type: "array", items: { type: "string" } } },
        },
      },
    },
  },
  /** Code Lab mode: Haiku's decision and widget code for one section (CODE_LAB_SYSTEM, server/explainer/codelab.py). */
  codeLab: {
    type: "object", additionalProperties: false,
    required: ["build", "why", "title", "afterBlock", "html"],
    properties: {
      build: { type: "boolean" },
      why: { type: "string" },
      title: { type: "string" },
      afterBlock: { type: "integer" },
      html: { type: "string" },
    },
  },
  /** Code Lab 7.0's look check: what a reader would see is wrong in the widget's screenshots (CODE_LAB_LOOK_SYSTEM). */
  codeLabLook: {
    type: "object", additionalProperties: false, required: ["problems"],
    properties: { problems: { type: "array", items: { type: "string" } } },
  },
  /** Code Lab mode, step 2: one section's interaction plan, written before the code (CODE_LAB_PLAN_SYSTEM, server/explainer/codelab.py). */
  codeLabPlan: CODE_LAB_PLAN,
  /** Code Lab 2.0: the same plan, carrying its whole widget as a Vega-Lite spec (CODE_LAB_CHART_NOTE). */
  codeLabPlanChart: {
    ...CODE_LAB_PLAN,
    required: [...CODE_LAB_PLAN.required, "vegaLite"],
    properties: { ...CODE_LAB_PLAN.properties, vegaLite: { type: "string" } },
  },
  /** Code Lab 3.0: only what a chart needs (CODE_LAB_3_PLAN_SYSTEM). The controls are read from the spec's params in
   * code (codelab.controls_from_spec), so the model doesn't write them twice. */
  codeLabPlanSlim: {
    type: "object", additionalProperties: false,
    required: ["build", "lesson", "title", "afterBlock", "backup", "checks", "claims", "sanity", "vegaLite"],
    properties: {
      build: CODE_LAB_PLAN.properties.build,
      lesson: CODE_LAB_PLAN.properties.lesson,
      title: CODE_LAB_PLAN.properties.title,
      afterBlock: CODE_LAB_PLAN.properties.afterBlock,
      backup: CODE_LAB_PLAN.properties.backup,
      checks: CODE_LAB_PLAN.properties.checks,
      claims: CLAIMS,
      sanity: SANITY,
      vegaLite: { type: "string" },
    },
  },
  /** Code Lab 3.0: the same chart-only plan, with the chart as a full Vega spec (CODE_LAB_VG_PLAN_SYSTEM). The
   * controls are read from the spec's bound signals in code. */
  codeLabPlanVg: {
    type: "object", additionalProperties: false,
    required: ["build", "lesson", "title", "afterBlock", "backup", "checks", "vega"],
    properties: {
      build: CODE_LAB_PLAN.properties.build,
      lesson: CODE_LAB_PLAN.properties.lesson,
      title: CODE_LAB_PLAN.properties.title,
      afterBlock: CODE_LAB_PLAN.properties.afterBlock,
      backup: CODE_LAB_PLAN.properties.backup,
      checks: CODE_LAB_PLAN.properties.checks,
      vega: { type: "string" },
    },
  },
  /** Code Lab 2.0: a Vega-Lite spec, as a JSON string: a corrected chart (CODE_LAB_VEGA_REPAIR) or the backup chart (CODE_LAB_VEGA_BACKUP). */
  codeLabVega: {
    type: "object", additionalProperties: false, required: ["spec"],
    properties: { spec: { type: "string" } },
  },
  /** Lab mode: one section's interaction plan, written before the scene is built (LAB_PLAN_SYSTEM, server/explainer/scenes.py). */
  labPlan: {
    type: "object", additionalProperties: false,
    required: ["build", "lesson", "pattern", "readerDoes", "variables", "buttons", "shows", "expects", "contrast", "replaces"],
    properties: {
      build: { type: "boolean" },
      lesson: { type: "string" },
      pattern: { type: "string", enum: ["sweep", "play", "step", "compare", "click"] },
      readerDoes: { type: "string" },
      variables: {
        type: "array",
        items: {
          type: "object", additionalProperties: false, required: ["name", "label", "min", "max", "step", "value", "role"],
          properties: {
            name: { type: "string" }, label: { type: "string" },
            min: { type: "number" }, max: { type: "number" }, step: { type: "number" }, value: { type: "number" },
            role: { type: "string", enum: ["reader", "rule", "page"] },
          },
        },
      },
      buttons: {
        type: "array",
        items: { type: "object", additionalProperties: false, required: ["id", "label", "does"], properties: { id: { type: "string" }, label: { type: "string" }, does: { type: "string" } } },
      },
      shows: {
        type: "array",
        items: {
          type: "object", additionalProperties: false, required: ["id", "kind", "what"],
          properties: { id: { type: "string" }, kind: { type: "string", enum: ["chart", "readout", "bars", "table", "text"] }, what: { type: "string" } },
        },
      },
      expects: {
        type: "array",
        items: { type: "object", additionalProperties: false, required: ["var", "changes"], properties: { var: { type: "string" }, changes: { type: "array", items: { type: "string" } } } },
      },
      contrast: {
        type: "object", additionalProperties: false, required: ["items", "part"],
        properties: { items: { type: "array", items: { type: "string" } }, part: { type: "string" } },
      },
      replaces: { type: "array", items: { type: "integer" } },
    },
  },
  /** Smart mode: the section list Jev judges before the page is written (SMART_SECTIONS_SYSTEM). */
  smartSections: {
    type: "object", additionalProperties: false, required: ["sections"],
    properties: {
      sections: {
        type: "array",
        items: {
          type: "object", additionalProperties: false, required: ["label", "blurb", "covers"],
          properties: { label: { type: "string" }, blurb: { type: "string" }, covers: { type: "string" } },
        },
      },
    },
  },
  /** Quality 2.0: topics a finished page still leaves out (DEEPEN_GAPS_SYSTEM). */
  deepenGaps: {
    type: "object", additionalProperties: false, required: ["gaps"],
    properties: {
      gaps: {
        type: "array",
        items: {
          type: "object", additionalProperties: false, required: ["topic", "why"],
          properties: { topic: { type: "string" }, why: { type: "string" } },
        },
      },
    },
  },
  /** "Add to each section": a deeper explanation and the same explanation in other words, per stage (SECTION_NOTES_SYSTEM). */
  /** "Go deeper" on a formula (FORMULA_DEEPER_SYSTEM; shared/spec.ts FormulaNote). */
  formulaDeeper: {
    type: "object", additionalProperties: false, required: ["idea", "terms", "origin", "shape", "example", "pitfall"],
    properties: {
      idea: { type: "string" },
      terms: {
        type: "array",
        items: {
          type: "object", additionalProperties: false, required: ["symbol", "name", "meaning", "role"],
          properties: { symbol: { type: "string" }, name: { type: "string" }, meaning: { type: "string" }, role: { type: "string" } },
        },
      },
      origin: { type: "array", items: { type: "string" } },
      shape: { type: "string" },
      example: { type: "array", items: { type: "string" } },
      pitfall: { type: "string" },
    },
  },
  sectionNotes: {
    type: "object", additionalProperties: false, required: ["stages"],
    properties: {
      stages: {
        type: "array",
        items: {
          type: "object", additionalProperties: false, required: ["index", "deeper", "another"],
          properties: { index: { type: "integer" }, deeper: { type: "string" }, another: { type: "string" } },
        },
      },
    },
  },
  /** Haiku's inserts and edits for the stages Jev flagged (CLARITY_SYSTEM). */
  clarify: {
    type: "object", additionalProperties: false, required: ["results"],
    properties: {
      results: {
        type: "array",
        items: {
          type: "object", additionalProperties: false, required: ["index", "inserts", "edits"],
          properties: {
            index: { type: "integer" },
            inserts: {
              type: "array",
              items: {
                type: "object", additionalProperties: false, required: ["before", "type", "variant", "title", "md"],
                properties: {
                  before: { type: "integer" },
                  type: { type: "string", enum: ["text", "callout"] },
                  variant: { type: "string", enum: ["info", "key", "analogy"] },
                  title: { type: "string" },
                  md: { type: "string" },
                },
              },
            },
            edits: { type: "array", items: { type: "object", additionalProperties: false, required: ["find", "replace"], properties: { find: { type: "string" }, replace: { type: "string" } } } },
          },
        },
      },
    },
  },
};

/** The planner's "compute" tool: it checks numbers with the page's own math engine. */
export const COMPUTE_TOOL = {
  name: "compute",
  description: "Evaluate formula-language expressions exactly as the explainer page will. For each expression give the variable values to use (for example the slider defaults you chose). Returns each value, or the error.",
  input_schema: {
    type: "object",
    properties: {
      expressions: {
        type: "array",
        items: {
          type: "object",
          properties: {
            expr: { type: "string" },
            vars: { type: "array", items: { type: "object", properties: { name: { type: "string" }, value: { type: "number" } }, required: ["name", "value"] } },
          },
          required: ["expr"],
        },
      },
    },
    required: ["expressions"],
  },
  // No eager_input_streaming: the input is tiny, and buffering lets the API
  // check it, so a model's JSON slip can't reach (and crash) the SDK.
};

const path = { type: "array", items: { anyOf: [{ type: "string" }, { type: "integer" }] }, description: "A path from the Values or Lists description, e.g. [\"series\", 0, \"label\"]" };

/** Edit chat tools. They run in the browser (app/src/shell/edit.ts); the server only relays Haiku's calls. */
export const EDIT_TOOLS = [
  {
    name: "set_values",
    description: "Change one or more values of the selected part (numbers, true/false, menu choices, formulas, and text only after the reader chose AI through edit_text). Text changes are shown to the reader for approval; other changes apply at once.",
    input_schema: {
      type: "object",
      properties: {
        changes: { type: "array", items: { type: "object", properties: { path, value: { anyOf: [{ type: "string" }, { type: "number" }, { type: "boolean" }] } }, required: ["path", "value"] } },
      },
      required: ["changes"],
    },
  },
  {
    name: "edit_text",
    description: "Use whenever the reader wants different wording (titles, labels, explanations, cards, quiz text). Give the text paths to change. Fixed code asks the reader whether to write it themselves (no AI) or have you rewrite it; never write new text before they choose.",
    input_schema: { type: "object", properties: { paths: { type: "array", items: path, description: "Text paths to change; empty means all the part's text" } }, required: ["paths"] },
  },
  {
    name: "list_edit",
    description: "Remove, duplicate, or move up/down one item of a list in the selected part (a card, a series, a quiz question, a bar...). The path ends with the item's index.",
    input_schema: { type: "object", properties: { path, op: { type: "string", enum: ["remove", "duplicate", "up", "down"] } }, required: ["path", "op"] },
  },
  {
    name: "change_type",
    description: "Turn the selected part into one of its listed other forms (same data, different graph or layout).",
    input_schema: { type: "object", properties: { option: { type: "integer", description: "Index from 'Other forms for this part'" } }, required: ["option"] },
  },
  {
    name: "show_graph_options",
    description: "Show the reader live previews of the part's other forms so they can click the one they like.",
    input_schema: { type: "object", properties: {} },
  },
  {
    name: "show_settings",
    description: "Show the reader a form with every setting of the part so they can change values themselves.",
    input_schema: { type: "object", properties: {} },
  },
  {
    name: "add_button",
    description: "Add a button to this step that sets page controls to chosen values when clicked (e.g. 'Paper's setting', 'Very creative').",
    input_schema: {
      type: "object",
      properties: {
        name: { type: "string" },
        settings: { type: "array", items: { type: "object", properties: { var: { type: "string" }, value: { type: "number" } }, required: ["var", "value"] } },
      },
      required: ["name", "settings"],
    },
  },
  {
    name: "arrange",
    description: "Move the whole selected part up or down within its step, duplicate it, or remove it.",
    input_schema: { type: "object", properties: { action: { type: "string", enum: ["up", "down", "duplicate", "remove"] } }, required: ["action"] },
  },
  {
    name: "undo",
    description: "Undo the last change made to the page.",
    input_schema: { type: "object", properties: {} },
  },
  {
    name: "ask_user",
    description: "Ask the reader one short clarifying question with 2-4 clickable answers. Their answer comes back as this tool's result.",
    input_schema: {
      type: "object",
      properties: { question: { type: "string" }, options: { type: "array", items: { type: "string" } } },
      required: ["question", "options"],
    },
  },
];
