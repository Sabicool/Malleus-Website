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
    "terms-of-use":            "3785964e68a480fca4dbcb245ecda795",
}

# Databases queried at build time (must be shared with the Notion integration)
JOBS_DB_ID     = "3405964e68a4807db351f96c76e93bf7"   # Project Malleus Positions
CONTACTS_DB_ID = "2d75964e68a4810996cbed680205b5a5"   # Committee contacts (About page)

NOTION_HEADERS = {
    "Authorization":  f"Bearer {NOTION_TOKEN}",
    "Notion-Version": "2022-06-28",
    "Content-Type":   "application/json",
}

SITE_URL   = "https://sabicool.github.io/Malleus-Website/"
ASSETS_DIR = DIST_DIR / "assets"


def localise_image(url: str, block_id: str) -> str:
    """Download a Notion-hosted image into dist/assets/ and return its relative
    path. Notion file URLs are presigned S3 links that expire after one hour,
    so they must not be embedded in the generated HTML directly. Non-expiring
    (external) URLs are returned unchanged; on download failure the original
    URL is kept so the build never breaks."""
    if "amazonaws.com" not in url and "X-Amz-" not in url:
        return url
    from urllib.parse import urlparse
    ext = os.path.splitext(urlparse(url).path)[1].lower()
    if not ext or len(ext) > 5:
        ext = ".png"
    fname = f"{block_id.replace('-', '')}{ext}"
    dest  = ASSETS_DIR / fname
    if not dest.exists():
        try:
            ASSETS_DIR.mkdir(parents=True, exist_ok=True)
            r = requests.get(url, timeout=30)
            r.raise_for_status()
            dest.write_bytes(r.content)
            print(f"    🖼️  Downloaded image → assets/{fname}")
        except Exception as e:
            print(f"    ⚠️  Image download failed for block {block_id}: {e}")
            return url
    return f"assets/{fname}"

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
            json={"page_size": 100}
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


def fetch_db_rows(db_id: str) -> list:
    """Query all rows of a Notion database. Returns [] on any failure so the
    build never breaks on a missing or unshared database."""
    rows, cursor = [], None
    try:
        while True:
            payload = {"page_size": 100}
            if cursor:
                payload["start_cursor"] = cursor
            r = requests.post(f"https://api.notion.com/v1/databases/{db_id}/query",
                              headers=NOTION_HEADERS, json=payload)
            if r.status_code != 200:
                print(f"    ⚠️  Database {db_id} query failed ({r.status_code}) — is it shared with the integration?")
                return rows
            data = r.json()
            rows += data.get("results", [])
            if not data.get("has_more"):
                break
            cursor = data.get("next_cursor")
    except Exception as e:
        print(f"    ⚠️  Database {db_id} query error: {e}")
    return rows


def _prop(row: dict, name: str, kind: str):
    """Extract a plain value from a Notion page property."""
    v = row.get("properties", {}).get(name, {}).get(kind)
    if kind in ("title", "rich_text"):
        return "".join(rt.get("plain_text", "") for rt in (v or []))
    if kind == "select":
        return (v or {}).get("name", "")
    if kind == "multi_select":
        return [o.get("name", "") for o in (v or [])]
    if kind == "date":
        return (v or {}).get("start", "")
    return v or ""


def _fmt_date(iso: str) -> str:
    """ISO date → '26 Apr 2026' (empty-safe)."""
    if not iso:
        return ""
    try:
        from datetime import date
        d = date.fromisoformat(iso[:10])
        return f"{d.day} {d.strftime('%b %Y')}"
    except Exception:
        return iso


def _slugify(text: str) -> str:
    import re as _re
    from html import unescape as _unescape
    text = _re.sub(r'<[^>]+>', '', text)
    text = _unescape(text)  # &#x27; etc. would otherwise leak "x27" into the slug
    text = _re.sub(r'[^\w\s-]', '', text.lower())
    return _re.sub(r'[\s_-]+', '-', text).strip('-')


