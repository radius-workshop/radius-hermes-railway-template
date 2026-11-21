import { expect, test } from "bun:test";
import { readFileSync } from "node:fs";
import { problems, SECTIONS, warnings } from "./check-pr-body";

const GOOD = `## Summary

- Adds a thing.
- Removes another.

## Risks

Low: docs only.

## How to revert

Revert this PR.
`;

test("a filled-in description passes", () => {
  expect(problems(GOOD)).toEqual([]);
  expect(problems(GOOD.replace(/\n/g, "\r\n") + "\n## Testing\n\nRan the suite.\n")).toEqual([]);
});

test("the untouched template fails, with every section named", () => {
  const template = readFileSync(new URL("../../.github/pull_request_template.md", import.meta.url), "utf8");
  for (const name of SECTIONS) expect(template).toContain(`## ${name}`);
  const errors = problems(template);
  expect(errors).toHaveLength(SECTIONS.length);
  for (const name of SECTIONS) expect(errors.join("\n")).toContain(`"## ${name}" is empty`);
});

test("an empty or free-form description fails", () => {
  expect(problems("")).toHaveLength(3);
  expect(problems("Fixes the bug.")).toHaveLength(3);
});

test("the summary must be a short bulleted list", () => {
  expect(problems(GOOD.replace("- Adds a thing.\n- Removes another.", "Adds a thing."))).toEqual([
    '"## Summary" needs at least one bullet ("- ...").',
  ]);
  const many = GOOD.replace("- Adds a thing.\n- Removes another.", Array.from({ length: 7 }, (_, i) => `- Change ${i}.`).join("\n"));
  expect(problems(many)).toEqual([]);
  expect(warnings(many)).toEqual(['"## Summary" has 7 bullets; it reads best with 6 or fewer.']);
  expect(warnings(GOOD)).toEqual([]);
  expect(problems(GOOD.replace("- Removes another.", "- Removes another,\nwrapped onto a second line.\n\nSee `x`.\n  - nested"))).toEqual([]);
});

test("a section holding only a comment is empty", () => {
  expect(problems(GOOD.replace("Revert this PR.", "<!-- how? -->"))).toEqual(['"## How to revert" is empty.']);
  expect(problems(GOOD.replace("## Risks", "## Risk"))).toEqual(['Missing the "## Risks" section.']);
});
