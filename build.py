#!/usr/bin/env python3
"""
Malleus Clinical Medicine — Static Site Builder
================================================
Run:  python3 build.py
Deps: pip install requests

Outputs: index.html, getting-started.html, submission-guidelines.html,
         checklist.html, register.html  (all in ./dist/)

Config
------
Copy .env.example to .env and fill in your values:
    cp .env.example .env
Or pass via environment variables:
    NOTION_TOKEN=ntn_... python3 build.py
"""

import os, re, json, textwrap, requests, shutil
from pathlib import Path
from html import escape

# ── Load .env file (if present) ──────────────────────────────────────────────
_env_path = Path(__file__).parent / ".env"
if _env_path.exists():
    for _line in _env_path.read_text().splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _v = _line.split("=", 1)
            os.environ.setdefault(_k.strip(), _v.strip())

# ── Config ──────────────────────────────────────────────────────────────────
NOTION_TOKEN = os.getenv("NOTION_TOKEN", "")
FORM_URL     = "https://docs.google.com/forms/d/e/1FAIpQLSd6G7DAgEeKjS-sXuX-Mvzfo5BGWaEpRZ9n3Sf2e4E1be7kXw/viewform"
FORM_POST    = FORM_URL.replace("/viewform", "/formResponse")
LOGO_PATH    = "logo.png"   # put your logo file here (same dir as build.py)
DIST_DIR     = Path("dist")

NOTION_PAGES = {
    "getting-started":         "31d5964e68a4807ba315f7413b776b1a",
    "submission-guidelines":   "31d5964e68a480fea3e3f9eed0c43486",
    "checklist":               "31d5964e68a4804dbd60c509ddf513ac",
}

NOTION_HEADERS = {
    "Authorization":  f"Bearer {NOTION_TOKEN}",
    "Notion-Version": "2022-06-28",
    "Content-Type":   "application/json",
}

# ── Notion → HTML ────────────────────────────────────────────────────────────

def rich_text_to_html(rich_texts: list) -> str:
    """Convert a Notion rich_text array to HTML."""
    out = []
    for rt in rich_texts:
        text = escape(rt.get("plain_text", ""))
        ann  = rt.get("annotations", {})
        href = rt.get("href")

        if ann.get("bold"):        text = f"<strong>{text}</strong>"
        if ann.get("italic"):      text = f"<em>{text}</em>"
        if ann.get("strikethrough"): text = f"<s>{text}</s>"
        if ann.get("underline"):   text = f"<u>{text}</u>"
        if ann.get("code"):        text = f"<code>{text}</code>"
        if href:                   text = f'<a href="{escape(href)}" target="_blank">{text}</a>'
        out.append(text)
    return "".join(out)


def fetch_blocks(block_id: str) -> list:
    """Recursively fetch all block children."""
    blocks = []
    url = f"https://api.notion.com/v1/blocks/{block_id}/children?page_size=100"
    while url:
        r = requests.get(url, headers=NOTION_HEADERS)
        if r.status_code == 404:
            print(f"    ⚠️  Block {block_id} not found (404) — skipping.")
            return blocks
        r.raise_for_status()
        data = r.json()
        for b in data.get("results", []):
            blocks.append(b)
            if b["type"] == "synced_block":
                synced_from = b.get("synced_block", {}).get("synced_from")
                if synced_from:
                    # Secondary synced block: content lives in the source block,
                    # not in self (self returns empty even when has_children=True).
                    b["_children"] = fetch_blocks(synced_from["block_id"])
                elif b.get("has_children"):
                    b["_children"] = fetch_blocks(b["id"])
            elif b.get("has_children"):
                b["_children"] = fetch_blocks(b["id"])
        url = data.get("next_cursor") and \
              f"https://api.notion.com/v1/blocks/{block_id}/children?page_size=100&start_cursor={data['next_cursor']}"
    return blocks


def fetch_database_as_table(db_id: str, title: str = "") -> str:
    """Query a Notion database and render it as an HTML table."""
    try:
        r = requests.post(
            f"https://api.notion.com/v1/databases/{db_id}/query",
            headers=NOTION_HEADERS,
            json={"page_size": 100, "sorts": [{"property": "", "direction": "ascending"}]}
        )
        if r.status_code != 200:
            return f'<div class="notion-child-page">🗄️ {escape(title)}</div>'
        rows = r.json().get("results", [])
        if not rows:
            return f'<div class="notion-child-page">🗄️ {escape(title)} (empty)</div>'

        # Get property names from first row, put title property first
        props = rows[0]["properties"]
        # Find title property
        title_prop = next((k for k, v in props.items() if v["type"] == "title"), None)
        other_props = [k for k in props if k != title_prop and k != ""]

        headers_list = ([title_prop] if title_prop else []) + other_props
        headers_html = "".join(f"<th>{escape(h)}</th>" for h in headers_list)

        def cell_value(prop):
            ptype = prop["type"]
            val = prop.get(ptype, "")
            if ptype == "title" or ptype == "rich_text":
                return escape("".join(rt.get("plain_text", "") for rt in (val or [])))
            elif ptype == "select":
                return escape(val.get("name", "") if val else "")
            elif ptype == "multi_select":
                return escape(", ".join(o.get("name", "") for o in (val or [])))
            elif ptype == "checkbox":
                return "✓" if val else ""
            elif ptype == "url":
                return f'<a href="{escape(val or "")}" target="_blank">{escape(val or "")}</a>' if val else ""
            else:
                return escape(str(val))[:80]

        rows_html = ""
        for row in rows:
            cells = "".join(
                f"<td>{cell_value(row['properties'].get(h, {'type': 'rich_text', 'rich_text': []}))}</td>"
                for h in headers_list
            )
            rows_html += f"<tr>{cells}</tr>"

        label = f"<h3 class='notion-h2'>🗄️ {escape(title)}</h3>" if title else ""
        return f'{label}<div class="notion-table-wrap"><table class="notion-table"><tr>{headers_html}</tr>{rows_html}</table></div>'
    except Exception as e:
        return f'<div class="notion-child-page">🗄️ {escape(title)} (error: {escape(str(e))})</div>'


def _slugify(text: str) -> str:
    import re as _re
    text = _re.sub(r'<[^>]+>', '', text)
    text = _re.sub(r'[^\w\s-]', '', text.lower())
    return _re.sub(r'[\s_-]+', '-', text).strip('-')