def _youtube_embed(url: str) -> str:
    """Return the YouTube embed URL for a watch/share link, or '' if not YouTube."""
    m = re.search(r'(?:youtu\.be/|youtube\.com/(?:watch\?v=|embed/|shorts/|live/))([\w-]{6,})', url)
    return f"https://www.youtube.com/embed/{m.group(1)}" if m else ""


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
        '<button type="button" class="toc-tab" aria-expanded="false" aria-label="Table of contents">'
        '<span class="toc-tab-icon">&#8801;</span>'
        '<span class="toc-tab-label">Contents</span>'
        '</button>'
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
                items.append(f'<li class="notion-todo"><input type="checkbox" {ck}><span class="notion-todo-text">{text}</span>{ch}</li>')
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
            if url:
                url = localise_image(url, b.get("id", "img"))
            cap  = rich_text_to_html(data.get("caption", []))
            html.append(f'<figure class="notion-image"><img src="{escape(url)}" alt="{escape(cap)}" loading="lazy"><figcaption>{cap}</figcaption></figure>')

        elif btype == "video":
            vd  = data.get("external", {}) or data.get("file", {})
            url = vd.get("url", "")
            cap = rich_text_to_html(data.get("caption", []))
            yt  = _youtube_embed(url)
            if yt:
                cap_html = f"<figcaption>{cap}</figcaption>" if cap else ""
                html.append(
                    f'<figure class="notion-video-figure">'
                    f'<div class="notion-video"><iframe src="{escape(yt)}" title="Video" '
                    f'allow="accelerometer; clipboard-write; encrypted-media; gyroscope; '
                    f'picture-in-picture; web-share" referrerpolicy="strict-origin-when-cross-origin" '
                    f'allowfullscreen loading="lazy"></iframe></div>{cap_html}</figure>')
            elif url:
                local = localise_image(url, b.get("id", "video"))
                if local.startswith("assets/"):
                    html.append(f'<video class="notion-video-file" controls preload="metadata" src="{escape(local)}"></video>')
                else:
                    html.append(f'<p class="notion-bookmark">▶ <a href="{escape(url)}" target="_blank">Watch video</a></p>')

        elif btype == "file":
            fd   = data.get("external", {}) or data.get("file", {})
            url  = fd.get("url", "")
            name = data.get("name", "") or "Download file"
            cap  = rich_text_to_html(data.get("caption", []))
            if url:
                local = localise_image(url, b.get("id", "file"))
                cap_html = f' <span class="notion-file-caption">— {cap}</span>' if cap else ""
                html.append(f'<p class="notion-file">📎 <a href="{escape(local)}" download>{escape(name)}</a>{cap_html}</p>')

        elif btype in ("bookmark", "embed", "link_preview"):
            url = data.get("url", "")
            cap = rich_text_to_html(data.get("caption", []))
            if url:
                label = cap or escape(url)
                html.append(f'<p class="notion-bookmark">🔗 <a href="{escape(url)}" target="_blank">{label}</a></p>')

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

  // Click-to-copy for inline code (add-on codes, tags)
  if (navigator.clipboard) {
    document.querySelectorAll('.page-content code').forEach(function(el) {
      if (el.closest('pre')) return;
      el.classList.add('code-copy');
      el.title = 'Click to copy';
      el.addEventListener('click', function() {
        navigator.clipboard.writeText(el.textContent).then(function() {
          el.classList.add('copied');
          setTimeout(function() { el.classList.remove('copied'); }, 1300);
        });
      });
    });
  }

  // Tickable checklists, remembered per page in this browser
  var boxes = Array.prototype.slice.call(
    document.querySelectorAll('.notion-todo input[type=checkbox]'));
  if (boxes.length) {
    boxes.forEach(function(cb, i) {
      var key = 'malleus-todo:' + location.pathname + ':' + i;
      var saved = localStorage.getItem(key);
      if (saved !== null) cb.checked = saved === '1';
      cb.addEventListener('change', function() {
        localStorage.setItem(key, cb.checked ? '1' : '0');
      });
    });
    if (boxes.length > 3) {
      var reset = document.createElement('button');
      reset.type = 'button';
      reset.className = 'todo-reset';
      reset.textContent = '\\u21BA Reset all checkboxes';
      reset.addEventListener('click', function() {
        boxes.forEach(function(cb, i) {
          cb.checked = false;
          localStorage.removeItem('malleus-todo:' + location.pathname + ':' + i);
        });
      });
      var first = document.querySelector('.notion-todo-list');
      if (first) first.parentNode.insertBefore(reset, first);
    }
  }
});
"""

SHARED_CSS = """
*, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
:root {
  --bg: #F0F4F8; --surface: #FFFFFF;
  --ink: #1A2B3C; --ink-muted: #4A6080; --ink-faint: #96AABF;
  --accent: #2E6DA4; --accent-light: #D4E5F5; --accent-mid: #7AAFD4;
  --accent-dark: #1B4E7A; --border: #C8DBE8; --border-light: #DDE8F0;
  --radius-sm: 6px; --radius: 10px; --radius-lg: 18px; --radius-xl: 24px;
  --shadow-sm: 0 1px 3px rgba(27, 78, 122, 0.06);
  --shadow-md: 0 4px 20px rgba(27, 78, 122, 0.08);
  --shadow-lg: 0 12px 40px rgba(27, 78, 122, 0.1);
  --ease-out: cubic-bezier(0.16, 1, 0.3, 1);
}
html { scroll-behavior: smooth; }
body { font-family: "Outfit", sans-serif; background: var(--bg); color: var(--ink);
  line-height: 1.6; font-size: 16px; -webkit-font-smoothing: antialiased; }

/* GRAIN OVERLAY (matches homepage) */
body::after { content: ""; position: fixed; inset: 0; z-index: 9999;
  pointer-events: none; opacity: 0.025;
  background-image: url("data:image/svg+xml,%3Csvg viewBox='0 0 256 256' xmlns='http://www.w3.org/2000/svg'%3E%3Cfilter id='noise'%3E%3CfeTurbulence type='fractalNoise' baseFrequency='0.9' numOctaves='4' stitchTiles='stitch'/%3E%3C/filter%3E%3Crect width='100%25' height='100%25' filter='url(%23noise)'/%3E%3C/svg%3E");
  background-repeat: repeat; background-size: 256px 256px; }

/* NAV */
nav { position: fixed; top:0; left:0; right:0; z-index:100;
  background: rgba(240,244,248,0.82); backdrop-filter: blur(16px) saturate(1.4);
  -webkit-backdrop-filter: blur(16px) saturate(1.4);
  border-bottom: 1px solid rgba(200,219,232,0.6);
  padding: 0 2.5rem; height: 64px; display:flex; align-items:center;
  justify-content:space-between; }
.nav-logo { display:flex; align-items:center; gap:0.65rem; text-decoration:none; color:var(--ink); }
.nav-logo img { width:34px; height:34px; border-radius:50%; object-fit:cover; }
.nav-logo-text { font-family:"Lora",serif; font-weight:600; font-size:1.1rem; letter-spacing:-0.02em; }
.nav-links { display:flex; align-items:center; gap:0.35rem; list-style:none; }
.nav-links a { text-decoration:none; color:var(--ink-muted); font-size:0.875rem;
  padding:0.4rem 0.75rem; border-radius:var(--radius-sm);
  transition:color 0.25s var(--ease-out), background 0.25s var(--ease-out); }
.nav-links a:hover, .nav-links a.active { color:var(--accent);
  background:rgba(46,109,164,0.06); }
