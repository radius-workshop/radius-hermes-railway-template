import * as THREE from "three";
import { OrbitControls } from "three/addons/controls/OrbitControls.js";

const LINE_COLOR = 0xd6b67a;
const DEFAULT_CAMERA_POSITION = new THREE.Vector3(0, 1.85, 16.6);
const DEFAULT_CAMERA_TARGET = new THREE.Vector3(0, 0.35, 0);
const ORBIT_BASE_SPEED = 0.055;
const BASE_NODE_SCALE = {
  agent: 1.28,
  surface: 0.42,
  capability: 0.35,
  skill: 0.31,
  plugin: 0.29,
  tool: 0.28,
  channel: 0.32,
};
const NODE_VISUAL_SCHEMA = {
  agent: {
    color: 0xf3c96d,
    emissive: 0xdd7857,
    emissiveIntensity: 0.35,
    metalness: 0.24,
    roughness: 0.24,
  },
  surface: {
    color: 0xeb6359,
    emissive: 0x5d211a,
    emissiveIntensity: 0.22,
    metalness: 0.12,
    roughness: 0.36,
  },
  interface: {
    color: 0x7fc7ff,
    emissive: 0x143a58,
    emissiveIntensity: 0.2,
    metalness: 0.14,
    roughness: 0.34,
  },
  capability: {
    color: 0xa8d86f,
    emissive: 0x2c4b17,
    emissiveIntensity: 0.21,
    metalness: 0.1,
    roughness: 0.38,
  },
  skill: {
    color: 0xffb86f,
    emissive: 0x563015,
    emissiveIntensity: 0.19,
    metalness: 0.1,
    roughness: 0.4,
  },
  tool: {
    color: 0x8f8cff,
    emissive: 0x25215e,
    emissiveIntensity: 0.18,
    metalness: 0.13,
    roughness: 0.37,
  },
  channel: {
    color: 0x63d8c6,
    emissive: 0x164d46,
    emissiveIntensity: 0.2,
    metalness: 0.12,
    roughness: 0.39,
  },
  plugin: {
    color: 0x596070,
    emissive: 0x171a22,
    emissiveIntensity: 0.1,
    metalness: 0.08,
    roughness: 0.48,
  },
  internal: {
    color: 0x302834,
    emissive: 0x17121d,
    emissiveIntensity: 0.14,
    metalness: 0.1,
    roughness: 0.42,
  },
};
const STRUCTURED_GROUP_ORDER = [
  "external:surfaces",
  "external:interfaces",
  "internal:capabilities",
  "internal:skills",
  "internal:tools",
  "internal:channels",
  "internal:plugins",
];
const STRUCTURED_GROUP_LAYOUT = {
  "external:surfaces": {
    radius: 6.8,
    angle: Math.PI * 0.74,
    y: 2.95,
    depthScale: 0.72,
    columns: 2,
    columnGap: 1.5,
    rowGap: 1.0,
    depthGap: 0.42,
  },
  "external:interfaces": {
    radius: 6.6,
    angle: Math.PI * 0.22,
    y: 2.15,
    depthScale: 0.72,
    columns: 2,
    columnGap: 1.45,
    rowGap: 0.98,
    depthGap: 0.4,
  },
  "internal:capabilities": {
    radius: 4.0,
    angle: -Math.PI * 0.5,
    y: -0.15,
    depthScale: 0.76,
    columns: 2,
    columnGap: 1.36,
    rowGap: 0.95,
    depthGap: 0.32,
  },
  "internal:skills": {
    radius: 6.1,
    angle: -Math.PI * 0.78,
    y: -2.45,
    depthScale: 0.86,
    columns: 3,
    columnGap: 1.34,
    rowGap: 0.96,
    depthGap: 0.44,
  },
  "internal:plugins": {
    radius: 5.45,
    angle: -Math.PI * 0.18,
    y: -2.42,
    depthScale: 0.84,
    columns: 2,
    columnGap: 1.35,
    rowGap: 0.95,
    depthGap: 0.4,
  },
  "internal:tools": {
    radius: 7.1,
    angle: -Math.PI * 0.03,
    y: -0.95,
    depthScale: 0.9,
    columns: 2,
    columnGap: 1.28,
    rowGap: 0.92,
    depthGap: 0.48,
  },
  "internal:channels": {
    radius: 6.95,
    angle: Math.PI * 0.92,
    y: -0.95,
    depthScale: 0.78,
    columns: 2,
    columnGap: 1.25,
    rowGap: 0.9,
    depthGap: 0.38,
  },
};

function resizeRendererToDisplaySize(renderer, maxPixelCount = 2560 * 1440) {
  // Clamp render resolution to keep the canvas responsive on large displays.
  const canvas = renderer.domElement;
  const pixelRatio = window.devicePixelRatio || 1;
  let width = Math.floor(canvas.clientWidth * pixelRatio);
  let height = Math.floor(canvas.clientHeight * pixelRatio);
  const pixelCount = width * height;
  if (pixelCount > maxPixelCount) {
    const scale = Math.sqrt(maxPixelCount / pixelCount);
    width = Math.floor(width * scale);
    height = Math.floor(height * scale);
  }
  const needResize = canvas.width !== width || canvas.height !== height;
  if (needResize) {
    renderer.setSize(width, height, false);
  }
  return needResize;
}