def add_ids_and_build_toc(content_html: str) -> tuple[str, str]:
    """Add id attributes to headings and return (modified_html, toc_html)."""
    import re as _re
    toc_items = []
    used: dict = {}

    def make_id(text):
        slug = _slugify(text)
        slug = slug or "section"
        if slug in used:
            used[slug] += 1
            slug = f"{slug}-{used[slug]}"
        else:
            used[slug] = 0
        return slug

    # Single pass: match both plain headings and toggle summaries in document order
    # Group 1-3: plain heading  |  Group 4-5: toggle summary
    def combined_replace(m):
        if m.group(1):
            tag, cls, inner = m.group(1), m.group(2), m.group(3)
            plain = _re.sub(r'<[^>]+>', '', inner).strip()
            sid = make_id(plain)
            level = {'notion-h1': 1, 'notion-h2': 2, 'notion-h3': 3}.get(cls, 1)
            if level <= 2:
                toc_items.append((level, plain, sid))
            return f"<{tag} class='{cls}' id='{sid}'>{inner}</{tag}>"
        else:
            cls, inner = m.group(4), m.group(5)
            plain = _re.sub(r'<[^>]+>', '', inner).strip()
            sid = make_id(plain)
            level = {'notion-h1': 1, 'notion-h2': 2, 'notion-h3': 3}.get(cls, 1)
            if level <= 2:
                toc_items.append((level, plain, sid))
            return f"<summary class='{cls}' id='{sid}'>{inner}</summary>"

    html = _re.sub(
        r"<(h[2-4]) class='(notion-h[123])'>(.*?)</\1>|<summary class='(notion-h[123])'>(.*?)</summary>",
        combined_replace, content_html, flags=_re.DOTALL
    )

    if not toc_items:
        return html, ""

    items_html = [
        f'<li class="toc-h{level}"><a href="#{sid}">{escape(text)}</a></li>'
        for level, text, sid in toc_items
    ]
    toc_html = (
        '<div class="toc-panel">'
        '<div class="toc-tab">'
        '<span class="toc-tab-icon">&#8801;</span>'
        '<span class="toc-tab-label">Contents</span>'
        '</div>'
        '<nav class="toc"><div class="toc-inner">'
        '<div class="toc-header"><span class="toc-title">On this page</span></div>'
        f'<ul>{"".join(items_html)}</ul>'
        '</div></nav>'
        '</div>'
    )
    return html, toc_html


def blocks_to_html(blocks: list, depth: int = 0) -> str:
    """Convert a list of Notion blocks to HTML."""
    html = []
    i = 0
    while i < len(blocks):
        b    = blocks[i]
        btype = b["type"]
        data  = b.get(btype, {})

        # ── Lists: gather consecutive items ─────────────────────────────────
        if btype == "bulleted_list_item":
            items = []
            while i < len(blocks) and blocks[i]["type"] == "bulleted_list_item":
                bi   = blocks[i]
                bd   = bi["bulleted_list_item"]
                text = rich_text_to_html(bd.get("rich_text", []))
                ch   = blocks_to_html(bi.get("_children", [])) if bi.get("_children") else ""
                items.append(f"<li>{text}{ch}</li>")
                i += 1
            html.append(f'<ul class="notion-ul">{"".join(items)}</ul>')
            continue

        if btype == "numbered_list_item":
            items = []
            while i < len(blocks) and blocks[i]["type"] == "numbered_list_item":
                bi   = blocks[i]
                bd   = bi["numbered_list_item"]
                text = rich_text_to_html(bd.get("rich_text", []))
                ch   = blocks_to_html(bi.get("_children", [])) if bi.get("_children") else ""
                items.append(f"<li>{text}{ch}</li>")
                i += 1
            html.append(f'<ol class="notion-ol">{"".join(items)}</ol>')
            continue

        if btype == "to_do":
            items = []
            while i < len(blocks) and blocks[i]["type"] == "to_do":
                bi    = blocks[i]
                bd    = bi["to_do"]
                text  = rich_text_to_html(bd.get("rich_text", []))
                ck    = "checked" if bd.get("checked") else ""
                ch    = blocks_to_html(bi.get("_children", [])) if bi.get("_children") else ""
                items.append(f'<li class="notion-todo"><input type="checkbox" {ck} disabled><span class="notion-todo-text">{text}</span>{ch}</li>')
                i += 1
            html.append(f'<ul class="notion-todo-list">{"".join(items)}</ul>')
            continue

        # ── Single blocks ────────────────────────────────────────────────────
        ch = blocks_to_html(b.get("_children", [])) if b.get("_children") else ""

        if btype == "paragraph":
            text = rich_text_to_html(data.get("rich_text", []))
            if text.strip():
                html.append(f"<p>{text}</p>")
            elif ch:
                html.append(ch)

        elif btype == "heading_1":
            text = rich_text_to_html(data.get("rich_text", []))
            if data.get("is_toggleable") and ch:
                html.append(f"<details class='notion-toggle'><summary class='notion-h1'>{text}</summary><div class='toggle-body'>{ch}</div></details>")
            else:
                html.append(f"<h2 class='notion-h1'>{text}</h2>{ch}")

        elif btype == "heading_2":
            text = rich_text_to_html(data.get("rich_text", []))
            if data.get("is_toggleable") and ch:
                html.append(f"<details class='notion-toggle'><summary class='notion-h2'>{text}</summary><div class='toggle-body'>{ch}</div></details>")
            else:
                html.append(f"<h3 class='notion-h2'>{text}</h3>{ch}")

        elif btype == "heading_3":
            text = rich_text_to_html(data.get("rich_text", []))
            if data.get("is_toggleable") and ch:
                html.append(f"<details class='notion-toggle'><summary class='notion-h3'>{text}</summary><div class='toggle-body'>{ch}</div></details>")
            else:
                html.append(f"<h4 class='notion-h3'>{text}</h4>{ch}")

        elif btype == "quote":
            text = rich_text_to_html(data.get("rich_text", []))
            html.append(f"<blockquote class='notion-quote'>{text}</blockquote>")

        elif btype == "callout":
            text  = rich_text_to_html(data.get("rich_text", []))
            emoji = data.get("icon", {}).get("emoji", "💡")
            color = data.get("color", "default")
            cls   = f"notion-callout notion-callout-{color}"
            html.append(f'<div class="{cls}"><span class="callout-icon">{emoji}</span><div>{text}{ch}</div></div>')

        elif btype == "code":
            text = escape("".join(rt.get("plain_text","") for rt in data.get("rich_text", [])))
            lang = data.get("language", "")
            html.append(f'<pre class="notion-code"><code class="language-{lang}">{text}</code></pre>')

        elif btype == "divider":
            html.append('<hr class="notion-divider">')

        elif btype == "image":
            img_data = data.get("file", {}) or data.get("external", {})
            url  = img_data.get("url", "")
            cap  = rich_text_to_html(data.get("caption", []))
            html.append(f'<figure class="notion-image"><img src="{escape(url)}" alt="{escape(cap)}" loading="lazy"><figcaption>{cap}</figcaption></figure>')

        elif btype == "toggle":
            text = rich_text_to_html(data.get("rich_text", []))
            html.append(f'<details class="notion-toggle"><summary>{text}</summary><div class="toggle-body">{ch}</div></details>')

        elif btype == "table":
            rows_html = ""
            for j, row in enumerate(b.get("_children", [])):
                row_data = row.get("table_row", {})
                cells = row_data.get("cells", [])
                tag   = "th" if j == 0 else "td"
                row_html = "".join(f"<{tag}>{rich_text_to_html(c)}</{tag}>" for c in cells)
                rows_html += f"<tr>{row_html}</tr>"
            html.append(f'<div class="notion-table-wrap"><table class="notion-table">{rows_html}</table></div>')

        elif btype == "column_list":
            children = b.get("_children", [])
            num_cols = len(children)
            cols = "".join(f'<div class="notion-col">{blocks_to_html(c.get("_children",[]))}</div>'
                           for c in children)
            style = f'grid-template-columns:repeat({num_cols},1fr)' if num_cols else ''
            html.append(f'<div class="notion-cols" style="{style}">{cols}</div>')

        elif btype == "synced_block":
            # Render children of synced blocks (they act as transparent containers)
            if ch:
                html.append(ch)

        elif btype == "child_page":
            title = data.get("title", "")
            child_blocks = fetch_blocks(b["id"])
            child_html = blocks_to_html(child_blocks)
            if child_html:
                html.append(f'<div class="notion-child-page-inline"><h3 class="notion-h2">📄 {escape(title)}</h3>{child_html}</div>')
            else:
                html.append(f'<div class="notion-child-page">📄 {escape(title)}</div>')

        elif btype == "child_database":
            title = data.get("title", "")
            html.append(fetch_database_as_table(b["id"], title))

        i += 1

    return "\n".join(html)