.nav-cta { background:var(--accent) !important; color:white !important;
  padding:0.45rem 1.1rem !important; border-radius:var(--radius-sm) !important;
  font-weight:500 !important; }
.nav-cta:hover { background:var(--accent-dark) !important; }
.nav-hamburger { display:none; background:none; border:none; cursor:pointer; color:var(--ink); padding:0.4rem; font-size:1.4rem; line-height:1; }
@media (max-width:700px) {
  .nav-hamburger { display:flex; align-items:center; }
  .nav-links { display:none; position:absolute; top:64px; left:0; right:0; flex-direction:column; align-items:stretch; background:rgba(240,244,248,0.97); backdrop-filter:blur(16px); -webkit-backdrop-filter:blur(16px); border-bottom:1px solid var(--border); padding:0.25rem 1.5rem 0.75rem; gap:0; z-index:99; }
  .nav-links.mobile-open { display:flex; }
  .nav-links li { border-bottom:1px solid var(--border-light); }
  .nav-links li:last-child { border-bottom:none; }
  .nav-links a { padding:0.8rem 0; font-size:0.95rem; display:block; border-radius:0; }
  .nav-links a:hover, .nav-links a.active { background:transparent; }
  .nav-links .nav-cta { display:inline-block; margin:0.6rem 0; padding:0.5rem 1.1rem !important; border-radius:var(--radius-sm) !important; }
}