function normalizeText(value) {
  return String(value || "").trim().toLowerCase();
}

function getNodeVisualType(node) {
  if (node.kind === "agent") {
    return "agent";
  }
  if (node.external) {
    return node.category === "interfaces" ? "interface" : "surface";
  }
  if (node.kind === "skill" || node.category === "skills") {
    return "skill";
  }
  if (node.kind === "tool" || node.category === "tools") {
    return "tool";
  }
  if (node.kind === "capability" || node.category === "capabilities") {
    return "capability";
  }
  if (node.kind === "channel" || node.category === "channels") {
    return "channel";
  }
  if (node.kind === "plugin" || node.category === "plugins") {
    return "plugin";
  }
  return "internal";
}

function getNodeVisualSchema(node) {
  return (
    NODE_VISUAL_SCHEMA[getNodeVisualType(node)] || NODE_VISUAL_SCHEMA.internal
  );
}

function getNodeTypeLabel(node) {
  switch (getNodeVisualType(node)) {
    case "agent":
      return "Agent";
    case "surface":
      return "External surface";
    case "interface":
      return "External interface";
    case "capability":
      return "Capability";
    case "skill":
      return "Skill";
    case "tool":
      return "Tool";
    case "channel":
      return "Internal channel";
    case "plugin":
      return "Implementation support";
    default:
      return node.kind || "Node";
  }
}

function stableHash(value) {
  return Array.from(String(value || "")).reduce(
    (total, character) => (total * 31 + character.charCodeAt(0)) % 997,
    7,
  );
}

function buildOrbitMeta(node, position) {
  if (node.id === "agent") {
    return null;
  }
  const radius = Math.hypot(position.x, position.z);
  if (radius < 0.001) {
    return null;
  }
  const seed = stableHash(node.id);
  const direction = seed % 2 === 0 ? 1 : -1;
  const speedByKind = {
    surface: 0.84,
    capability: 0.58,
    skill: 0.72,
    tool: 1.08,
    channel: 0.48,
  };
  return {
    angle: Math.atan2(position.z, position.x),
    radius,
    speed:
      ORBIT_BASE_SPEED *
      direction *
      (speedByKind[node.kind] || 0.66) *
      (0.82 + (seed % 7) * 0.055),
    y: position.y,
    yAmplitude: 0.035 + (seed % 5) * 0.008,
    yPhase: seed * 0.017,
  };
}

function getNodeScale(node) {
  return BASE_NODE_SCALE[node.kind] || 0.28;
}

function getNodeColor(node) {
  return getNodeVisualSchema(node).color;
}

function getNodeEmissive(node) {
  return getNodeVisualSchema(node).emissive;
}

function normalizeKindFilterValue(value) {
  return value === "plugin" ? "capability" : value || "all";
}

function matchesKindFilter(node, kindFilter) {
  if (!kindFilter || kindFilter === "all") {
    return true;
  }
  return node.kind === kindFilter;
}

function updateGraphControlLabels(searchInput, kindFilter) {
  if (searchInput?.placeholder) {
    searchInput.placeholder =
      "Search capabilities, skills, tools, channels, surfaces or interfaces";
  }

  const capabilityOption = kindFilter?.querySelector(
    'option[value="capability"]',
  );
  if (capabilityOption) {
    capabilityOption.textContent = "Capabilities";
  }

  const pluginOption = kindFilter?.querySelector('option[value="plugin"]');
  if (!pluginOption) {
    return;
  }
  if (capabilityOption) {
    pluginOption.remove();
    return;
  }
  pluginOption.value = "capability";
  pluginOption.textContent = "Capabilities";
}

function getFallbackGroupLayout(index, total) {
  const span = Math.PI * 0.55;
  const angle =
    -Math.PI * 0.84 + ((index + 0.5) / Math.max(total, 1)) * span;
  return {
    radius: 6.55,
    angle,
    y: -2.8,
    depthScale: 0.84,
    columns: 3,
    columnGap: 1.28,
    rowGap: 0.94,
    depthGap: 0.42,
  };
}

