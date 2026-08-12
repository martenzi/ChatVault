# Concepts — Label vs Tag vs project_id

**Written:** 2026-08-09
**Why:** these three get confused constantly. They are genuinely different mechanisms with different rules.

---

## The one-line version

> **A project label says where a conversation came FROM. A tag says what YOU think about it.**

One is a fact from the source. The other is your judgement, added later.

---

## The three things

### 1. Project label — `conversations.project`

A plain text column. One value per conversation.

| | |
|---|---|
| **Who sets it** | The adapter, automatically, during ingest |
| **From what** | The source data — a `cwd` field, a `workspace_uri`, a folder name |
| **How many per conversation** | Exactly one, or nothing |
| **Can you edit it** | Yes, but you shouldn't — it's a record of fact |
| **What it can hold** | A name. Nothing else. `"latex"` is five characters and no more. |

This is what you have today. It works — `chatvault find --project "latex"` filters on it.

### 2. `project_id` — a link to the `projects` table

A number pointing at a real row in a real table.

| | |
|---|---|
| **Who sets it** | The loader, when a proper project record exists |
| **What the row holds** | Name, **full path**, platform, kind, description, dates, provenance |
| **How many per conversation** | Exactly one, or nothing |
| **Current state** | **Zero conversations have one.** The table exists; nothing populates it for workspaces. |

**What "project_id is 0" practically means:** the `projects` table is empty of workspace projects. Nothing is broken and nothing is lost — filtering still works off the string. But a project can't carry a path, a description, or anything beyond its name, and `chatvault projects list` shows nothing for editor workspaces.

It's the difference between writing "CSN_Case" on a folder tab, and having a folder that knows where it lives on disk, when it started, and what it's about.

### 3. Tag — `tag_links`

A many-to-many link between conversations and tags.

| | |
|---|---|
| **Who sets it** | **You.** Or a rule you wrote. Or an AI you asked. Always after ingest. |
| **How many per conversation** | Unlimited |
| **Structure** | Hierarchical — `tech/macos/npm` |
| **Set automatically at ingest?** | **No, never** — except deliberate `review/*` flags (see below) |

---

## Side by side

| | Project label | project_id | Tag |
|---|---|---|---|
| Answers | "where did this come from?" | same, but properly | "what is this about?" |
| Per conversation | one | one | many |
| Set when | at ingest | at ingest | whenever you like |
| Set by | the adapter | the loader | you / rule / AI |
| Nature | **fact** | **fact** | **judgement** |
| Should you change it | rarely | rarely | constantly |

**The rule that resolves most confusion:**

> Project = provenance. It records something true about where the conversation happened.
> Tag = interpretation. It records something *you decided*.

A conversation that happened in the `CSN_Case` workspace **is** a CSN_Case conversation — that's not an opinion. Whether it's `purpose/troubleshooting` or `value/gold` is entirely your call, and can change.

---

## Where each one comes from, per platform

| Platform | Project label source |
|---|---|
| cursor, vscode, cursor_copilot, windsurf | `workspace_uri` in the legacy export → resolved during corpus export in `history-r` |
| claude_code, openclaw | `cwd` field written by the tool itself |
| hermes | session header `cwd` |
| chatgpt, claude_web | **none** — web platforms have no workspace concept |

`claude_web` should eventually get `project_id` from Claude Desktop projects, which are a different mechanism entirely — a conceptual grouping you created inside Claude, not a folder on disk. Both are "projects" for filtering purposes and rank equally; they just come from different places, which is why `projects.kind` exists.

---

## The `review/*` exception

The one case where tags *are* applied automatically at ingest: marking anomalies.

```
review/project-unresolved     no workspace could be determined
review/project-ambiguous      name collides with a different path
review/project-scratch        an ad-hoc editor session, no real project
review/model-unknown          model couldn't be recovered
```

This is deliberate, and it follows a principle worth stating:

> **Flag, don't guess.** When something is odd, mark it rather than silently picking an answer.

Anything flagged stays findable forever — `chatvault find --tag "review/**"` — so nothing odd disappears quietly, and you can review it whenever you feel like it rather than at ingest time.
