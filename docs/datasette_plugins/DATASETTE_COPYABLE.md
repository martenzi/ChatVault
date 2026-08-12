# Cheat Sheet: datasette-copyable

**Plugin Version:** `0.3.2`  
**Installed In:** `miniforge3` base environment  
**Hook Type:** Output renderer & template hook

---

## 1. Overview & Purpose

`datasette-copyable` adds instant export and copy options directly to Datasette query pages and table views.

Instead of needing to download a `.csv` or `.json` file to your Mac disk and open it, `datasette-copyable` provides buttons to copy query results in multiple formats directly to your clipboard.

---

## 2. How to Access & Activate

1. Execute any SQL query or navigate to any table/view page in Datasette.
2. Look above the table results header for the **Copyable** format bar.
3. Select your desired format:
   * **JSON**
   * **CSV**
   * **TSV**
   * **Markdown Table**
   * **HTML Table**
   * **SQL INSERTs**

---

## 3. What to Expect Visually

* **Format Selector Bar:** An interactive toolbar above query results table.
* **Instant Copy Modal / Textbox:** Clicking any format button (e.g. `Markdown` or `CSV`) opens a preview box with a **Copy to Clipboard** button.
* **One-Click Clipboard:** Clicking "Copy to Clipboard" instantly copies the query output into your system clipboard ready to paste into Obsidian, terminal scripts, or LLM chat prompts.

---

## 4. ChatVault Power-User Recipes

### Recipe A: Export Filtered Chat Summary into Obsidian Markdown Table

1. Run the canned query or custom SQL in Datasette:
   ```sql
   SELECT id, started_at, title, platform, model_primary
   FROM v_conversations
   WHERE platform = 'claude-code'
   ORDER BY started_at DESC;
   ```
2. Click **Markdown** on the copyable toolbar.
3. Click **Copy to Clipboard**.
4. Paste directly into your Obsidian markdown notes.

### Recipe B: Export Query Results as SQL INSERT Statements
Select **SQL** in the copyable toolbar to instantly generate valid `INSERT INTO ...` statements for testing or migration scripts.