# ── Google Form entry ID discovery ───────────────────────────────────────────

def discover_form_entry_ids(form_url: str) -> dict:
    """
    Fetch the Google Form page source and extract entry IDs from
    the FB_PUBLIC_LOAD_DATA_ JavaScript variable.
    Returns dict mapping field labels → entry IDs.
    """
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
    r = requests.get(form_url, headers=headers)
    r.raise_for_status()

    # FB_PUBLIC_LOAD_DATA_ is a JSON-like array embedded in the page JS
    match = re.search(r'FB_PUBLIC_LOAD_DATA_\s*=\s*(\[.*?\]);\s*</script>', r.text, re.DOTALL)
    if not match:
        print("⚠️  Could not auto-discover form entry IDs. Using placeholders.")
        return {}

    try:
        raw = json.loads(match.group(1))
        fields = raw[1][1]  # list of field definitions
        result = {}
        for field in fields:
            label    = field[1] if len(field) > 1 else ""
            entry_id = field[4][0][0] if len(field) > 4 and field[4] else None
            if label and entry_id:
                result[label] = f"entry.{entry_id}"
        return result
    except Exception as e:
        print(f"⚠️  Error parsing form data: {e}. Using placeholders.")
        return {}


# ── Logo ─────────────────────────────────────────────────────────────────────

def find_logo() -> Path | None:
    """Return the Path to the logo file, or None if not found."""
    if os.path.exists(LOGO_PATH):
        return Path(LOGO_PATH)
    script_dir = Path(__file__).parent
    for ext in ("png", "jpg", "jpeg"):
        for name in ("logo", "malleus"):
            p = script_dir / f"{name}.{ext}"
            if p.exists():
                return p
    return None


# ── Shared CSS / HTML fragments ───────────────────────────────────────────────

BASE_JS = """
function toggleMobileNav() {
  var n = document.getElementById('nav-links');
  if (n) n.classList.toggle('mobile-open');
}
document.addEventListener('DOMContentLoaded', function() {
  document.querySelectorAll('.nav-links a').forEach(function(a) {
    a.addEventListener('click', function() {
      var n = document.getElementById('nav-links');
      if (n) n.classList.remove('mobile-open');
    });
  });
});
"""