function buildLayout(payload) {
  const positions = new Map();
  positions.set("agent", new THREE.Vector3(0, 0.45, 0));

  const groups = new Map();
  payload.nodes.forEach((node) => {
    if (node.id === "agent") {
      return;
    }
    const groupKey = `${node.external ? "external" : "internal"}:${node.category}`;
    const bucket = groups.get(groupKey) || [];
    bucket.push(node);
    groups.set(groupKey, bucket);
  });

  const groupKeys = [
    ...STRUCTURED_GROUP_ORDER.filter((key) => groups.has(key)),
    ...Array.from(groups.keys())
      .filter((key) => !STRUCTURED_GROUP_ORDER.includes(key))
      .sort(),
  ];
  const unknownKeys = groupKeys.filter((key) => !STRUCTURED_GROUP_LAYOUT[key]);

  groupKeys.forEach((groupKey) => {
    const nodes = (groups.get(groupKey) || []).sort((left, right) =>
      String(left.label || "").localeCompare(String(right.label || "")),
    );
    if (!nodes.length) {
      return;
    }

    const config =
      STRUCTURED_GROUP_LAYOUT[groupKey] ||
      getFallbackGroupLayout(
        unknownKeys.indexOf(groupKey),
        Math.max(unknownKeys.length, 1),
      );
    const center = new THREE.Vector3(
      Math.cos(config.angle) * config.radius,
      config.y,
      Math.sin(config.angle) * config.radius * config.depthScale,
    );
    const radialDir = new THREE.Vector3(center.x, 0, center.z);
    if (radialDir.lengthSq() < 0.0001) {
      radialDir.set(0, 0, 1);
    } else {
      radialDir.normalize();
    }
    const tangentDir = new THREE.Vector3(-radialDir.z, 0, radialDir.x).normalize();
    const verticalDir = new THREE.Vector3(0, 1, 0);
    const columns = Math.min(
      config.columns || 3,
      Math.max(1, Math.ceil(Math.sqrt(nodes.length))),
    );
    const rows = Math.ceil(nodes.length / columns);

    nodes.forEach((node, index) => {
      const row = Math.floor(index / columns);
      const rowCount = Math.min(columns, nodes.length - row * columns);
      const column = index - row * columns;
      const xOffset = (column - (rowCount - 1) / 2) * (config.columnGap || 1.3);
      const yOffset = ((rows - 1) / 2 - row) * (config.rowGap || 0.95);
      const zOffset = (((column + row) % 2) - 0.5) * (config.depthGap || 0.4);
      const position = center
        .clone()
        .add(tangentDir.clone().multiplyScalar(xOffset))
        .add(verticalDir.clone().multiplyScalar(yOffset))
        .add(radialDir.clone().multiplyScalar(zOffset));
      positions.set(node.id, position);
    });
  });

  return positions;
}

function buildSelectionView(root, node, edgeCount) {
  root.replaceChildren();
  root.className = "graph-selection-panel";

  const kicker = document.createElement("div");
  kicker.className = "graph-selection-kicker";
  kicker.textContent = getNodeTypeLabel(node);
  root.appendChild(kicker);

  const head = document.createElement("div");
  head.className = "graph-selection-head";

  const title = document.createElement("h4");
  title.textContent = node.label || "Unknown node";
  head.appendChild(title);

  const score = document.createElement("span");
  score.className = "graph-selection-score";
  score.textContent = `${edgeCount} ${edgeCount === 1 ? "link" : "links"}`;
  head.appendChild(score);

  root.appendChild(head);

  const rule = document.createElement("p");
  rule.className = "graph-selection-rule";
  rule.textContent = node.detail || "No description available.";
  root.appendChild(rule);

  const meta = document.createElement("div");
  meta.className = "graph-selection-meta";
  [
    node.external ? "External" : "Internal",
    getNodeTypeLabel(node),
    node.status || `${edgeCount} links`,
  ]
    .filter(Boolean)
    .forEach((label) => {
      const pill = document.createElement("span");
      pill.className = "graph-selection-pill";
      pill.textContent = label;
      meta.appendChild(pill);
    });
  root.appendChild(meta);

  if (node.tags?.length) {
    const tags = document.createElement("div");
    tags.className = "graph-selection-meta";
    node.tags.forEach((tag) => {
      const pill = document.createElement("span");
      pill.className = "graph-selection-pill";
      pill.textContent = tag;
      tags.appendChild(pill);
    });
    root.appendChild(tags);
  }

  if (node.href) {
    const link = document.createElement("a");
    link.className = "graph-selection-link";
    link.href = node.href;
    link.target = "_blank";
    link.rel = "noopener";
    link.textContent = "Open endpoint";
    root.appendChild(link);
  }
}

function buildContextSet(neighborMap, ids) {
  const context = new Set(["agent"]);
  ids.forEach((id) => {
    context.add(id);
    neighborMap.get(id)?.forEach((neighborId) => context.add(neighborId));
  });
  return context;
}

function computeFilterState(payload, graphObjects, searchQuery, kindFilter) {
  const normalizedQuery = normalizeText(searchQuery);
  const hasQuery = normalizedQuery.length > 0;
  const hasKind = kindFilter && kindFilter !== "all";
  const matchedIds = new Set();

  payload.nodes.forEach((node) => {
    if (node.id === "agent") {
      return;
    }
    if (hasKind && !matchesKindFilter(node, kindFilter)) {
      return;
    }
    if (
      hasQuery &&
      !graphObjects.nodeSearchText.get(node.id)?.includes(normalizedQuery)
    ) {
      return;
    }
    matchedIds.add(node.id);
  });

  const hasFiltering = hasQuery || hasKind;
  return {
    hasFiltering,
    matchedIds,
    contextIds: hasFiltering
      ? buildContextSet(graphObjects.neighborMap, matchedIds)
      : null,
  };
}

