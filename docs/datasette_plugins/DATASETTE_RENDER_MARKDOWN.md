# Cheat Sheet: datasette-render-markdown

**Plugin Version:** `2.2.1`  
**Installed In:** `miniforge3` base environment  
**Hook Type:** Cell renderer & Jinja2 environment filter

---

## 1. Overview & Purpose

`datasette-render-markdown` automatically renders raw Markdown text stored inside SQLite database columns into rich, formatted HTML inside your Datasette web UI.

Without this plugin, conversation text like `**bold**`, headers `# Title`, or code snippets ```python ... ``` render as unformatted plain text. With this plugin active, table cells display styled typography, rendered lists, formatted links, blockquotes, and code blocks.

---

## 2. How to Activate & Configure

### Automatic Configuration (`metadata.json`)
The plugin is configured in ChatVault's `metadata.json` to automatically target columns commonly containing Markdown content (`content`, `text`, `message`, `body`):

```json
{
  "plugins": {
    "datasette-render-markdown": {
      "columns": [
        "content",
        "text",
        "message",
        "body"
      ]
    }
  }
}
```

### Manual / SQL Activation
You can also force any text column to render as Markdown in custom SQL queries by applying the custom SQL function `render_markdown()` or alias column names:

```sql
SELECT 
    role, 
    content AS message 
FROM v_messages 
LIMIT 20;
```

---

## 3. What to Expect Visually

When viewing table results containing markdown columns (e.g. `v_messages` or `v_blocks`):

* **Headings:** `# Heading` displays as styled bold header text.
* **Bold / Italics:** `**bold**` and `*italic*` render with appropriate font weights.
* **Lists:** `- item 1` renders as bulleted HTML lists `<ul><li>item 1</li></ul>`.
* **Code Blocks:** Fenced code blocks ```python ... ``` render inside dark code containers with monospace fonts.
* **Blockquotes:** `> quote` renders with a left vertical border and muted text tone.

---

## 4. ChatVault Power-User Recipes

### Recipe A: Browse Formatted Messages in `v_messages`
```sql
SELECT
    m.id,
    m.role,
    m.content AS message,
    m.created_at
FROM v_messages m
WHERE m.role = 'assistant'
ORDER BY m.created_at DESC
LIMIT 10;
```

### Recipe B: Inspect Rendered Blocks in `v_blocks`
```sql
SELECT
    b.block_type,
    b.content AS body
FROM v_blocks b
ORDER BY b.id DESC
LIMIT 15;
```
