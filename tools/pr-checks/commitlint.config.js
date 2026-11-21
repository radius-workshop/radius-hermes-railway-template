// Conventional Commits (https://www.conventionalcommits.org), checked on every PR commit and on the PR title.
export default {
  extends: ["@commitlint/config-conventional"],
  // Messages GitHub's web UI writes for people, which they can't reword there.
  ignores: [
    (message) => /^(Apply suggestions? from code review|Potential fix for )/.test(message),
    (message) => /^(Update|Create|Delete|Rename) \S+( to \S+)?\n*$/.test(message),
  ],
  rules: {
    // "fix: Slack home crashes on an empty wallet" is fine; proper nouns start sentences.
    "subject-case": [0],
    "header-max-length": [2, "always", 100],
    // Bodies and footers hold URLs, logs and trailers; don't wrap-police them.
    "body-max-line-length": [0],
    "footer-max-line-length": [0],
  },
};
