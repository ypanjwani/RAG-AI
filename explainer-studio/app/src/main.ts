import "katex/dist/katex.min.css";
import "./styles/app.css";
import { App } from "./shell/app";
import { injectExplainerStyles } from "./styles";

injectExplainerStyles();
new App().start(document.getElementById("root")!);
