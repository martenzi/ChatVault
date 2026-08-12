# Cheat Sheet: datasette-pretty-json

**Plugin Version:** `0.3`  
**Installed In:** `miniforge3` base environment  
**Hook Type:** Cell renderer

---

## 1. Overview & Purpose

`datasette-pretty-json` detects raw JSON text strings inside table cells and automatically formats and pretty-prints them with syntax highlighting, indentation, and structure.

Without this plugin, JSON columns (such as raw export headers, tool call parameters, or metadata dictionaries) appear as dense, unreadable single-line strings. With this plugin active, JSON fields expand into structured code blocks.

---

## 2. How to Activate & Trigger

The plugin operates automatically on any column whose content is valid JSON.

It requires no manual SQL syntax—simply query or browse any table containing JSON string data (e.g. `source_files` or tool output metadata).

---

## 3. What to Expect Visually

* **Unformatted Input:** `{"platform":"claude-code","version":"1.0","active":true}`
* **Pretty-Printed Output:**
```json
{
  "platform": "claude-code",
  "version": "1.0",
  "active": true
}
```
* **Styling:** Key names, strings, numbers, and booleans are styled in code font inside dark mode container cells.

---

## 4. ChatVault Power-User Recipes

### Recipe A: Inspect Source File Metadata
```sql
SELECT
    path,
    platform,
    status,
    ingested_at
FROM source_files
ORDER BY ingested_at DESC
LIMIT 10;
```

### Recipe B: Parse Raw JSON Strings in Custom SQL Queries
```sql
SELECT
    id,
    path,
    json_extract(path, '$') AS formatted_path
FROM source_files
LIMIT 10;
```
