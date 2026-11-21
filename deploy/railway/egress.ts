import { defineRailway, project, service } from "railway/iac";

// This partial owns only egress. Existing agent services remain unmanaged here.
export const partial = "egress";

export default defineRailway((ctx) => {
  if (
    ctx.projectId !== "6be9491a-1fd6-4372-ba7e-559cfe9dd469" ||
    ctx.environmentId !== "1767c49d-10e4-4c8f-8d36-5e6dbcc39e1a"
  ) {
    throw new Error("This egress configuration targets agent0's production environment only.");
  }

  const egress = service("egress", {
    build: { buildEnvironment: "V3", builder: "dockerfile" },
    start: "python3 /app/scripts/egress/iron_egress.py proxy",
    deploy: {
      healthcheckPath: null,
      requiredMountPath: null,
      restartPolicyType: "ALWAYS",
    },
    replicas: { "us-east4-eqdc4a": 1 },
  });

  // Preserve Railway-managed variables, including sealed credentials. This
  // shape is emitted by the installed Railway CLI's single-service migrator.
  return project("hermes-0", {
    variables: { managed: false },
    resources: [egress],
  });
});