SHARED_CSS = """
*, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
:root {
  --bg: #F0F4F8; --surface: #FFFFFF;
  --ink: #1A2B3C; --ink-muted: #4A6080; --ink-faint: #96AABF;
  --accent: #2E6DA4; --accent-light: #D4E5F5; --accent-mid: #7AAFD4;
  --accent-dark: #1B4E7A; --border: #C8DBE8; --border-light: #DDE8F0;
  --radius: 4px;
}
html { scroll-behavior: smooth; }
body { font-family: "DM Sans", sans-serif; background: var(--bg); color: var(--ink);
  line-height: 1.6; font-size: 16px; -webkit-font-smoothing: antialiased; }

/* NAV */
nav { position: fixed; top:0; left:0; right:0; z-index:100;
  background: rgba(240,244,248,0.9); backdrop-filter: blur(12px);
  -webkit-backdrop-filter: blur(12px); border-bottom: 1px solid var(--border);
  padding: 0 2rem; height: 62px; display:flex; align-items:center;
  justify-content:space-between; }
.nav-logo { display:flex; align-items:center; gap:0.6rem; text-decoration:none; color:var(--ink); }
.nav-logo img { width:32px; height:32px; border-radius:50%; object-fit:cover; }
.nav-logo-text { font-family:"Lora",serif; font-weight:600; font-size:1rem; }
.nav-links { display:flex; align-items:center; gap:1.75rem; list-style:none; }
.nav-links a { text-decoration:none; color:var(--ink-muted); font-size:0.875rem;
  transition:color 0.2s; }
.nav-links a:hover, .nav-links a.active { color:var(--accent); }
.nav-cta { background:var(--accent) !important; color:white !important;
  padding:0.42rem 1rem; border-radius:var(--radius); font-weight:500 !important; }
.nav-cta:hover { background:var(--accent-dark) !important; }
.nav-hamburger { display:none; background:none; border:none; cursor:pointer; color:var(--ink); padding:0.4rem; font-size:1.4rem; line-height:1; }
@media (max-width:700px) {
  .nav-hamburger { display:flex; align-items:center; }
  .nav-links { display:none; position:absolute; top:62px; left:0; right:0; flex-direction:column; background:rgba(240,244,248,0.97); backdrop-filter:blur(12px); -webkit-backdrop-filter:blur(12px); border-bottom:1px solid var(--border); padding:0.25rem 1.5rem 0.75rem; gap:0; z-index:99; }
  .nav-links.mobile-open { display:flex; }
  .nav-links li { border-bottom:1px solid var(--border-light); }
  .nav-links li:last-child { border-bottom:none; }
  .nav-links a { padding:0.8rem 0; font-size:0.95rem; display:block; }
}

/* PAGE SHELL */
.page-body { padding-top: 62px; }
.page-header { background: var(--surface); border-bottom: 1px solid var(--border);
  padding: 4rem 2rem 3rem; }
.page-header-inner { max-width: 780px; margin: 0 auto; }
.page-eyebrow { font-size:0.72rem; font-weight:500; letter-spacing:0.1em;
  text-transform:uppercase; color:var(--accent); margin-bottom:0.6rem; }
.page-title { font-family:"Lora",serif; font-size:clamp(1.9rem,4vw,2.7rem);
  font-weight:600; letter-spacing:-0.02em; color:var(--ink); line-height:1.2;
  margin-bottom:0.6rem; }
.page-subtitle { font-size:1rem; color:var(--ink-muted); font-weight:300;
  line-height:1.7; max-width:540px; }
.page-content { max-width:1060px; margin:0 auto; padding:3rem 2rem 5rem; }

/* NOTION CONTENT */
.notion-content { font-size:0.97rem; line-height:1.75; }
.notion-content p { margin-bottom:1rem; color:var(--ink-muted); }
.notion-content h2.notion-h1 { font-family:"Lora",serif; font-size:1.7rem;
  font-weight:600; color:var(--ink); margin:2.2rem 0 0.8rem; letter-spacing:-0.01em;
  scroll-margin-top:82px; }
.notion-content h3.notion-h2 { font-family:"Lora",serif; font-size:1.3rem;
  font-weight:600; color:var(--ink); margin:1.8rem 0 0.6rem; scroll-margin-top:82px; }
.notion-content h4.notion-h3 { font-size:1.05rem; font-weight:600;
  color:var(--ink); margin:1.5rem 0 0.5rem; scroll-margin-top:82px; }
.notion-toggle summary { scroll-margin-top:82px; }
.notion-ul, .notion-ol { padding-left:1.5rem; margin-bottom:1rem;
  color:var(--ink-muted); }
.notion-ul li, .notion-ol li { margin-bottom:0.35rem; }
.notion-ul li .notion-ul, .notion-ul li .notion-ol { margin:0.3rem 0 0; }
.notion-todo-list { list-style:none; padding-left:0; margin-bottom:1rem; }
.notion-todo { display:flex; align-items:flex-start; gap:0.5rem; flex-wrap:wrap;
  margin-bottom:0.35rem; color:var(--ink-muted); }
.notion-todo input { accent-color:var(--accent); flex-shrink:0; margin-top:0.2em; }
.notion-todo-text { flex:1; min-width:0; }
.notion-todo > .notion-toggle, .notion-todo > .notion-ul, .notion-todo > .notion-ol,
.notion-todo > p, .notion-todo > .notion-callout { width:100%; margin-left:1.5rem; margin-top:0.3rem; }
.notion-callout { display:flex; gap:0.9rem; padding:1rem 1.25rem;
  border-radius:8px; margin:1.25rem 0; border:1px solid var(--border); }
.notion-callout-default, .notion-callout-gray { background:#F5F7FA; }
.notion-callout-blue  { background:var(--accent-light); border-color:var(--accent-mid); }
.notion-callout-yellow { background:#FEF9E7; border-color:#F1C40F; }
.notion-callout-red   { background:#FDEDEC; border-color:#E74C3C; }
.notion-callout-green { background:#EAFAF1; border-color:#27AE60; }
.callout-icon { font-size:1.2rem; flex-shrink:0; line-height:1.5; }
.notion-code { background:#1E2B3C; color:#E8EFF5; padding:1.2rem 1.5rem;
  border-radius:8px; overflow-x:auto; font-size:0.83rem; line-height:1.6;
  margin:1.25rem 0; }
.notion-code code { font-family:"Fira Code","Consolas",monospace; }
code { background:#E8EFF5; color:var(--accent-dark); padding:0.1em 0.35em;
  border-radius:3px; font-size:0.88em; font-family:"Fira Code","Consolas",monospace; }
.notion-divider { border:none; border-top:1px solid var(--border); margin:2rem 0; }
.notion-quote { border-left:3px solid var(--accent); padding:0.5rem 1.25rem;
  margin:1.25rem 0; color:var(--ink-muted); font-style:italic; }
.notion-image { margin:1.5rem 0; }
.notion-image img { max-width:100%; border-radius:8px; border:1px solid var(--border); }
.notion-image figcaption { font-size:0.8rem; color:var(--ink-faint); margin-top:0.4rem; }
.notion-toggle { border:1px solid var(--border); border-radius:6px;
  padding:0.75rem 1rem; margin:0.5rem 0; }
.notion-toggle summary { cursor:pointer; font-weight:500; color:var(--ink); list-style:none; }
.notion-toggle summary::-webkit-details-marker { display:none; }
.notion-toggle summary::before { content:"▶ "; font-size:0.75em; color:var(--ink-faint); transition:transform 0.2s; display:inline-block; }
.notion-toggle[open] summary::before { content:"▼ "; }
.notion-toggle summary.notion-h1 { font-family:"Lora",serif; font-size:1.7rem; font-weight:600; letter-spacing:-0.01em; }
.notion-toggle summary.notion-h2 { font-family:"Lora",serif; font-size:1.3rem; font-weight:600; }
.notion-toggle summary.notion-h3 { font-size:1.05rem; font-weight:600; }
.toggle-body { padding:0.75rem 0 0; }
.notion-table-wrap { overflow-x:auto; margin:1.5rem 0; }
.notion-table { width:100%; border-collapse:collapse; font-size:0.9rem; }
.notion-table th, .notion-table td { border:1px solid var(--border);
  padding:0.6rem 1rem; text-align:left; }
.notion-table th { background:var(--bg); font-weight:600; }
.notion-cols { display:grid; gap:2rem; grid-template-columns:repeat(auto-fit,minmax(240px,1fr)); margin:1rem 0; }
@media (max-width:640px) { .notion-cols { grid-template-columns:1fr !important; } }
.notion-child-page { padding:0.5rem 0.75rem; background:var(--bg);
  border:1px solid var(--border); border-radius:6px; margin:0.5rem 0;
  font-size:0.9rem; color:var(--ink-muted); }
.notion-child-page-inline { margin:1.5rem 0; }

/* TOC — fixed hover panel on the left */
.toc-panel { position:fixed; left:0; top:110px; z-index:50; display:flex; align-items:flex-start; }
.toc-tab { width:32px; min-height:140px; padding:1rem 0; background:var(--surface); border:1px solid var(--border); border-left:none; border-radius:0 10px 10px 0; display:flex; flex-direction:column; align-items:center; justify-content:center; gap:0.6rem; flex-shrink:0; cursor:default; transition:background 0.2s, border-color 0.2s, border-radius 0.3s; box-shadow:2px 0 8px rgba(46,109,164,0.07); }
.toc-panel:hover .toc-tab { background:var(--bg); border-color:var(--accent-mid); border-radius:0; }
.toc-tab-icon { font-size:1rem; color:var(--accent); line-height:1; }
.toc-tab-label { writing-mode:vertical-rl; font-size:0.58rem; font-weight:600; letter-spacing:0.14em; text-transform:uppercase; color:var(--ink-faint); transform:rotate(180deg); white-space:nowrap; }
.toc { position:static; width:0; min-width:0; height:0; overflow:hidden; opacity:0; transition:width 0.3s cubic-bezier(0.4,0,0.2,1), opacity 0.2s; background:var(--surface); border-top:1px solid var(--border); border-right:1px solid var(--border); border-bottom:1px solid var(--border); border-radius:0 10px 10px 0; box-shadow:4px 0 20px rgba(46,109,164,0.08); }
.toc-inner { width:256px; padding:1.5rem 1.25rem; height:100%; overflow-y:auto; scrollbar-width:thin; scrollbar-color:var(--border) transparent; }
.toc-inner::-webkit-scrollbar { width:4px; }
.toc-inner::-webkit-scrollbar-track { background:transparent; }
.toc-inner::-webkit-scrollbar-thumb { background:var(--border); border-radius:2px; }
.toc-header { border-bottom:1px solid var(--border-light); padding-bottom:0.6rem; margin-bottom:1rem; }
.toc-title { font-weight:600; font-size:0.65rem; letter-spacing:0.12em; text-transform:uppercase; color:var(--accent); }
.toc ul { list-style:none; padding:0; margin:0; }
.toc li { line-height:1.4; }
.toc li.toc-h1 { margin-bottom:0.4rem; }
.toc li.toc-h2 { margin-bottom:0.25rem; }
.toc a { color:var(--ink-muted); text-decoration:none; display:block; font-size:0.85rem; padding:0.3rem 0.5rem; border-radius:4px; border-left:2px solid transparent; transition:color 0.15s, background 0.15s, border-color 0.15s; }
.toc a:hover { color:var(--accent); background:var(--accent-light); border-left-color:var(--accent); }
.toc li.toc-h2 > a { padding-left:1.25rem; font-size:0.8rem; color:var(--ink-faint); }
.toc li.toc-h2 > a:hover { color:var(--accent); background:var(--accent-light); border-left-color:var(--accent-mid); }
@media (max-width:900px) { .toc-panel { display:none; } }
a { color:var(--accent); }

/* FORM */
.form-wrap { background:var(--surface); border:1px solid var(--border);
  border-radius:12px; padding:2.5rem; max-width:640px; }
.form-group { margin-bottom:1.5rem; }
.form-label { display:block; font-size:0.87rem; font-weight:500;
  color:var(--ink); margin-bottom:0.4rem; }
.form-label .req { color:var(--accent); margin-left:0.15rem; }
.form-input, .form-select { width:100%; padding:0.65rem 0.9rem;
  border:1px solid var(--border); border-radius:var(--radius);
  background:var(--bg); font-family:inherit; font-size:0.93rem;
  color:var(--ink); transition:border-color 0.2s, box-shadow 0.2s;
  -webkit-appearance:none; }
.form-input:focus, .form-select:focus { outline:none;
  border-color:var(--accent); box-shadow:0 0 0 3px rgba(46,109,164,0.12); }
.radio-group { display:flex; flex-direction:column; gap:0.55rem; margin-top:0.2rem; }
.radio-item { display:flex; align-items:center; gap:0.6rem;
  cursor:pointer; font-size:0.9rem; color:var(--ink-muted); }
.radio-item input[type=radio] { accent-color:var(--accent); width:15px; height:15px;
  flex-shrink:0; cursor:pointer; }
.radio-item:hover { color:var(--ink); }
.other-input { margin-top:0.5rem; margin-left:1.65rem; width:calc(100% - 1.65rem); }
.consent-group { display:flex; flex-direction:column; gap:0.5rem; }
.checkbox-item { display:flex; align-items:center; gap:0.6rem;
  font-size:0.9rem; color:var(--ink-muted); cursor:pointer; }
.checkbox-item input { accent-color:var(--accent); width:15px; height:15px; cursor:pointer; }
.form-hint { font-size:0.78rem; color:var(--ink-faint); margin-top:0.3rem; line-height:1.5; }
.btn-submit { display:inline-flex; align-items:center; gap:0.4rem;
  background:var(--accent); color:white; border:none; cursor:pointer;
  padding:0.75rem 2rem; border-radius:var(--radius); font-family:inherit;
  font-size:0.95rem; font-weight:500; transition:background 0.2s, transform 0.15s; }
.btn-submit:hover { background:var(--accent-dark); transform:translateY(-1px); }
.btn-submit:disabled { opacity:0.6; cursor:not-allowed; transform:none; }
.form-success { display:none; background:#EAFAF1; border:1px solid #27AE60;
  border-radius:8px; padding:1.5rem; text-align:center; }
.form-success h3 { font-family:"Lora",serif; color:#1A6B35; margin-bottom:0.4rem; }
.form-success p  { color:#2D7A47; font-size:0.9rem; }
.form-error { display:none; background:#FDEDEC; border:1px solid #E74C3C;
  border-radius:8px; padding:1rem 1.25rem; margin-bottom:1rem; font-size:0.87rem;
  color:#922B21; }

/* FOOTER */
footer { padding:2rem; border-top:1px solid var(--border); background:var(--surface); }
.footer-inner { max-width:1100px; margin:0 auto; display:flex;
  align-items:center; justify-content:space-between; gap:1rem; flex-wrap:wrap; }
.footer-logo { display:flex; align-items:center; gap:0.55rem;
  text-decoration:none; color:var(--ink); }
.footer-logo img { width:24px; height:24px; border-radius:50%; object-fit:cover; }
.footer-name { font-family:"Lora",serif; font-weight:600; font-size:0.9rem; }
.footer-tagline { font-size:0.77rem; color:var(--ink-faint); margin-left:0.5rem; }
.footer-links { display:flex; gap:1.4rem; }
.footer-links a { font-size:0.79rem; color:var(--ink-muted); text-decoration:none; }
.footer-links a:hover { color:var(--accent); }
"""

