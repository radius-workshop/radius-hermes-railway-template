---
name: linear
description: View and manage Linear issues, projects, and teams — create issues, update status, add comments, query projects
published: false
---

# Linear Skill

This agent can interact with a Linear workspace to view and manage issues, projects, and teams. Use this skill whenever the user asks about Linear tasks, bug tracking, project status, or wants to create or update issues.

The `LINEAR_API_KEY` environment variable must be set for this skill to work.

## When to use this skill

Use this skill whenever the user asks anything like:

- "create a Linear issue" / "add a bug to Linear" / "log this as a task"
- "what issues are in this project?" / "list my Linear issues" / "show open tasks"
- "update the status of ENG-123" / "mark that issue as done"
- "add a comment to ENG-123"
- "show me the Linear project" / "what's the project status?"
- "search for issues about X in Linear"
- any question involving Linear, issues, tickets, sprints, or project tracking

## Setup check

Before performing any Linear operation, if you haven't used Linear in this session yet, verify the connection:

```bash
cd /app/scripts/linear-skill && npm run test-connection
```

If this fails with a missing API key error, tell the user to set `LINEAR_API_KEY` in their Railway variables.

## Available commands

All commands are run from `/app/scripts/linear-skill` using `npm run`. Always prefix with `cd /app/scripts/linear-skill &&`.

### Query issues and projects (GraphQL)

For flexible querying, use the `query` script with a GraphQL string:

```bash
# Get current user
cd /app/scripts/linear-skill && npm run query -- "query { viewer { name email } }"

# List issues assigned to me
cd /app/scripts/linear-skill && npm run query -- "query { viewer { assignedIssues { nodes { identifier title state { name } priority } } } }"

# List teams
cd /app/scripts/linear-skill && npm run query -- "query { teams { nodes { id name key } } }"

# List projects for a team (replace TEAM_ID)
cd /app/scripts/linear-skill && npm run query -- "query { team(id: \"TEAM_ID\") { projects { nodes { id name state { name } description } } } }"

# Get issues in a project (replace PROJECT_ID)
cd /app/scripts/linear-skill && npm run query -- "query { project(id: \"PROJECT_ID\") { issues { nodes { identifier title state { name } assignee { name } priority } } } }"

# Search issues by text
cd /app/scripts/linear-skill && npm run query -- "query { issueSearch(query: \"SEARCH_TERM\") { nodes { identifier title state { name } } } }"
```

### Create and update issues (ops)

```bash
# Show all available ops commands
cd /app/scripts/linear-skill && npm run ops -- help

# Show current user and org
cd /app/scripts/linear-skill && npm run ops -- whoami

# List all initiatives
cd /app/scripts/linear-skill && npm run ops -- list-initiatives

# List all projects (optionally filter by initiative name)
cd /app/scripts/linear-skill && npm run ops -- list-projects
cd /app/scripts/linear-skill && npm run ops -- list-projects "Q1 Goals"

# Create an issue in a project
cd /app/scripts/linear-skill && npm run ops -- create-issue "Project Name" "Issue title" "Detailed description of what needs to be done and why."

# Create an issue with labels and priority
cd /app/scripts/linear-skill && npm run ops -- create-issue "Project Name" "Fix login bug" "Users cannot log in on mobile." --labels bug,frontend --priority 2

# Create a sub-issue under a parent
cd /app/scripts/linear-skill && npm run ops -- create-sub-issue ENG-123 "Sub-task title" "Description of sub-task."

# Update issue status
cd /app/scripts/linear-skill && npm run ops -- status Done ENG-123
cd /app/scripts/linear-skill && npm run ops -- status "In Progress" ENG-123 ENG-124

# Update multiple issues at once
cd /app/scripts/linear-skill && npm run ops -- status Done ENG-100 ENG-101 ENG-102

# Create a project
cd /app/scripts/linear-skill && npm run ops -- create-project "Phase 1: Feature Name" "Initiative Name"

# Update project status
cd /app/scripts/linear-skill && npm run ops -- project-status "Project Name" in-progress
# Valid states: backlog, planned, in-progress, paused, completed, canceled

# Create a project update with health status
cd /app/scripts/linear-skill && npm run ops -- create-project-update "Project Name" "Weekly update body text"
```

## Scoping to a specific project

If `LINEAR_PROJECT_ID` is set in the environment, the user has scoped this agent to a specific Linear project. For most queries and issue creation, use this project as the default when the user doesn't specify one.

To get the project details when `LINEAR_PROJECT_ID` is set:

```bash
PROJECT_ID="${LINEAR_PROJECT_ID}"
cd /app/scripts/linear-skill && npm run query -- "query { project(id: \"${PROJECT_ID}\") { name description state { name } issues { nodes { identifier title state { name } priority assignee { name } } } } }"
```

If `LINEAR_TEAM_ID` is set, use it when querying team-specific data to avoid ambiguity across teams.

## Issue creation checklist

When creating a Linear issue, always do these three things — even if the user only gives you a title:

1. **Write a detailed description**: include what the change is, why it's needed, and acceptance criteria. If the user only gives a title, infer and write the description yourself.

2. **Apply labels**: use `--labels` with one type label plus 1–2 domain labels:
   - Type labels: `feature`, `bug`, `refactor`, `chore`, `spike`
   - Domain labels: `backend`, `frontend`, `security`, `infrastructure`, `data`, `mobile`

3. **Assign to a project**: ask the user which project if not obvious, or use the project from `LINEAR_PROJECT_ID`.

## Error handling

- **"LINEAR_API_KEY environment variable is required"** — tell the user to add `LINEAR_API_KEY` to their Railway variables
- **"Project not found"** — the project name doesn't match; use `npm run ops -- list-projects` to find the correct name
- **"Issue not found"** — verify the issue identifier (e.g., ENG-123); use the query script to search for it
- **Build errors** — if `dist/` scripts are missing, run `npm run build` first from `/app/scripts/linear-skill`
