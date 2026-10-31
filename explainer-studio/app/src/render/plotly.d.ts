// The minified Plotly bundle ships no types; this is the part chartlib.ts uses.
declare module "plotly.js-cartesian-dist-min" {
  const Plotly: {
    react(el: HTMLElement, data: object[], layout: object, config?: object): Promise<unknown>;
    purge(el: HTMLElement): void;
  };
  export default Plotly;
}