FONTS = '<link href="https://fonts.googleapis.com/css2?family=Lora:ital,wght@0,400;0,600;1,400&family=DM+Sans:wght@300;400;500&display=swap" rel="stylesheet">'

FORM_JS = """
function setFieldError(el, msg) {
  el.style.borderColor = '#E74C3C';
  var hint = el.parentNode.querySelector('.field-error-msg');
  if (!hint) {
    hint = document.createElement('p');
    hint.className = 'field-error-msg';
    hint.style.cssText = 'color:#E74C3C;font-size:0.8rem;margin-top:0.3rem;';
    el.parentNode.appendChild(hint);
  }
  hint.textContent = msg;
}
function clearFieldError(el) {
  el.style.borderColor = '';
  var hint = el.parentNode.querySelector('.field-error-msg');
  if (hint) hint.remove();
}

function submitMalleusForm(e) {
  e.preventDefault();
  const form    = document.getElementById('malleus-form');
  const btn     = document.getElementById('submit-btn');
  const success = document.getElementById('form-success');
  const error   = document.getElementById('form-error');

  // ── Validate all required fields ────────────────────────────────────────
  var valid = true;

  // Text / email inputs with [required]
  form.querySelectorAll('input[required]').forEach(function(inp) {
    if (inp.type === 'radio') return; // handled separately
    clearFieldError(inp);
    if (!inp.value.trim()) {
      setFieldError(inp, 'This field is required.');
      if (valid) inp.focus();
      valid = false;
    } else if (inp.type === 'email' && !/^[^@\\s]+@[^@\\s]+\\.[^@\\s]+$/.test(inp.value.trim())) {
      setFieldError(inp, 'Please enter a valid email address.');
      if (valid) inp.focus();
      valid = false;
    } else {
      clearFieldError(inp);
    }
  });

  // Radio groups with [required] — check at least one is selected
  var radioGroups = {};
  form.querySelectorAll('input[type="radio"][required]').forEach(function(r) {
    radioGroups[r.name] = radioGroups[r.name] || r;
  });
  Object.keys(radioGroups).forEach(function(name) {
    var checked = form.querySelector('input[name="' + name + '"]:checked');
    var firstRadio = radioGroups[name];
    var group = firstRadio.closest('.radio-group') || firstRadio.parentNode;
    var hint = group.querySelector('.field-error-msg');
    if (!checked) {
      if (!hint) {
        hint = document.createElement('p');
        hint.className = 'field-error-msg';
        hint.style.cssText = 'color:#E74C3C;font-size:0.8rem;margin-top:0.3rem;';
        group.appendChild(hint);
      }
      hint.textContent = 'Please select an option.';
      valid = false;
    } else {
      if (hint) hint.remove();
    }
  });

  // "Other" text field — required if Other radio is selected
  const otherRadio = form.querySelector('input[value="__other_option__"]:checked');
  const otherInput = form.querySelector('#other-text input');
  if (otherInput) {
    clearFieldError(otherInput);
    if (otherRadio && !otherInput.value.trim()) {
      setFieldError(otherInput, 'Please specify your status.');
      if (valid) otherInput.focus();
      valid = false;
    }
  }

  if (!valid) return;

  // ── Submit ───────────────────────────────────────────────────────────────
  btn.disabled    = true;
  btn.textContent = 'Submitting…';
  error.style.display = 'none';

  const data   = new FormData(form);
  const params = new URLSearchParams(data);

  // Google Forms no-cors POST: response is always opaque — treat network success as form success.
  fetch('FORM_POST_URL', { method:'POST', mode:'no-cors', body: params })
    .then(() => {
      form.style.display    = 'none';
      success.style.display = 'block';
    })
    .catch(() => {
      error.textContent   = 'Submission failed — please check your connection and try again.';
      error.style.display = 'block';
      btn.disabled        = false;
      btn.textContent     = 'Submit';
    });
}

// Toggle "Other" text field visibility
document.addEventListener('DOMContentLoaded', () => {
  const radios = document.querySelectorAll('input[type="radio"]');
  const other  = document.getElementById('other-text');
  radios.forEach(r => r.addEventListener('change', () => {
    if (other) other.style.display = r.value === '__other_option__' && r.checked ? 'block' : 'none';
  }));
});
"""


