// Checks that a pull request description follows .github/pull_request_template.md.
// Usage: PR_BODY="..." bun run check-pr-body.ts

export const SECTIONS = ["Summary", "Risks", "How to revert"] as const;
export const MAX_SUMMARY_BULLETS = 6;
const TEMPLATE = ".github/pull_request_template.md";

/** The text under each `## Heading`, with HTML comments (the template's hints) removed. */
export function sections(body: string): Map<string, string> {
  const text = body.replace(/\r\n/g, "\n").replace(/<!--[\s\S]*?-->/g, "");
  const found = new Map<string, string>();
  let current: string | null = null;
  for (const line of text.split("\n")) {
    const heading = /^##\s+(.+?)\s*#*\s*$/.exec(line);
    if (heading) {
      current = heading[1].toLowerCase();
      if (!found.has(current)) found.set(current, "");
    } else if (current !== null) {
      found.set(current, found.get(current) + line + "\n");
    }
  }
  return found;
}

/** Things worth a nudge that don't fail the check. */
export function warnings(body: string): string[] {
  const summary = sections(body ?? "").get("summary") ?? "";
  const bullets = summary.split("\n").filter((line) => /^[-*]\s+\S/.test(line));
  return bullets.length > MAX_SUMMARY_BULLETS
    ? [`"## Summary" has ${bullets.length} bullets; it reads best with ${MAX_SUMMARY_BULLETS} or fewer.`]
    : [];
}

/** Every problem with the description; empty when it passes. */
export function problems(body: string): string[] {
  const found = sections(body ?? "");
  const errors: string[] = [];
  for (const name of SECTIONS) {
    const text = found.get(name.toLowerCase());
    if (text === undefined) errors.push(`Missing the "## ${name}" section.`);
    else if (!text.trim()) errors.push(`"## ${name}" is empty.`);
  }
  const summary = found.get("summary")?.trim();
  if (summary) {
    const bullets = summary.split("\n").filter((line) => /^\s*[-*]\s+\S/.test(line));
    if (bullets.length === 0) errors.push('"## Summary" needs at least one bullet ("- ...").');
  }
  return errors;
}

if (import.meta.main) {
  const errors = problems(process.env.PR_BODY ?? "");
  for (const warning of warnings(process.env.PR_BODY ?? "")) console.log(`::warning title=PR description::${warning}`);
  for (const error of errors) console.log(`::error title=PR description::${error}`);
  if (errors.length > 0) {
    console.log(`\nThe PR description must follow ${TEMPLATE}: edit the description and this check runs again.`);
    process.exit(1);
  }
  console.log("PR description follows the template.");
}
