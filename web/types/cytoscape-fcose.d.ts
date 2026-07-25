/**
 * `cytoscape-fcose` ships no type declarations, so this is the minimum that
 * makes `cytoscape.use(fcose)` type-check.
 *
 * The layout's own options are passed through `cy.layout({ name: "fcose", … })`,
 * which `@types/cytoscape` already types loosely — nothing more is needed here.
 */
declare module "cytoscape-fcose" {
  import type cytoscape from "cytoscape";

  const fcose: (cy: typeof cytoscape) => void;
  export default fcose;
}