# ── Template helpers ──────────────────────────────────────────────────────────

def nav_html(logo_name: str, active: str = "") -> str:
    pages = [
        ("index.html",                "Home"),
        ("getting-started.html",      "Getting Started"),
        ("submission-guidelines.html","Submission Guidelines"),
        ("checklist.html",            "Checklist"),
        ("register.html",             "Register"),
    ]
    items = []
    for href, label in pages:
        cls = ' class="active"' if label.lower().replace(" ","-") == active else ""
        items.append(f'<li><a href="{href}"{cls}>{label}</a></li>')
    img = f'<img src="{logo_name}" alt="Malleus">' if logo_name else ""
    return f"""
<nav>
  <a class="nav-logo" href="index.html">
    {img}<span class="nav-logo-text">Malleus</span>
  </a>
  <button class="nav-hamburger" onclick="toggleMobileNav()" aria-label="Menu">&#9776;</button>
  <ul class="nav-links" id="nav-links">
    {"".join(items)}
  </ul>
</nav>"""


def footer_html(logo_name: str) -> str:
    img = f'<img src="{logo_name}" alt="Malleus">' if logo_name else ""
    return f"""
<footer>
  <div class="footer-inner">
    <div style="display:flex;align-items:center;gap:0.4rem;flex-wrap:wrap;">
      <a class="footer-logo" href="index.html">{img}<span class="footer-name">Malleus</span></a>
      <span class="footer-tagline">· Clinical Medicine · AU/NZ · Open Source · Not for Profit</span>
    </div>
    <div class="footer-links">
      <a href="https://malleuscm.notion.site/" target="_blank">Notion</a>
      <a href="https://discord.gg/4WqgJzjVyH" target="_blank">Discord</a>
      <a href="https://www.facebook.com/MalleusCM" target="_blank">Facebook</a>
      <a href="https://www.paypal.com/donate/?hosted_button_id=N5G46YHELZJ6C" target="_blank">Donate</a>
      <a href="mailto:president@malleus.org.au">Contact</a>
    </div>
  </div>
</footer>"""