function createGraphObjects(payload, scene, labelLayer) {
  const rig = new THREE.Group();
  const nodeGroup = new THREE.Group();
  const positions = buildLayout(payload);
  scene.add(rig);
  rig.add(nodeGroup);

  const nodeMeshes = [];
  const nodeMeshMap = new Map();
  const nodeMap = new Map(payload.nodes.map((node) => [node.id, node]));
  const nodeSearchText = new Map();
  const labels = new Map();
  const labelBindings = [];
  const adjacencyCounts = new Map();
  const neighborMap = new Map(payload.nodes.map((node) => [node.id, new Set()]));
  const edgeMeshes = [];
  const geometry = new THREE.SphereGeometry(1, 28, 20);

  payload.nodes.forEach((node) => {
    nodeSearchText.set(
      node.id,
      normalizeText(
        [
          node.label,
          node.kind,
          node.category,
          node.detail,
          ...(node.tags || []),
        ].join(" "),
      ),
    );
  });

  payload.edges.forEach((edge) => {
    adjacencyCounts.set(edge.source, (adjacencyCounts.get(edge.source) || 0) + 1);
    adjacencyCounts.set(edge.target, (adjacencyCounts.get(edge.target) || 0) + 1);
    neighborMap.get(edge.source)?.add(edge.target);
    neighborMap.get(edge.target)?.add(edge.source);
  });

  payload.nodes.forEach((node) => {
    const visualSchema = getNodeVisualSchema(node);
    const visualType = getNodeVisualType(node);
    const material = new THREE.MeshStandardMaterial({
      color: getNodeColor(node),
      roughness: visualSchema.roughness,
      metalness: visualSchema.metalness,
      emissive: new THREE.Color(getNodeEmissive(node)),
      emissiveIntensity: visualSchema.emissiveIntensity,
      transparent: true,
      opacity: 1,
    });
    const mesh = new THREE.Mesh(geometry, material);
    mesh.position.copy(positions.get(node.id) || new THREE.Vector3());
    mesh.scale.setScalar(getNodeScale(node));
    mesh.userData.nodeId = node.id;
    mesh.userData.baseScale = getNodeScale(node);
    mesh.userData.baseEmissiveIntensity = visualSchema.emissiveIntensity;
    mesh.userData.orbit = buildOrbitMeta(node, mesh.position);
    mesh.userData.currentOpacity = 1;
    nodeGroup.add(mesh);
    nodeMeshes.push(mesh);
    nodeMeshMap.set(node.id, mesh);

    const label = document.createElement("div");
    label.className = "graph-label";
    label.dataset.nodeId = node.id;
    label.dataset.kind = visualType;
    label.dataset.variant = node.external ? "external" : "internal";
    label.textContent = node.label || node.id;
    labelLayer.appendChild(label);
    labels.set(node.id, label);
    labelBindings.push({ label, mesh, node });
  });

  payload.edges.forEach((edge) => {
    const source = positions.get(edge.source);
    const target = positions.get(edge.target);
    if (!source || !target) {
      return;
    }
    const edgeGeometry = new THREE.BufferGeometry().setFromPoints([
      source.clone(),
      target.clone(),
    ]);
    const edgeMaterial = new THREE.LineBasicMaterial({
      color: LINE_COLOR,
      transparent: true,
      opacity: 0.42,
    });
    const line = new THREE.Line(edgeGeometry, edgeMaterial);
    line.userData.source = edge.source;
    line.userData.target = edge.target;
    nodeGroup.add(line);
    edgeMeshes.push({
      geometry: edgeGeometry,
      line,
      material: edgeMaterial,
      source: edge.source,
      target: edge.target,
    });
  });

  return {
    adjacencyCounts,
    edgeMeshes,
    geometry,
    labelBindings,
    labels,
    neighborMap,
    nodeMap,
    nodeMeshMap,
    nodeMeshes,
    nodeSearchText,
    rig,
  };
}

function getNodeOpacity(nodeId, viewState) {
  const {
    contextIds,
    focusContext,
    focusedNodeId,
    hasFiltering,
    hoveredNodeId,
    matchedIds,
    selectedNodeId,
  } = viewState;
  let opacity = 1;

  if (hasFiltering) {
    if (matchedIds.has(nodeId)) {
      opacity = 1;
    } else if (contextIds?.has(nodeId)) {
      opacity = nodeId === "agent" ? 0.58 : 0.26;
    } else {
      opacity = 0.06;
    }
  }

  if (focusedNodeId && focusContext) {
    let focusOpacity = 1;
    if (nodeId === focusedNodeId) {
      focusOpacity = 1;
    } else if (focusContext.has(nodeId)) {
      focusOpacity = nodeId === "agent" ? 0.62 : 0.34;
    } else {
      focusOpacity = 0.08;
    }
    opacity = hasFiltering ? Math.max(opacity, focusOpacity) : focusOpacity;
  }

  if (nodeId === selectedNodeId || nodeId === hoveredNodeId) {
    opacity = 1;
  }

  return opacity;
}