/* PAGE SHELL */
.page-body { padding-top: 64px; }
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
code.code-copy { cursor:pointer; position:relative; transition:background 0.2s; }
code.code-copy:hover { background:#D8E6F3; }
code.code-copy.copied::after { content:"Copied \\2713"; position:absolute; top:-1.9em;
  left:50%; transform:translateX(-50%); background:var(--ink); color:#fff;
  font-size:0.72rem; font-family:"Outfit",sans-serif; padding:0.18rem 0.55rem;
  border-radius:4px; white-space:nowrap; z-index:5; }
.notion-todo input { cursor:pointer; }
.notion-todo:has(input:checked) > .notion-todo-text { color:var(--ink-faint); }
.todo-reset { display:inline-flex; align-items:center; gap:0.3rem; background:none;
  border:1px solid var(--border); border-radius:var(--radius-sm); color:var(--ink-muted);
  font-family:inherit; font-size:0.78rem; padding:0.3rem 0.8rem; cursor:pointer;
  margin-bottom:1rem; transition:border-color 0.2s, color 0.2s; }
.todo-reset:hover { border-color:var(--accent-mid); color:var(--accent); }
.notion-divider { border:none; border-top:1px solid var(--border); margin:2rem 0; }
.notion-quote { border-left:3px solid var(--accent); padding:0.5rem 1.25rem;
  margin:1.25rem 0; color:var(--ink-muted); font-style:italic; }
.notion-image { margin:1.5rem 0; }
.notion-image img { max-width:100%; max-height:460px; width:auto; height:auto;
  display:block; margin:0 auto; border-radius:8px; border:1px solid var(--border); }
.notion-image figcaption { font-size:0.8rem; color:var(--ink-faint); margin-top:0.4rem;
  text-align:center; }
.notion-video-figure { margin:1.5rem 0; }
.notion-video { position:relative; aspect-ratio:16/9; overflow:hidden;
  max-width:720px; margin:0 auto; border-radius:8px; border:1px solid var(--border);
  background:#000; }
.notion-video-figure figcaption { font-size:0.8rem; color:var(--ink-faint);
  margin-top:0.4rem; text-align:center; }
.notion-video iframe { position:absolute; top:0; left:0; width:100%; height:100%; border:0; }
.notion-video-file { display:block; max-width:720px; width:100%; margin:1.5rem auto;
  border-radius:8px; border:1px solid var(--border); }
.notion-file, .notion-bookmark { margin-bottom:1rem; }
.notion-file-caption { color:var(--ink-faint); font-size:0.85em; }
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

/* TOC — fixed panel on the left (hover or tap/click to open; pinned on very wide screens) */
.toc-panel { position:fixed; left:0; top:110px; z-index:50; display:flex; align-items:flex-start; }
.toc-tab { width:32px; min-height:140px; padding:1rem 0; background:var(--surface); border:1px solid var(--border); border-left:none; border-radius:0 10px 10px 0; display:flex; flex-direction:column; align-items:center; justify-content:center; gap:0.6rem; flex-shrink:0; cursor:pointer; font-family:inherit; transition:background 0.2s, border-color 0.2s, border-radius 0.3s; box-shadow:2px 0 8px rgba(46,109,164,0.07); }
.toc-tab:focus-visible { outline:2px solid var(--accent); outline-offset:2px; }
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
/* Very wide screens: pin the TOC open as a permanent sidebar */
@media (min-width:1700px) {
  .toc-panel .toc-tab { display:none; }
  .toc { width:288px !important; height:auto !important; opacity:1 !important; }
  .toc-inner { width:288px; height:auto; max-height:calc(100vh - 150px); }
}
a { color:var(--accent); }

/* FORM */
.form-wrap { background:var(--surface); border:1px solid var(--border);
  border-radius:12px; padding:2.5rem; max-width:640px; }
.form-group { margin-bottom:1.5rem; }
.form-label { display:block; font-size:0.87rem; font-weight:500;
  color:var(--ink); margin-bottom:0.4rem; }
.form-label .req { color:var(--accent); margin-left:0.15rem; }
.form-input, .form-select { width:100%; padding:0.65rem 0.9rem;
  border:1px solid var(--border); border-radius:var(--radius-sm);
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
  padding:0.75rem 2rem; border-radius:var(--radius-sm); font-family:inherit;
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

/* SPONSORS */
.sponsors-list { display:flex; flex-direction:column; gap:1.5rem; max-width:760px; }
.sponsor-card { background:var(--surface); border:1px solid var(--border);
  border-radius:var(--radius-lg); padding:2.5rem; }
.sponsor-tier { display:inline-block; font-size:0.7rem; font-weight:500;
  letter-spacing:0.1em; text-transform:uppercase; color:var(--accent);
  background:var(--accent-light); padding:0.25rem 0.75rem; border-radius:2rem;
  margin-bottom:1.4rem; }
.sponsor-tier-gold { color:#8A6A15; background:#FBF1D5; border:1px solid #E8D193; }
.sponsor-logo { display:block; max-width:280px; max-height:72px; width:100%;
  object-fit:contain; object-position:left; margin-bottom:1.4rem; }
.sponsor-desc { color:var(--ink-muted); font-size:0.95rem; line-height:1.75;
  margin-bottom:0.9rem; }
.sponsor-thanks { color:var(--ink-faint); font-size:0.85rem; font-style:italic;
  margin-bottom:1.4rem; }
.btn-sponsor { display:inline-flex; align-items:center; gap:0.4rem;
  background:var(--accent); color:#fff !important; text-decoration:none;
  padding:0.6rem 1.4rem; border-radius:var(--radius-sm); font-size:0.9rem;
  font-weight:500; transition:background 0.2s; }
.btn-sponsor:hover { background:var(--accent-dark); }
.sponsor-invite { background:var(--accent-light); border:1px solid var(--accent-mid);
  border-radius:var(--radius-lg); padding:2rem 2.5rem; max-width:760px; margin-top:2rem; }
.sponsor-invite h3 { font-family:"Lora",serif; font-size:1.25rem; color:var(--ink);
  margin-bottom:0.5rem; }
.sponsor-invite p { color:var(--ink-muted); font-size:0.92rem; line-height:1.7;
  margin-bottom:1.1rem; }
@media (max-width:640px) { .sponsor-card, .sponsor-invite { padding:1.75rem 1.5rem; } }

/* JOBS BOARD */
.job-card { background:var(--surface); border:1px solid var(--border);
  border-radius:var(--radius-lg); padding:1.75rem; margin-bottom:1.25rem; max-width:820px; }
.job-head { display:flex; justify-content:space-between; align-items:center;
  gap:1rem; flex-wrap:wrap; }
.job-title { font-family:"Lora",serif; font-size:1.25rem; font-weight:600;
  color:var(--ink); margin:0; }
.job-status { font-size:0.68rem; font-weight:500; letter-spacing:0.1em;
  text-transform:uppercase; padding:0.22rem 0.7rem; border-radius:2rem;
  background:#EAFAF1; color:#1A6B35; border:1px solid #9CD7B4; }
.job-meta { font-size:0.8rem; color:var(--ink-faint); margin:0.4rem 0 0.9rem; }
.job-desc { color:var(--ink-muted); font-size:0.93rem; line-height:1.7; }
.job-req { font-size:0.88rem; color:var(--ink-muted); background:var(--bg);
  border:1px solid var(--border-light); border-radius:var(--radius);
  padding:0.8rem 1rem; margin:0.9rem 0; line-height:1.65; }
.job-actions { display:flex; gap:0.75rem; flex-wrap:wrap; align-items:center;
  margin-top:1rem; }
.jobs-empty { background:var(--surface); border:1px dashed var(--border);
  border-radius:var(--radius-lg); padding:2rem; max-width:820px;
  color:var(--ink-muted); font-size:0.95rem; }

/* ABOUT */
.about-prose { max-width:760px; color:var(--ink-muted); font-size:0.97rem;
  line-height:1.75; }
.about-prose p { margin-bottom:1rem; }
.about-prose strong { color:var(--ink); }
.about-h { font-family:"Lora",serif; font-size:1.7rem; font-weight:600;
  color:var(--ink); margin:2.5rem 0 1rem; letter-spacing:-0.01em; }
.about-stats { display:grid; grid-template-columns:repeat(3,1fr); gap:1rem;
  max-width:760px; margin:2rem 0; }
.about-stat { background:var(--surface); border:1px solid var(--border);
  border-radius:var(--radius-lg); padding:1.4rem 1rem; text-align:center; }
.about-stat-value { font-family:"Lora",serif; font-size:1.7rem; font-weight:600;
  color:var(--accent); line-height:1.15; }
.about-stat-label { font-size:0.78rem; color:var(--ink-muted); margin-top:0.25rem; }
@media (max-width:600px) { .about-stats { grid-template-columns:1fr; } }
.team-grid { display:grid; grid-template-columns:repeat(auto-fill,minmax(190px,1fr));
  gap:1rem; margin:1.5rem 0 2.5rem; }
.team-card { background:var(--surface); border:1px solid var(--border);
  border-radius:var(--radius-lg); padding:1.5rem 1.1rem; text-align:center;
  transition:border-color 0.25s, box-shadow 0.25s; }
.team-card:hover { border-color:var(--accent-mid); box-shadow:var(--shadow-sm); }
.team-avatar, .team-avatar-fallback { width:72px; height:72px; border-radius:50%;
  margin:0 auto 0.8rem; }
.team-avatar { object-fit:cover; display:block; border:2px solid var(--accent-light); }
.team-avatar-fallback { background:var(--accent-light); color:var(--accent-dark);
  display:flex; align-items:center; justify-content:center;
  font-family:"Lora",serif; font-weight:600; font-size:1.25rem; }
.team-name { font-weight:500; color:var(--ink); font-size:0.95rem; }
.team-role { font-size:0.76rem; color:var(--ink-muted); margin-top:0.2rem; line-height:1.5; }
.team-email { display:inline-block; font-size:0.75rem; margin-top:0.45rem; }

/* FOOTER */
footer { padding:3rem 2rem 0; border-top:1px solid var(--border); background:var(--surface); }
.footer-inner { max-width:1100px; margin:0 auto; display:flex;
  align-items:flex-start; justify-content:space-between; gap:2.5rem;
  flex-wrap:wrap; padding-bottom:2.25rem; }
.footer-brand { max-width:240px; }
.footer-logo { display:flex; align-items:center; gap:0.55rem;
  text-decoration:none; color:var(--ink); margin-bottom:0.6rem; }
.footer-logo img { width:26px; height:26px; border-radius:50%; object-fit:cover; }
.footer-name { font-family:"Lora",serif; font-weight:600; font-size:0.95rem; }
.footer-tagline { font-size:0.77rem; color:var(--ink-faint); line-height:1.6; }
.footer-cols { display:flex; gap:3rem; flex-wrap:wrap; }
.footer-col { display:flex; flex-direction:column; gap:0.45rem; min-width:120px; }
.footer-col-title { font-size:0.68rem; font-weight:500; letter-spacing:0.12em;
  text-transform:uppercase; color:var(--ink-faint); margin-bottom:0.25rem; }
.footer-col a { font-size:0.82rem; color:var(--ink-muted); text-decoration:none;
  transition:color 0.2s; }
.footer-col a:hover { color:var(--accent); }
.footer-bottom { max-width:1100px; margin:0 auto; border-top:1px solid var(--border-light);
  padding:1rem 0 1.5rem; display:flex; justify-content:space-between;
  align-items:center; gap:0.5rem 1.5rem; flex-wrap:wrap;
  font-size:0.75rem; color:var(--ink-faint); }
.footer-bottom a { color:var(--ink-muted); text-decoration:none; }
.footer-bottom a:hover { color:var(--accent); }
@media (max-width:640px) {
  footer { padding:2.5rem 1.25rem 0; }
  .footer-cols { gap:2rem; }
}
"""

FONTS = ('<link rel="preconnect" href="https://fonts.googleapis.com">'
         '<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>'
         '<link href="https://fonts.googleapis.com/css2?family=Lora:ital,wght@0,400;0,600;1,400'
         '&family=Outfit:wght@300;400;500;600&display=swap" rel="stylesheet">')

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
    # One nav for the whole site — index.html and register.html mirror this manually
    pages = [
        ("index.html",                 "Home",            "home"),
        ("getting-started.html",       "Getting Started", "getting-started"),
        ("submission-guidelines.html", "Submit Cards",    "submission-guidelines"),
        ("checklist.html",             "Checklist",       "checklist"),
        ("about.html",                 "About",           "about"),
        ("sponsors.html",              "Sponsors",        "sponsors"),
    ]
    items = []
    for href, label, key in pages:
        cls = ' class="active"' if key == active else ""
        items.append(f'<li><a href="{href}"{cls}>{label}</a></li>')
    reg_cls = "nav-cta active" if active == "register" else "nav-cta"
    items.append(f'<li><a href="register.html" class="{reg_cls}">Register</a></li>')
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
    <div class="footer-brand">
      <a class="footer-logo" href="index.html">{img}<span class="footer-name">Malleus</span></a>
      <p class="footer-tagline">Clinical Medicine · AU/NZ<br>Open Source · Not for Profit</p>
    </div>
    <div class="footer-cols">
      <div class="footer-col">
        <span class="footer-col-title">Project</span>
        <a href="getting-started.html">Getting Started</a>
        <a href="submission-guidelines.html">Submit Cards</a>
        <a href="checklist.html">Checklist</a>
        <a href="about.html">About Us</a>
        <a href="sponsors.html">Sponsors</a>
        <a href="https://malleuscm.notion.site/" target="_blank" rel="noopener">Notion Hub</a>
      </div>
      <div class="footer-col">
        <span class="footer-col-title">Community</span>
        <a href="https://discord.gg/4WqgJzjVyH" target="_blank" rel="noopener">Discord</a>
        <a href="https://www.facebook.com/MalleusCM" target="_blank" rel="noopener">Facebook</a>
        <a href="https://www.instagram.com/projectmalleus" target="_blank" rel="noopener">Instagram</a>
        <a href="https://www.youtube.com/@MalleusClinicalMedicine" target="_blank" rel="noopener">YouTube</a>
        <a href="https://community.ankihub.net/tags/c/ankihub-decks/updates/31/updates-malleus-clinical-medicine-aunz-stapedius/2338" target="_blank" rel="noopener">Newsletter</a>
      </div>
      <div class="footer-col">
        <span class="footer-col-title">Get Involved</span>
        <a href="register.html">Register</a>
        <a href="jobs-board.html">Jobs Board</a>
        <a href="https://www.paypal.com/donate/?hosted_button_id=N5G46YHELZJ6C" target="_blank" rel="noopener">Donate</a>
        <a href="mailto:admin@malleus.org.au">Contact</a>
      </div>
    </div>
  </div>
  <div class="footer-bottom">
    <span>&copy; Project Malleus. Volunteer run, not for profit.</span>
    <a href="terms-of-use.html">Terms of Use</a>
  </div>
</footer>"""


def page_shell(title: str, logo_name: str, active: str, body: str,
               extra_js: str = "", description: str = "") -> str:
    description = description or (
        "Malleus Clinical Medicine — the open-source, collaborative Anki flashcard "
        "deck for Australian and New Zealand medical students and JMOs.")
    page_url = f"{SITE_URL}{active}.html" if active else SITE_URL
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>{title} — Malleus Clinical Medicine</title>
  <meta name="description" content="{escape(description)}">
  <link rel="icon" type="image/png" href="favicon.png">
  <link rel="apple-touch-icon" href="apple-touch-icon.png">
  <meta property="og:type" content="website">
  <meta property="og:site_name" content="Malleus Clinical Medicine">
  <meta property="og:title" content="{escape(title)} — Malleus Clinical Medicine">
  <meta property="og:description" content="{escape(description)}">
  <meta property="og:image" content="{SITE_URL}logo.png">
  <meta property="og:url" content="{page_url}">
  <meta name="twitter:card" content="summary">
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
  var _tocNav    = _tocPanel.querySelector('.toc');
  var _tocTab    = _tocPanel.querySelector('.toc-tab');
  var _tocPinned = window.matchMedia('(min-width: 1700px)');
  var _tocOpen   = false;
  function tocOpen() {
    if (_tocPinned.matches) return;
    _tocOpen = true;
    _tocNav.style.height = (window.innerHeight - 125) + 'px';
    _tocNav.style.width = '256px'; _tocNav.style.opacity = '1';
    _tocTab.setAttribute('aria-expanded', 'true');
  }
  function tocClose() {
    if (_tocPinned.matches) return;
    _tocOpen = false;
    _tocNav.style.width = '0px'; _tocNav.style.height = '0px'; _tocNav.style.opacity = '0';
    _tocTab.setAttribute('aria-expanded', 'false');
  }
  _tocTab.addEventListener('click', function() { _tocOpen ? tocClose() : tocOpen(); });
  if (window.matchMedia('(hover: hover)').matches) {
    _tocPanel.addEventListener('mouseenter', tocOpen);
    _tocPanel.addEventListener('mouseleave', tocClose);
  }
  _tocNav.addEventListener('click', function(e) { if (e.target.closest('a')) tocClose(); });
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
    return page_shell(title, logo_name, active, body, toc_js, description=subtitle)


# Sponsors shown on sponsors.html — logo files must sit next to build.py
SPONSORS = [
    {
        "name":  "eMedici",
        "logo":  "emedici.png",
        "url":   "https://emedici.com",
        "tier":  "Gold Sponsor",
        "blurb": "eMedici is an Australian clinical education platform built around "
                 "thousands of case-based practice questions, used by medical students "
                 "and junior doctors across Australia and New Zealand. Malleus cards are "
                 "cross-linked to eMedici questions through our Question Bank tags, so "
                 "you can practise a topic on eMedici and review the matching flashcards "
                 "in Anki.",
        "thanks": "We're grateful to eMedici for supporting Malleus and helping keep "
                  "the project free for students.",
    },
]


def build_sponsors_page(logo_name: str) -> str:
    cards = ""
    for s in SPONSORS:
        tier_cls = "sponsor-tier sponsor-tier-" + _slugify(s["tier"].split()[0])
        cards += f"""
    <div class="sponsor-card">
      <span class="{tier_cls}">{escape(s["tier"])}</span>
      <img class="sponsor-logo" src="{escape(s["logo"])}" alt="{escape(s["name"])} logo">
      <p class="sponsor-desc">{escape(s["blurb"])}</p>
      <p class="sponsor-thanks">{escape(s["thanks"])}</p>
      <a class="btn-sponsor" href="{escape(s["url"])}" target="_blank" rel="noopener">Visit {escape(s["name"])} &rarr;</a>
    </div>"""

    body = f"""
<div class="page-header">
  <div class="page-header-inner">
    <div class="page-eyebrow">Our Supporters</div>
    <h1 class="page-title">Sponsors</h1>
    <p class="page-subtitle">
      Malleus is a not-for-profit, student-run project. Our sponsors help cover hosting
      and tooling costs so the deck stays free for every medical student in Australia
      and New Zealand.
    </p>
  </div>
</div>
<div class="page-content">
  <div class="sponsors-list">{cards}
  </div>

  <div class="sponsor-invite">
    <h3>Become a sponsor</h3>
    <p>
      If your organisation would like to support open-source medical education in
      Australia and New Zealand, we'd love to hear from you. Every sponsorship package
      is personalised to your organisation — get in touch and we'll work out something
      that fits. Sponsorship directly funds hosting, tooling, and the volunteer-driven
      maintenance of the deck.
    </p>
    <a class="btn-sponsor" href="mailto:sponsorship@malleus.org.au">Get in Touch &rarr;</a>
  </div>
</div>"""
    return page_shell("Sponsors", logo_name, "sponsors", body, description=(
        "The sponsors who keep Malleus Clinical Medicine free for AU/NZ medical "
        "students — and how your organisation can support the project."))


# Roles in display priority order (Contacts DB multi-select; unknown roles sort last)
ROLE_ORDER = [
    "President", "Vice-President", "Secretary", "Treasurer",
    "Sponsorship Officer", "Publications/Promotions Officer", "IT Officer",
    "Lead Malleus Maintainer", "Lead Content Review Officer", "JMO Rep",
    "Maintainer", "Content Reviewer",
]


# Open positions whose title contains one of these (case-insensitive) are pinned
# to the top of the jobs board, in this order; everything else follows by date.
PINNED_JOBS = ("publications/promotions",)


def build_jobs_page(logo_name: str) -> str:
    print("  Fetching positions database…")
    rows = fetch_db_rows(JOBS_DB_ID)
    open_rows = [r for r in rows if _prop(r, "Status", "select") == "Open"]

    # Newest first, then stable-sort pinned titles to the top
    open_rows.sort(key=lambda r: _prop(r, "Job Posted", "date") or "", reverse=True)
    open_rows.sort(key=lambda r: next(
        (i for i, p in enumerate(PINNED_JOBS)
         if p in _prop(r, "Job Title", "title").lower()), len(PINNED_JOBS)))

    # Election rules apply board-wide: surface the most common link once, up top
    from collections import Counter
    rules_counts = Counter(u for r in rows if (u := _prop(r, "Election Rules", "url")))
    rules_url = rules_counts.most_common(1)[0][0] if rules_counts else ""
    rules_intro = (f' Most positions are filled by election — read the '
                   f'<a href="{escape(rules_url)}" target="_blank" rel="noopener">election rules</a> '
                   f'before applying.' if rules_url else "")

    cards = ""
    for r in open_rows:
        title   = _prop(r, "Job Title", "title") or "Untitled position"
        dept    = _prop(r, "Department", "select")
        posted  = _fmt_date(_prop(r, "Job Posted", "date"))
        closes  = _fmt_date(_prop(r, "Application Closing Date", "date"))
        desc    = _prop(r, "Role Description", "rich_text")
        reqs    = _prop(r, "Application Requirements", "rich_text")
        email   = _prop(r, "Contact Email", "email") or "secretary@malleus.org.au"

        meta = " · ".join(x for x in (
            dept,
            f"Posted {posted}" if posted else "",
            f"Applications close {closes}" if closes else "Open until filled",
        ) if x)
        desc_html = f'<p class="job-desc">{escape(desc)}</p>' if desc else ""
        reqs_html = f'<div class="job-req"><strong>To apply:</strong> {escape(reqs)}</div>' if reqs else ""
        cards += f"""
  <div class="job-card">
    <div class="job-head"><h3 class="job-title">{escape(title)}</h3><span class="job-status">Open</span></div>
    <div class="job-meta">{escape(meta)}</div>
    {desc_html}
    {reqs_html}
    <div class="job-actions">
      <a class="btn-sponsor" href="mailto:{escape(email)}?subject=Application: {escape(title)}">Apply by Email &rarr;</a>
    </div>
  </div>"""

    if not cards:
        cards = """
  <div class="jobs-empty">
    There are no open positions right now — but we're always happy to hear from keen
    contributors. Email <a href="mailto:secretary@malleus.org.au">secretary@malleus.org.au</a>
    to register your interest for the next round, or just start
    <a href="submission-guidelines.html">suggesting cards</a> — most of our team started that way.
  </div>"""

    body = f"""
<div class="page-header">
  <div class="page-header-inner">
    <div class="page-eyebrow">We're Hiring</div>
    <h1 class="page-title">Jobs Board</h1>
    <p class="page-subtitle">
      Malleus is run entirely by volunteer medical students and junior doctors —
      and we're always looking for more. Open committee positions are listed below
      and update automatically.
    </p>
  </div>
</div>
<div class="page-content">
  <div class="notion-callout notion-callout-blue" style="max-width:820px;">
    <span class="callout-icon">📨</span>
    <div>Questions about a role, or want to apply outside the formal election period?
    Email our Secretary at <a href="mailto:secretary@malleus.org.au">secretary@malleus.org.au</a>
    with your name, uni or work status (e.g. PGY1), and location. You can see the current
    committee on our <a href="about.html">About page</a>.{rules_intro}</div>
  </div>
  {cards}
</div>"""
    return page_shell("Jobs Board", logo_name, "jobs-board", body, description=(
        "Open volunteer positions on the Malleus Clinical Medicine committee — "
        "join the team behind the AU/NZ Anki deck."))


def build_about_page(logo_name: str) -> str:
    print("  Fetching contacts database…")
    rows = fetch_db_rows(CONTACTS_DB_ID)

    def role_rank(r):
        roles = _prop(r, "Role(s)", "multi_select")
        ranks = [ROLE_ORDER.index(x) for x in roles if x in ROLE_ORDER] or [len(ROLE_ORDER)]
        return (min(ranks), _prop(r, "Name", "title"))

    rows.sort(key=role_rank)

    team_cards = ""
    for r in rows:
        name  = _prop(r, "Name", "title")
        if not name:
            continue
        roles = _prop(r, "Role(s)", "multi_select")
        roles = sorted(roles, key=lambda x: ROLE_ORDER.index(x) if x in ROLE_ORDER else len(ROLE_ORDER))
        email = _prop(r, "Email", "email")
        # Photos are optional: drop team/<name-slug>.jpg|.png next to build.py
        photo = next((f"team/{_slugify(name)}{ext}" for ext in (".jpg", ".jpeg", ".png")
                      if Path(f"team/{_slugify(name)}{ext}").exists()), None)
        if photo:
            avatar = f'<img class="team-avatar" src="{photo}" alt="{escape(name)}" loading="lazy">'
        else:
            initials = "".join(w[0] for w in name.split()[:2]).upper()
            avatar = f'<div class="team-avatar-fallback">{escape(initials)}</div>'
        email_html = f'<a class="team-email" href="mailto:{escape(email)}">{escape(email)}</a>' if email else ""
        team_cards += f"""
    <div class="team-card">
      {avatar}
      <div class="team-name">{escape(name)}</div>
      <div class="team-role">{escape(" · ".join(roles))}</div>
      {email_html}
    </div>"""

    team_section = f'<div class="team-grid">{team_cards}\n  </div>' if team_cards else """
  <p class="about-prose">Our full committee list is on
  <a href="https://malleuscm.notion.site/about-project-malleus" target="_blank" rel="noopener">Notion</a>.</p>"""

    body = f"""
<div class="page-header">
  <div class="page-header-inner">
    <div class="page-eyebrow">Who We Are</div>
    <h1 class="page-title">About Project Malleus</h1>
    <p class="page-subtitle">
      A volunteer-run, not-for-profit student association with one mission:
      to radically reform medical education in Australia and New Zealand.
    </p>
  </div>
</div>
<div class="page-content">
  <div class="about-prose">
    <p>
      Project Malleus was founded by <strong>Eric Smith</strong> in 2022, with the
      invaluable help of <strong>Sabiqul Hoque</strong> — both now PGY2 resident medical
      officers. What started as a student project has grown into the largest
      collaborative clinical medicine Anki deck written for Australian and New Zealand
      practice, and it has always stayed volunteer run, open source, and free to use.
    </p>
    <p>
      <strong>Hugh Fenton-White</strong>, our Lead Malleus Maintainer, oversees deck
      completion — which we anticipate by December 2026 — assisted by
      <strong>Michael Colla</strong>, our Lead Content Reviewer, and a team of
      maintainers who review every community submission.
    </p>
  </div>

  <div class="about-stats">
    <div class="about-stat"><div class="about-stat-value">2,000+</div><div class="about-stat-label">Active subscribers on AnkiHub</div></div>
    <div class="about-stat"><div class="about-stat-value">11</div><div class="about-stat-label">Volunteer maintainers</div></div>
    <div class="about-stat"><div class="about-stat-value">100%</div><div class="about-stat-label">Volunteer run &amp; not for profit</div></div>
  </div>

  <h2 class="about-h">The Committee</h2>
  {team_section}

  <div class="sponsor-invite">
    <h3>We're hiring</h3>
    <p>
      Malleus runs on volunteers, and there's always room for more — from committee
      positions to maintainers and content reviewers. Open roles are listed on our
      jobs board and update automatically.
    </p>
    <a class="btn-sponsor" href="jobs-board.html">View Open Positions &rarr;</a>
  </div>

  <h2 class="about-h" id="contact">Get in Touch</h2>
  <p class="about-prose">
    General enquiries: <a href="mailto:admin@malleus.org.au">admin@malleus.org.au</a> ·
    You can also find us on <a href="https://discord.gg/4WqgJzjVyH" target="_blank" rel="noopener">Discord</a>,
    <a href="https://www.instagram.com/projectmalleus" target="_blank" rel="noopener">Instagram</a>,
    <a href="https://www.youtube.com/@MalleusClinicalMedicine" target="_blank" rel="noopener">YouTube</a> and
    <a href="https://www.facebook.com/MalleusCM" target="_blank" rel="noopener">Facebook</a>.
    If you'd like to support our work, you can
    <a href="https://www.paypal.com/donate/?hosted_button_id=N5G46YHELZJ6C" target="_blank" rel="noopener">buy us a coffee</a>.
  </p>
</div>"""
    return page_shell("About Us", logo_name, "about", body, description=(
        "Project Malleus is a volunteer-run, not-for-profit student association building "
        "the open-source clinical medicine Anki deck for Australia and New Zealand."))


def build_404_page(logo_name: str) -> str:
    body = """
<div class="page-header">
  <div class="page-header-inner">
    <div class="page-eyebrow">404</div>
    <h1 class="page-title">This card has been suspended</h1>
    <p class="page-subtitle">
      The page you're looking for doesn't exist — it may have moved, been deleted,
      or never made it past the maintainer review.
    </p>
  </div>
</div>
<div class="page-content">
  <div class="sponsor-actions" style="display:flex;gap:0.75rem;flex-wrap:wrap;">
    <a class="btn-sponsor" href="index.html">Back to Home &rarr;</a>
    <a class="btn-sponsor" style="background:var(--surface);color:var(--ink) !important;border:1px solid var(--border);" href="getting-started.html">Getting Started</a>
  </div>
</div>"""
    html = page_shell("Page Not Found", logo_name, "", body,
                      description="Page not found — Malleus Clinical Medicine.")
    # GitHub Pages serves 404.html for any missing path, including nested ones,
    # so relative links need an explicit base to keep resolving to the site root.
    return html.replace("<head>", f'<head>\n  <base href="{SITE_URL}">', 1)


def write_seo_files():
    """Write sitemap.xml and robots.txt into dist/."""
    from datetime import date
    today = date.today().isoformat()
    pages = ["", "getting-started.html", "submission-guidelines.html",
             "checklist.html", "about.html", "jobs-board.html",
             "sponsors.html", "register.html", "terms-of-use.html"]
    urls = "".join(
        f"<url><loc>{SITE_URL}{p}</loc><lastmod>{today}</lastmod></url>" for p in pages)
    (DIST_DIR / "sitemap.xml").write_text(
        '<?xml version="1.0" encoding="UTF-8"?>'
        f'<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{urls}</urlset>',
        encoding="utf-8")
    (DIST_DIR / "robots.txt").write_text(
        f"User-agent: *\nAllow: /\nSitemap: {SITE_URL}sitemap.xml\n", encoding="utf-8")


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

    return page_shell("Register as a Member", logo_name, "register", body, js, description=(
        "Register as a general member of Malleus Clinical Medicine — get community "
        "updates, voting rights at AGMs, and a free @malleus.org.au email address."))


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
    for asset in ("addon.png", "phone-transparent.png", "anki-screenshot.jpg",
                  "emedici.png", "favicon.png", "apple-touch-icon.png"):
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

    print("📄  Generating Terms of Use page…")
    html = build_notion_page(
        "Terms of Use", "Legal",
        "The terms and conditions for using the Malleus deck, website, and associated materials.",
        NOTION_PAGES["terms-of-use"], "terms-of-use", logo_name
    )
    (DIST_DIR / "terms-of-use.html").write_text(html, encoding="utf-8")

    print("📄  Generating Jobs Board page…")
    html = build_jobs_page(logo_name)
    (DIST_DIR / "jobs-board.html").write_text(html, encoding="utf-8")

    print("📄  Generating About page…")
    html = build_about_page(logo_name)
    (DIST_DIR / "about.html").write_text(html, encoding="utf-8")
    if Path("team").exists():
        shutil.copytree("team", DIST_DIR / "team", dirs_exist_ok=True)
        print("  ✅  team/ photos copied to dist/")

    print("📄  Generating Sponsors page…")
    html = build_sponsors_page(logo_name)
    (DIST_DIR / "sponsors.html").write_text(html, encoding="utf-8")

    print("📄  Generating 404 page, sitemap.xml and robots.txt…")
    (DIST_DIR / "404.html").write_text(build_404_page(logo_name), encoding="utf-8")
    write_seo_files()

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
