# ChatVault Datasette Plugins & Canned Queries Guide

This directory contains individual cheat sheets for each Datasette plugin installed in the `miniforge3` Python environment, along with instructions for canned query configurations.

---

## 1. Configured Canned Queries (`metadata.json`)

The default canned query is configured in [`metadata.json`](file:///home/user/ChatVault/metadata.json):

```sql
SELECT
    id,
    started_at,
    title,
    platform,
    model_primary
FROM v_conversations
ORDER BY started_at DESC
```

* **Access:** Featured on the database home page (`http://localhost:8001/chatvault`) as **"Recent Conversations (Default View)"** and directly at `http://localhost:8001/chatvault/recent_conversations`.
* **Original Columns:** Preserves native SQLite schema column names (`id`, `started_at`, `title`, `platform`, `model_primary`) for full clarity on database structure.

---

## 2. Plugin Cheat Sheets

Click any link below for a full cheat sheet on usage, activation, visual UI expectations, and ChatVault SQL recipes:

* 📄 [**datasette-render-markdown**](DATASETTE_RENDER_MARKDOWN.md) — Renders Markdown text formatting inside message and block tables.
* 📄 [**datasette-search-all**](DATASETTE_SEARCH_ALL.md) — Adds cross-table full-text search at `http://localhost:8001/-/search-all`.
* 📄 [**datasette-json-html**](DATASETTE_JSON_HTML.md) — Formats JSON schemas into interactive HTML links and elements.
* 📄 [**datasette-pretty-json**](DATASETTE_PRETTY_JSON.md) — Automatically formats and syntax-highlights JSON data columns.
* 📄 [**datasette-copyable**](DATASETTE_COPYABLE.md) — Adds one-click copy buttons (Markdown, CSV, TSV, JSON, SQL) above query tables.