def page_shell(title: str, logo_name: str, active: str, body: str, extra_js: str = "") -> str:
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>{title} — Malleus Clinical Medicine</title>
  {FONTS}
  <style>{SHARED_CSS}</style>
</head>
<body>
{nav_html(logo_name, active)}
<div class="page-body">
{body}
</div>
{footer_html(logo_name)}
<script>{BASE_JS}</script>
{f"<script>{extra_js}</script>" if extra_js else ""}
</body>
</html>"""


# ── Page builders ─────────────────────────────────────────────────────────────

def build_notion_page(
    title: str, eyebrow: str, subtitle: str,
    page_id: str, active: str, logo_name: str,
    with_toc: bool = True
) -> str:
    print(f"  Fetching Notion blocks for: {title}…")
    blocks = fetch_blocks(page_id)
    content_html = blocks_to_html(blocks)

    if with_toc:
        content_html, toc_html = add_ids_and_build_toc(content_html)
    else:
        toc_html = ""

    if toc_html:
        content_inner = f'{toc_html}<div class="notion-content">{content_html}</div>'
    else:
        content_inner = f'<div class="notion-content">{content_html}</div>'

    toc_js = ("""
var _tocPanel = document.querySelector('.toc-panel');
if (_tocPanel) {
  var _tocNav = _tocPanel.querySelector('.toc');
  _tocPanel.addEventListener('mouseenter', function() { _tocNav.style.height = (window.innerHeight - 125) + 'px'; _tocNav.style.width = '256px'; _tocNav.style.opacity = '1'; });
  _tocPanel.addEventListener('mouseleave', function() { _tocNav.style.width = '0px'; _tocNav.style.height = '0px'; _tocNav.style.opacity = '0'; });
}
""" if toc_html else "")

    body = f"""
<div class="page-header">
  <div class="page-header-inner">
    <div class="page-eyebrow">{eyebrow}</div>
    <h1 class="page-title">{title}</h1>
    <p class="page-subtitle">{subtitle}</p>
  </div>
</div>
<div class="page-content">
  {content_inner}
</div>"""
    return page_shell(title, logo_name, active, body, toc_js)


def build_register_page(form_ids: dict, logo_name: str) -> str:
    # Map field labels to entry IDs (with fallbacks)
    # Common fallback IDs — replace these with real ones from discover_form_entry_ids()
    EMAIL    = "emailAddress"  # Google Forms built-in email collection field
    FNAME    = form_ids.get("What is your first name?",     "entry.FNAME_ID")
    LNAME    = form_ids.get("What is your last name?",      "entry.LNAME_ID")
    ANKIHUB  = form_ids.get("AnkiHub username (optional)",  "entry.ANKIHUB_ID")
    STATUS   = form_ids.get("What is your current school/uni/work status?", "entry.STATUS_ID")
    CONSENT  = form_ids.get("Do you consent to opt-in for email updates on the status of the project?", "entry.CONSENT_ID")

    status_options = [
        "High school student",
        "Pre-clinical year medical student",
        "Clinical year medical student",
        "University student (non-Medicine)",
        "Prevocational postgraduate medical officer/JMO",
        "Registrar in training/Advanced trainee",
        "Consultant/Fellow",
        "IMG studying for AMC Part 1/2",
    ]
    radio_items = "\n".join(
        f'<label class="radio-item"><input type="radio" name="{STATUS}" value="{o}" required> {o}</label>'
        for o in status_options
    )
    # "Other" radio
    radio_items += f"""
<label class="radio-item">
  <input type="radio" name="{STATUS}" value="__other_option__"> Other:
</label>
<div id="other-text" style="display:none;">
  <input class="form-input other-input" type="text" name="{STATUS}.other_option_response"
    placeholder="Please specify…" aria-label="Other status">
</div>"""

    js = FORM_JS.replace("FORM_POST_URL", FORM_POST)

    body = f"""
<div class="page-header">
  <div class="page-header-inner">
    <div class="page-eyebrow">Community</div>
    <h1 class="page-title">Register as a Member</h1>
    <p class="page-subtitle">
      Join Malleus as a general member to receive community updates, gain voting rights
      at AGMs, and be eligible to nominate for committee positions.
    </p>
  </div>