function getEdgeOpacity(edge, viewState) {
  const {
    contextIds,
    focusContext,
    focusedNodeId,
    hasFiltering,
    matchedIds,
  } = viewState;
  let opacity = 0.42;

  if (hasFiltering) {
    const touchesMatch =
      matchedIds.has(edge.source) || matchedIds.has(edge.target);
    const inContext =
      contextIds?.has(edge.source) && contextIds?.has(edge.target);
    if (touchesMatch) {
      opacity = 0.5;
    } else if (inContext) {
      opacity = 0.16;
    } else {
      opacity = 0.03;
    }
  }

  if (focusedNodeId && focusContext) {
    const touchesFocused =
      edge.source === focusedNodeId || edge.target === focusedNodeId;
    const inFocus =
      focusContext.has(edge.source) && focusContext.has(edge.target);
    const focusOpacity = touchesFocused ? 0.64 : inFocus ? 0.18 : 0.03;
    opacity = hasFiltering ? Math.max(opacity, focusOpacity) : focusOpacity;
  }

  return opacity;
}

function getLabelPriority(node, viewState, adjacencyCounts) {
  let priority = 0;
  if (node.id === viewState.selectedNodeId) {
    priority += 1000;
  }
  if (node.id === viewState.hoveredNodeId) {
    priority += 900;
  }
  if (node.id === "agent") {
    priority += 700;
  }
  if (viewState.matchedIds.has(node.id)) {
    priority += 560;
  }
  if (viewState.focusContext?.has(node.id)) {
    priority += 300;
  }
  if (node.external) {
    priority += 180;
  }
  if (node.kind === "capability") {
    priority += 130;
  }
  if (node.kind === "skill") {
    priority += 78;
  }
  if (node.kind === "tool" || node.kind === "channel") {
    priority += 52;
  }
  if (node.kind === "plugin") {
    priority += 10;
  }
  priority += Math.min(50, (adjacencyCounts.get(node.id) || 0) * 6);
  return priority;
}

function updateLabels(graphObjects, camera, canvas, viewState) {
  if (!graphObjects) {
    return;
  }

  const projected = new THREE.Vector3();
  const worldPosition = new THREE.Vector3();
  const candidates = [];
  const budget = viewState.hasFiltering
    ? Math.min(18, Math.max(8, viewState.matchedIds.size + 6))
    : viewState.focusedNodeId
      ? 10
      : 8;

  graphObjects.labelBindings.forEach(({ label, mesh, node }) => {
    const opacity = mesh.userData.currentOpacity ?? 1;
    mesh.getWorldPosition(worldPosition);
    projected.copy(worldPosition).project(camera);
    const visible =
      projected.z > -1 &&
      projected.z < 1 &&
      projected.x > -1.35 &&
      projected.x < 1.35 &&
      projected.y > -1.35 &&
      projected.y < 1.35;
    if (!visible || opacity < 0.14) {
      label.style.display = "none";
      return;
    }
    candidates.push({
      id: node.id,
      label,
      opacity,
      priority: getLabelPriority(node, viewState, graphObjects.adjacencyCounts),
      x: (projected.x * 0.5 + 0.5) * canvas.clientWidth,
      y: (projected.y * -0.5 + 0.5) * canvas.clientHeight,
    });
  });

  const guaranteedIds = new Set(["agent"]);
  if (viewState.selectedNodeId) {
    guaranteedIds.add(viewState.selectedNodeId);
  }
  if (viewState.hoveredNodeId) {
    guaranteedIds.add(viewState.hoveredNodeId);
  }
  if (viewState.focusedNodeId) {
    guaranteedIds.add(viewState.focusedNodeId);
  }
  if (viewState.hasFiltering && viewState.matchedIds.size <= 6) {
    viewState.matchedIds.forEach((nodeId) => guaranteedIds.add(nodeId));
  }

  candidates.sort((left, right) => right.priority - left.priority);
  const allowedIds = new Set(
    candidates.slice(0, budget).map((candidate) => candidate.id),
  );
  guaranteedIds.forEach((nodeId) => allowedIds.add(nodeId));

  candidates.forEach((candidate) => {
    const showLabel = allowedIds.has(candidate.id);
    candidate.label.style.display = showLabel ? "" : "none";
    if (!showLabel) {
      return;
    }
    candidate.label.style.left = `${candidate.x}px`;
    candidate.label.style.top = `${candidate.y}px`;
    candidate.label.style.opacity = `${Math.max(0.28, candidate.opacity)}`;
    candidate.label.classList.toggle(
      "is-selected",
      candidate.id === viewState.selectedNodeId,
    );
    candidate.label.classList.toggle(
      "is-hovered",
      candidate.id === viewState.hoveredNodeId,
    );
  });
}

