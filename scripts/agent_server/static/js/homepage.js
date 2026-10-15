import { initAgentGraph } from "./agent-graph.js";

const graphRoot = document.querySelector("#agent-graph-root");
const graphController = graphRoot ? initAgentGraph(graphRoot) : null;

function syncGraph() {
  if (!graphController) {
    return;
  }
  graphController.setActive(!document.hidden);
}

document.addEventListener("visibilitychange", syncGraph);
syncGraph();