</div>
<div class="page-content">
  <div class="form-error" id="form-error"></div>

  <form class="form-wrap" id="malleus-form" onsubmit="submitMalleusForm(event)" novalidate>

    <div class="form-group">
      <label class="form-label" for="field-email">Email<span class="req">*</span></label>
      <input class="form-input" id="field-email" type="email" name="{EMAIL}"
        placeholder="you@example.com" required autocomplete="email">
    </div>

    <div class="form-group">
      <label class="form-label" for="field-fname">First Name<span class="req">*</span></label>
      <input class="form-input" id="field-fname" type="text" name="{FNAME}"
        placeholder="Jane" required autocomplete="given-name">
    </div>

    <div class="form-group">
      <label class="form-label" for="field-lname">Last Name<span class="req">*</span></label>
      <input class="form-input" id="field-lname" type="text" name="{LNAME}"
        placeholder="Smith" required autocomplete="family-name">
    </div>

    <div class="form-group">
      <label class="form-label" for="field-ankihub">AnkiHub Username
        <span style="font-weight:300;color:var(--ink-faint)">(optional)</span>
      </label>
      <input class="form-input" id="field-ankihub" type="text" name="{ANKIHUB}"
        placeholder="your_ankihub_username" autocomplete="username">
      <p class="form-hint">Your AnkiHub username links your membership to your deck subscription.</p>
    </div>

    <div class="form-group">
      <label class="form-label">Current School / Uni / Work Status<span class="req">*</span></label>
      <div class="radio-group">
        {radio_items}
      </div>
    </div>

    <div class="form-group">
      <label class="form-label">Email Updates<span class="req">*</span></label>
      <div class="consent-group">
        <label class="checkbox-item">
          <input type="radio" name="{CONSENT}" value="Yes" required> Yes, I consent to opt-in for email updates
        </label>
        <label class="checkbox-item">
          <input type="radio" name="{CONSENT}" value="No" required> No
        </label>
      </div>
      <p class="form-hint">
        Your name and email will be stored securely and used only for Malleus community updates.
        You can opt out at any time by emailing
        <a href="mailto:admin@malleus.org.au">admin@malleus.org.au</a>.
      </p>
    </div>

    <button class="btn-submit" id="submit-btn" type="submit">Submit →</button>
  </form>

  <div class="form-success" id="form-success">
    <h3>🎉 Registration received!</h3>
    <p>Thanks for joining Malleus. You'll receive a <strong>your.name@malleus.org.au</strong>
    email address within 14 days. Keep an eye on your inbox.</p>
  </div>
</div>"""

    return page_shell("Register as a Member", logo_name, "register", body, js)


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    DIST_DIR.mkdir(exist_ok=True)

    print("📖  Copying static assets…")
    logo_src = find_logo()
    if logo_src:
        logo_name = logo_src.name
        shutil.copy(logo_src, DIST_DIR / logo_name)
        print(f"  ✅  {logo_name} copied to dist/")
    else:
        logo_name = ""
        print("  ⚠️  No logo file found. Place logo.png next to build.py.")
    for asset in ("addon.png", "phone-transparent.png", "anki-screenshot.jpg"):
        src = Path(asset)
        if src.exists():
            shutil.copy(src, DIST_DIR / src.name)
            print(f"  ✅  {src.name} copied to dist/")

    print("🔑  Discovering Google Form entry IDs…")
    form_ids = discover_form_entry_ids(FORM_URL)
    if form_ids:
        print(f"  ✅  Found {len(form_ids)} fields: {list(form_ids.keys())}")
    else:
        print("  ⚠️  Using placeholder IDs — the form will need real IDs to submit.")

    print("📄  Generating Getting Started page…")
    html = build_notion_page(
        "Getting Started", "How to use Malleus",
        "Install the deck, set up AnkiHub, and start reviewing clinical medicine flashcards today.",
        NOTION_PAGES["getting-started"], "getting-started", logo_name
    )
    (DIST_DIR / "getting-started.html").write_text(html, encoding="utf-8")

    print("📄  Generating Submission Guidelines page…")
    html = build_notion_page(
        "Submission Guidelines", "Contributing to Malleus",
        "Everything you need to know about formatting, tagging, and quality standards for new cards.",
        NOTION_PAGES["submission-guidelines"], "submission-guidelines", logo_name
    )
    (DIST_DIR / "submission-guidelines.html").write_text(html, encoding="utf-8")

    print("📄  Generating Card Submission Checklist page…")
    html = build_notion_page(
        "Card Submission Checklist", "Before you submit",
        "Run through this checklist before submitting a card to make sure it meets Malleus standards.",
        NOTION_PAGES["checklist"], "checklist", logo_name
    )
    (DIST_DIR / "checklist.html").write_text(html, encoding="utf-8")

    print("📄  Copying register.html…")
    if Path("register.html").exists():
        shutil.copy("register.html", DIST_DIR / "register.html")
        print("  ✅  register.html copied to dist/")
    else:
        print("  ⚠️  register.html not found next to build.py — skipping.")

    print("📄  Copying index.html…")
    if Path("index.html").exists():
        shutil.copy("index.html", DIST_DIR / "index.html")
        print("  ✅  index.html copied to dist/")
    else:
        print("  ⚠️  index.html not found next to build.py — skipping.")

    print(f"\n✅  Done!  All files written to ./{DIST_DIR}/")
    print("   Host the entire dist/ folder on GitHub Pages, Netlify, or any static host.")
    if not form_ids:
        print("""
⚠️  Google Form entry IDs could not be auto-discovered (Google blocks server-side scraping).
   To get the real IDs:
     1. Open the form in Chrome: https://docs.google.com/forms/d/e/1FAIpQLSd6G7DAgEeKjS-sXuX-Mvzfo5BGWaEpRZ9n3Sf2e4E1be7kXw/viewform
     2. Right-click → View Page Source  (Ctrl+U / Cmd+U)
     3. Press Ctrl+F and search for: FB_PUBLIC_LOAD_DATA_
     4. The entry IDs appear as large numbers inside that JS variable, e.g. 123456789
     5. Grep the source for the pattern: entry\\.\\d+
     6. Replace the placeholder strings in dist/register.html:
        ENTRY.EMAIL_ID, ENTRY.FNAME_ID, etc.
   Or run build.py from a machine without a firewall — it auto-discovers them.
""")


if __name__ == "__main__":
    main()