function applyGraphVisualState(graphObjects, viewState) {
  if (!graphObjects) {
    return;
  }

  graphObjects.nodeMeshes.forEach((mesh) => {
    const nodeId = mesh.userData.nodeId;
    const isSelected = nodeId === viewState.selectedNodeId;
    const isHovered = nodeId === viewState.hoveredNodeId;
    const opacity = getNodeOpacity(nodeId, viewState);
    let scaleMultiplier = 1;

    if (viewState.matchedIds.has(nodeId)) {
      scaleMultiplier += 0.05;
    }
    if (isHovered) {
      scaleMultiplier += 0.08;
    }
    if (isSelected) {
      scaleMultiplier = 1.24;
    }

    mesh.scale.setScalar(mesh.userData.baseScale * scaleMultiplier);
    mesh.material.opacity = opacity;
    const baseEmissiveIntensity = mesh.userData.baseEmissiveIntensity ?? 0.18;
    mesh.material.emissiveIntensity = isSelected
      ? Math.max(0.72, baseEmissiveIntensity + 0.34)
      : isHovered || viewState.matchedIds.has(nodeId)
        ? Math.max(0.34, baseEmissiveIntensity + 0.16)
        : baseEmissiveIntensity;
    mesh.userData.currentOpacity = opacity;
  });

  graphObjects.edgeMeshes.forEach((edge) => {
    edge.material.opacity = getEdgeOpacity(edge, viewState);
  });

  graphObjects.labels.forEach((label, nodeId) => {
    label.classList.toggle("is-selected", nodeId === viewState.selectedNodeId);
    label.classList.toggle("is-hovered", nodeId === viewState.hoveredNodeId);
  });
}

function updateIdleOrbit(graphObjects, elapsedSeconds) {
  if (!graphObjects) {
    return;
  }

  graphObjects.nodeMeshes.forEach((mesh) => {
    const orbit = mesh.userData.orbit;
    if (!orbit) {
      return;
    }
    const angle = orbit.angle + elapsedSeconds * orbit.speed;
    mesh.position.x = Math.cos(angle) * orbit.radius;
    mesh.position.z = Math.sin(angle) * orbit.radius;
    mesh.position.y =
      orbit.y + Math.sin(elapsedSeconds * Math.abs(orbit.speed) * 1.7 + orbit.yPhase) * orbit.yAmplitude;
  });

  graphObjects.edgeMeshes.forEach((edge) => {
    const source = graphObjects.nodeMeshMap.get(edge.source);
    const target = graphObjects.nodeMeshMap.get(edge.target);
    if (!source || !target) {
      return;
    }
    const positions = edge.geometry.attributes.position;
    positions.setXYZ(0, source.position.x, source.position.y, source.position.z);
    positions.setXYZ(1, target.position.x, target.position.y, target.position.z);
    positions.needsUpdate = true;
    edge.geometry.computeBoundingSphere();
  });
}

