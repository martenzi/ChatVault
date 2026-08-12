# Cheat Sheet: datasette-json-html

**Plugin Version:** `1.0.1`  
**Installed In:** `miniforge3` base environment  
**Hook Type:** Cell renderer & SQLite connection hook

---

## 1. Overview & Purpose

`datasette-json-html` converts structured JSON objects in SQL query outputs into interactive HTML elements.

Instead of displaying raw JSON text strings like `{"href": "...", "label": "..."}`, this plugin renders clickable hyperlinks, images, tags, audio, video, or formatted data structures directly inside Datasette table cells.

---

## 2. How to Activate & Trigger

The plugin automatically intercepts any column returning a JSON string matching supported schemas or generated using SQLite `json_object()` functions.

### Supported JSON Schemas:

#### A. Hyperlinks (`href`)
```json
{
  "href": "https://github.com",
  "label": "Open Repository"
}
```

#### B. Images (`img_src`)
```json
{
  "img_src": "https://example.com/logo.png",
  "alt": "Logo Preview"
}
```

---

## 3. What to Expect Visually

* **Raw JSON String:** `{"href": "/chatvault/v_conversations?id=123", "label": "View Chat"}`
* **Rendered Output:** Displays an interactive, clickable HTML link: <a href="/chatvault/v_conversations?id=123">View Chat</a>.

---

## 4. ChatVault Power-User Recipes

### Recipe A: Build Dynamic Navigation Links in Custom SQL Queries
```sql
SELECT
    title,
    platform,
    json_object(
        'href', '/chatvault/v_messages?conversation_id=' || id,
        'label', 'Explore Messages (' || id || ')'
    ) AS action_link
FROM v_conversations
ORDER BY started_at DESC
LIMIT 15;
```

### Recipe B: Create Clickable External Web References
```sql
SELECT
    model_primary,
    json_object(
        'href', 'https://openrouter.ai/models/' || model_primary,
        'label', 'View Model Specs'
    ) AS model_doc
FROM v_conversations
WHERE model_primary IS NOT NULL
GROUP BY model_primary;
```
