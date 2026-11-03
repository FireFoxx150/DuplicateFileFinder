"""Write duplicate groups to a self-contained HTML page."""
import html
import os

STYLE = """body{font-family:sans-serif;margin:24px}table{border-collapse:collapse;width:100%}
td,th{border:1px solid #bbb;padding:4px 8px;text-align:left}th{background:#eee}
h2{margin-top:28px}"""


def write_html(groups, path, title="Duplicate File Finder report"):
    wasted = 0
    parts = []
    for i, group in enumerate(groups, 1):
        size = os.path.getsize(group[0])
        wasted += size * (len(group) - 1)
        rows = "".join(f"<tr><td>{html.escape(p)}</td><td>{size:,} bytes</td></tr>" for p in group)
        parts.append(f"<h2>Group {i}: {len(group)} copies</h2><table><tr><th>Path</th><th>Size</th></tr>{rows}</table>")
    body = (f"<h1>{html.escape(title)}</h1><p>{len(groups)} groups, "
            f"{wasted:,} bytes could be reclaimed.</p>" + "".join(parts))
    with open(path, "w", encoding="utf-8") as f:
        f.write(f"<!doctype html><html><head><meta charset='utf-8'><title>{html.escape(title)}</title>"
                f"<style>{STYLE}</style></head><body>{body}</body></html>")
