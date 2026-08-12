# Cheat Sheet: datasette-search-all

**Plugin Version:** `1.1.4`  
**Installed In:** `miniforge3` base environment  
**Hook Type:** Global route (`/-/search-all`) & top navbar integration

---

## 1. Overview & Purpose

`datasette-search-all` adds a cross-table full-text search engine to Datasette. Instead of querying individual tables one by one, `datasette-search-all` searches across **all Full-Text Search (FTS) index tables** in `chatvault.sqlite` simultaneously and presents unified search results.

---

## 2. How to Access & Activate

### URL Route
Navigate directly to:
```
http://localhost:8001/-/search-all
```

### Top Navigation Bar
When Datasette is running, click **Search all tables** in the top navigation bar header on any Datasette page.

---

## 3. What to Expect Visually

* **Search Input Field:** A large, focused search box on `/-/search-all`.
* **Grouped Results:** When you search for terms (e.g. `sqlite`, `neo4j`, `error`, `hermes`), results are organized into separate panels for each matching FTS table (e.g. `messages_fts`, `conversations_fts`).
* **Hit Counts & Snippets:** Each table panel displays the number of matching records along with text snippets highlighting the matched search terms.
* **Clickable Drill-Down Links:** Clicking any result opens the exact conversation or message in Datasette.

---

## 4. ChatVault Power-User Recipes

### Workflow: Instant Keyword Discovery Across All Indexed Chats

1. Start Datasette (`chatvault.command` option `10` or direct CLI).
2. Open `http://localhost:8001/-/search-all`.
3. Type a query string:
   * `docker-compose`
   * `sqlite-vec`
   * `launchd`
4. Review hits across both conversation metadata and message contents instantly.