export function initAgentGraph(root) {
  const canvas = root.querySelector(".graph-canvas");
  const labelLayer = root.querySelector(".graph-label-layer");
  const status = root.querySelector("[data-graph-status]");
  const searchInput = root.parentElement?.querySelector("[data-graph-search]");
  const kindFilter = root.parentElement?.querySelector("[data-graph-kind-filter]");
  const clearButton = root.parentElement?.querySelector("[data-graph-clear]");
  const resultCount = root.parentElement?.querySelector(
    "[data-graph-result-count]",
  );
  const selectionRoot = document.querySelector("[data-graph-selection]");
  updateGraphControlLabels(searchInput, kindFilter);
  let renderer;
  let scene;
  let camera;
  let controls;
  let raycaster;
  let graphObjects = null;
  let payload = null;
  let pointer = new THREE.Vector2();
  let hoveredNodeId = null;
  let selectedNodeId = "agent";
  let focusedNodeId = null;
  let active = false;
  let ready = false;
  let animationFrame = 0;
  let searchQuery = "";
  let kindFilterValue = normalizeKindFilterValue(kindFilter?.value || "all");
  let focusTransitionFrames = 0;
  let orbitPaused = false;
  let orbitElapsedSeconds = 0;
  let lastFrameTime = 0;
  const desiredTarget = DEFAULT_CAMERA_TARGET.clone();
  const desiredCameraPosition = DEFAULT_CAMERA_POSITION.clone();
  let filterState = {
    hasFiltering: false,
    matchedIds: new Set(),
    contextIds: null,
  };

  const setStatus = (message) => {
    if (status) {
      status.textContent = message;
      status.hidden = !message;
    }
  };

  const pauseIdleOrbit = () => {
    orbitPaused = true;
    if (controls) {
      controls.autoRotate = false;
    }
  };

  const buildViewState = () => {
    const focusContext = focusedNodeId
      ? buildContextSet(graphObjects.neighborMap, new Set([focusedNodeId]))
      : null;
    return {
      ...filterState,
      focusContext,
      focusedNodeId,
      hoveredNodeId,
      selectedNodeId,
    };
  };

  const updateResultCount = () => {
    if (!resultCount || !payload || !graphObjects) {
      return;
    }
    if (filterState.hasFiltering) {
      if (!filterState.matchedIds.size) {
        resultCount.textContent = "No matching nodes";
        return;
      }
      resultCount.textContent = `${
        filterState.matchedIds.size
      } matching node${filterState.matchedIds.size === 1 ? "" : "s"}`;
      return;
    }
    if (focusedNodeId) {
      const focusContext = buildContextSet(
        graphObjects.neighborMap,
        new Set([focusedNodeId]),
      );
      resultCount.textContent = `${
        Math.max(0, focusContext.size - 1)
      } nodes in focus`;
      return;
    }
    resultCount.textContent = `${
      Math.max(0, payload.nodes.length - 1)
    } runtime nodes`;
  };

  const refreshFilterState = () => {
    if (!payload || !graphObjects) {
      return;
    }
    filterState = computeFilterState(
      payload,
      graphObjects,
      searchQuery,
      kindFilterValue,
    );
    updateResultCount();
    if (filterState.hasFiltering && !filterState.matchedIds.size) {
      setStatus("No matching nodes. Adjust the search or type filter.");
    } else {
      setStatus("");
    }
  };

  const beginCameraFocus = (nodeId) => {
    focusedNodeId = nodeId && nodeId !== "agent" ? nodeId : null;
    if (!camera || !controls || !graphObjects) {
      return;
    }

    if (!focusedNodeId) {
      desiredTarget.copy(DEFAULT_CAMERA_TARGET);
      desiredCameraPosition.copy(DEFAULT_CAMERA_POSITION);
      focusTransitionFrames = 18;
      updateResultCount();
      return;
    }

    const focusMesh = graphObjects.nodeMeshMap.get(focusedNodeId);
    if (!focusMesh) {
      return;
    }
    const worldPosition = new THREE.Vector3();
    focusMesh.getWorldPosition(worldPosition);

    const offset = camera.position.clone().sub(controls.target);
    if (offset.lengthSq() < 0.0001) {
      offset.copy(DEFAULT_CAMERA_POSITION).sub(DEFAULT_CAMERA_TARGET);
    }
    const distance = Math.max(11.5, Math.min(15.8, offset.length()));
    offset.normalize().multiplyScalar(distance);

    desiredTarget.copy(worldPosition);
    desiredCameraPosition.copy(worldPosition).add(offset);
    focusTransitionFrames = 18;
    updateResultCount();
  };

  const setSelection = (nodeId, { focus = false } = {}) => {
    if (!payload || !graphObjects || !selectionRoot) {
      return;
    }
    const node = graphObjects.nodeMap.get(nodeId);
    if (!node) {
      return;
    }
    selectedNodeId = nodeId;
    buildSelectionView(
      selectionRoot,
      node,
      graphObjects.adjacencyCounts.get(nodeId) || 0,
    );
    if (focus) {
      beginCameraFocus(nodeId);
    }
  };

  const updatePointer = (event) => {
    const rect = canvas.getBoundingClientRect();
    pointer.x = ((event.clientX - rect.left) / rect.width) * 2 - 1;
    pointer.y = -((event.clientY - rect.top) / rect.height) * 2 + 1;
  };

  const pickNode = () => {
    if (!graphObjects || !raycaster) {
      return null;
    }
    raycaster.setFromCamera(pointer, camera);
    const intersects = raycaster.intersectObjects(graphObjects.nodeMeshes, false);
    return intersects[0]?.object?.userData?.nodeId || null;
  };

  const renderFrame = () => {
    if (!active || !ready || !renderer || !scene || !camera || !graphObjects) {
      return;
    }
    animationFrame = requestAnimationFrame(renderFrame);
    const now = performance.now();
    const deltaSeconds = lastFrameTime
      ? Math.min(0.05, (now - lastFrameTime) / 1000)
      : 0;
    lastFrameTime = now;
    if (resizeRendererToDisplaySize(renderer)) {
      camera.aspect = canvas.clientWidth / canvas.clientHeight;
      camera.updateProjectionMatrix();
    }

    if (!orbitPaused && !filterState.hasFiltering && !focusedNodeId) {
      orbitElapsedSeconds += deltaSeconds;
      updateIdleOrbit(graphObjects, orbitElapsedSeconds);
    }

    if (focusTransitionFrames > 0) {
      controls.target.lerp(desiredTarget, 0.18);
      camera.position.lerp(desiredCameraPosition, 0.18);
      focusTransitionFrames -= 1;
    }

    const viewState = buildViewState();
    applyGraphVisualState(graphObjects, viewState);
    controls.update();
    renderer.render(scene, camera);
    updateLabels(graphObjects, camera, canvas, viewState);
  };

  const start = () => {
    if (!ready || active) {
      return;
    }
    active = true;
    lastFrameTime = performance.now();
    renderFrame();
  };

  const stop = () => {
    active = false;
    if (animationFrame) {
      cancelAnimationFrame(animationFrame);
      animationFrame = 0;
    }
  };

  const handlePointerMove = (event) => {
    if (!ready) {
      return;
    }
    updatePointer(event);
    hoveredNodeId = pickNode();
    canvas.style.cursor = hoveredNodeId ? "pointer" : "grab";
  };

  const handlePointerLeave = () => {
    hoveredNodeId = null;
    canvas.style.cursor = "grab";
  };

  const handleClick = (event) => {
    if (!ready) {
      return;
    }
    pauseIdleOrbit();
    updatePointer(event);
    const pickedNode = pickNode();
    if (pickedNode) {
      setSelection(pickedNode, { focus: true });
    }
  };

  const handleSearchInput = (event) => {
    pauseIdleOrbit();
    searchQuery = event.currentTarget.value || "";
    refreshFilterState();
  };

  const handleKindFilter = (event) => {
    pauseIdleOrbit();
    kindFilterValue = normalizeKindFilterValue(event.currentTarget.value);
    refreshFilterState();
  };

  const handleClear = () => {
    pauseIdleOrbit();
    if (searchInput) {
      searchInput.value = "";
    }
    if (kindFilter) {
      kindFilter.value = "all";
    }
    searchQuery = "";
    kindFilterValue = "all";
    refreshFilterState();
    setSelection("agent");
    beginCameraFocus(null);
  };

  const destroy = () => {
    stop();
    canvas.removeEventListener("pointermove", handlePointerMove);
    canvas.removeEventListener("pointerleave", handlePointerLeave);
    canvas.removeEventListener("click", handleClick);
    canvas.removeEventListener("pointerdown", pauseIdleOrbit);
    canvas.removeEventListener("wheel", pauseIdleOrbit);
    searchInput?.removeEventListener("input", handleSearchInput);
    kindFilter?.removeEventListener("change", handleKindFilter);
    clearButton?.removeEventListener("click", handleClear);
    controls?.dispose();
    graphObjects?.geometry?.dispose();
    graphObjects?.edgeMeshes?.forEach((edge) => {
      edge.geometry.dispose();
      edge.material.dispose();
    });
    graphObjects?.nodeMeshes?.forEach((mesh) => mesh.material.dispose());
    renderer?.dispose();
    labelLayer?.replaceChildren();
  };

  try {
    renderer = new THREE.WebGLRenderer({ canvas, antialias: true, alpha: true });
  } catch (error) {
    setStatus("WebGL is unavailable in this browser. Use the runtime details below.");
    return {
      setActive() {},
      destroy,
    };
  }

  renderer.setClearColor(0xffffff, 0);
  renderer.outputColorSpace = THREE.SRGBColorSpace;
  renderer.toneMapping = THREE.ACESFilmicToneMapping;
  renderer.toneMappingExposure = 1.08;

  scene = new THREE.Scene();
  camera = new THREE.PerspectiveCamera(42, 1, 0.1, 100);
  camera.position.copy(DEFAULT_CAMERA_POSITION);

  controls = new OrbitControls(camera, canvas);
  controls.enableDamping = true;
  controls.enablePan = false;
  controls.minDistance = 11;
  controls.maxDistance = 26;
  controls.minPolarAngle = Math.PI * 0.22;
  controls.maxPolarAngle = Math.PI * 0.7;
  controls.autoRotate = false;
  controls.target.copy(DEFAULT_CAMERA_TARGET);
  controls.addEventListener("start", () => {
    pauseIdleOrbit();
    focusTransitionFrames = 0;
  });

  raycaster = new THREE.Raycaster();

  scene.add(new THREE.AmbientLight(0xffffff, 0.44));
  scene.add(new THREE.HemisphereLight(0xfff5d9, 0x8c5d49, 1.38));

  const keyLight = new THREE.DirectionalLight(0xffffff, 1.35);
  keyLight.position.set(6, 9, 10);
  scene.add(keyLight);

  const rimLight = new THREE.DirectionalLight(0xf0b17c, 0.78);
  rimLight.position.set(-8, 5, -7);
  scene.add(rimLight);

  const fillLight = new THREE.DirectionalLight(0xf8e7cf, 0.46);
  fillLight.position.set(0, -4, 6);
  scene.add(fillLight);

  canvas.addEventListener("pointermove", handlePointerMove);
  canvas.addEventListener("pointerleave", handlePointerLeave);
  canvas.addEventListener("click", handleClick);
  canvas.addEventListener("pointerdown", pauseIdleOrbit);
  canvas.addEventListener("wheel", pauseIdleOrbit, { passive: true });
  searchInput?.addEventListener("input", handleSearchInput);
  kindFilter?.addEventListener("change", handleKindFilter);
  clearButton?.addEventListener("click", handleClear);
  canvas.style.cursor = "grab";

  fetch(root.dataset.graphEndpoint || "/agent-graph.json", {
    headers: { Accept: "application/json" },
  })
    .then((response) => {
      if (!response.ok) {
        throw new Error(`Graph request failed with ${response.status}`);
      }
      return response.json();
    })
    .then((graphPayload) => {
      payload = graphPayload;
      graphObjects = createGraphObjects(payload, scene, labelLayer);
      setSelection("agent");
      refreshFilterState();
      updateResultCount();
      ready = true;
      setStatus("");
      if (active) {
        renderFrame();
      }
    })
    .catch((error) => {
      console.error(error);
      setStatus(
        "Could not load the graph payload. The runtime details below remain available.",
      );
    });

  return {
    setActive(nextActive) {
      if (nextActive) {
        if (ready) {
          start();
        } else {
          active = true;
        }
      } else {
        stop();
      }
    },
    destroy,
  };
}
