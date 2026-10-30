"""PDF Exporter for DocuPulse Enterprise Engineering Report.

Converts Markdown documentation with tables, math, and code blocks into an
executive-grade PDF using headless Google Chrome / Microsoft Edge.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

from markdown_it import MarkdownIt


def find_browser_executable() -> str:
    """Locate headless Chrome or Edge executable on the host machine."""
    candidates = [
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    ]
    for path in candidates:
        if os.path.exists(path):
            return path
    raise FileNotFoundError("Neither Google Chrome nor Microsoft Edge was found on the system.")


def format_math_expressions(text: str) -> str:
    """Preprocess LaTeX math formulas for HTML display."""
    # Display math: $$...$$
    def replace_display_math(match: re.Match) -> str:
        inner = match.group(1).strip()
        # Clean common math symbols for clean presentation
        cleaned = inner.replace(r"\le", "&le;").replace(r"\ge", "&ge;")
        cleaned = cleaned.replace(r"\pm", "&plusmn;").replace(r"\neq", "&ne;")
        cleaned = cleaned.replace(r"\sum", "&sum;").replace(r"\times", "&times;")
        cleaned = cleaned.replace(r"\cdot", "&middot;").replace(r"\left|", "|").replace(r"\right|", "|")
        cleaned = cleaned.replace(r"\text{", "").replace(r"}", "")
        return f'<div class="math-display"><em>{cleaned}</em></div>'

    text = re.sub(r"\$\$(.*?)\$\$", replace_display_math, text, flags=re.DOTALL)

    # Inline math: $...$
    def replace_inline_math(match: re.Match) -> str:
        inner = match.group(1).strip()
        cleaned = inner.replace(r"\le", "&le;").replace(r"\ge", "&ge;")
        cleaned = cleaned.replace(r"\pm", "&plusmn;").replace(r"\neq", "&ne;")
        cleaned = cleaned.replace(r"\sum", "&sum;").replace(r"\times", "&times;")
        cleaned = cleaned.replace(r"\cdot", "&middot;").replace(r"\left|", "|").replace(r"\right|", "|")
        cleaned = cleaned.replace(r"\text{", "").replace(r"}", "")
        return f'<span class="math-inline"><em>{cleaned}</em></span>'

    text = re.sub(r"\$([^\$\n]+?)\$", replace_inline_math, text)
    return text


def build_styled_html(md_content: str) -> str:
    """Convert Markdown to complete executive HTML document with print CSS."""
    # Preprocess math
    processed_md = format_math_expressions(md_content)

    # Initialize parser with tables
    md = MarkdownIt("commonmark", {"linkify": False, "html": True}).enable("table")
    body_html = md.render(processed_md)

    html_template = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>DocuPulse Enterprise AP v4.2 - Engineering Architecture & Benchmark Report</title>
<style>
    @page {{
        size: A4 portrait;
        margin: 20mm 16mm 20mm 16mm;
        @bottom-right {{
            content: "Page " counter(page);
            font-size: 8pt;
            color: #64748b;
        }}
        @bottom-left {{
            content: "DocuPulse AP v4.2 | Confidential & Proprietary";
            font-size: 8pt;
            color: #64748b;
        }}
    }}

    *, *:before, *:after {{
        box-sizing: border-box;
    }}

    body {{
        font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
        font-size: 9.5pt;
        line-height: 1.55;
        color: #1e293b;
        background-color: #ffffff;
        margin: 0;
        padding: 0;
    }}

    h1, h2, h3, h4, h5, h6 {{
        color: #0f172a;
        font-weight: 700;
        margin-top: 1.4em;
        margin-bottom: 0.5em;
        line-height: 1.25;
    }}

    h1 {{
        font-size: 18pt;
        border-bottom: 2px solid #2563eb;
        padding-bottom: 8px;
        margin-top: 0;
        color: #1e3a8a;
    }}

    h2 {{
        font-size: 13pt;
        border-bottom: 1px solid #cbd5e1;
        padding-bottom: 4px;
        page-break-before: always;
        color: #1e40af;
        margin-top: 24pt;
    }}

    /* Prevent page break on the very first h2 */
    h2:first-of-type {{
        page-break-before: avoid;
    }}

    h3 {{
        font-size: 11pt;
        color: #1e293b;
    }}

    h4 {{
        font-size: 10pt;
        color: #334155;
    }}

    p {{
        margin-top: 0;
        margin-bottom: 0.8em;
        text-align: justify;
    }}

    ul, ol {{
        margin-top: 0;
        margin-bottom: 0.8em;
        padding-left: 20px;
    }}

    li {{
        margin-bottom: 0.3em;
    }}

    hr {{
        border: 0;
        height: 1px;
        background: #cbd5e1;
        margin: 20px 0;
    }}

    table {{
        width: 100%;
        border-collapse: collapse;
        margin: 12pt 0;
        font-size: 8.5pt;
        page-break-inside: avoid;
    }}

    th, td {{
        border: 1px solid #cbd5e1;
        padding: 6px 8px;
        text-align: left;
    }}

    th {{
        background-color: #f1f5f9;
        font-weight: 600;
        color: #0f172a;
    }}

    tr:nth-child(even) {{
        background-color: #f8fafc;
    }}

    pre, code {{
        font-family: "JetBrains Mono", Consolas, "Courier New", monospace;
    }}

    pre {{
        background-color: #0f172a;
        color: #f8fafc;
        padding: 10px 14px;
        border-radius: 6px;
        font-size: 7.8pt;
        line-height: 1.45;
        overflow-x: auto;
        white-space: pre-wrap;
        word-wrap: break-word;
        page-break-inside: avoid;
        margin: 10pt 0;
        border: 1px solid #1e293b;
    }}

    code:not(pre code) {{
        background-color: #f1f5f9;
        color: #0f172a;
        padding: 2px 4px;
        border-radius: 4px;
        font-size: 8.5pt;
        border: 1px solid #e2e8f0;
    }}

    blockquote {{
        border-left: 4px solid #3b82f6;
        margin: 10pt 0;
        padding: 6pt 14pt;
        background-color: #eff6ff;
        color: #1e3a8a;
        font-style: italic;
    }}

    .math-display {{
        background-color: #f8fafc;
        border: 1px solid #e2e8f0;
        border-left: 3px solid #6366f1;
        padding: 8px 12px;
        margin: 8pt 0;
        text-align: center;
        font-family: Georgia, serif;
        font-size: 10pt;
        color: #1e1b4b;
        page-break-inside: avoid;
    }}

    .math-inline {{
        font-family: Georgia, serif;
        font-size: 9.5pt;
        color: #1e1b4b;
        padding: 0 2px;
    }}

    .badge {{
        display: inline-block;
        padding: 2px 6px;
        font-size: 7.5pt;
        font-weight: 600;
        border-radius: 4px;
    }}
    .badge-pass {{
        background-color: #dcfce7;
        color: #166534;
        border: 1px solid #86efac;
    }}

    /* Print optimizations */
    @media print {{
        body {{
            -webkit-print-color-adjust: exact;
            print-color-adjust: exact;
        }}
        a {{
            color: #1e40af;
            text-decoration: none;
        }}
        pre {{
            background-color: #0f172a !important;
            color: #f8fafc !important;
        }}
    }}
</style>
</head>
<body>
{body_html}
</body>
</html>
"""
    return html_template


def compile_pdf(input_md_path: Path, output_pdf_path: Path) -> Path:
    """Generate high-fidelity PDF from Markdown."""
    if not input_md_path.exists():
        raise FileNotFoundError(f"Input file not found: {input_md_path}")

    browser_bin = find_browser_executable()
    print(f"[PDF] Using browser engine: {browser_bin}")

    with open(input_md_path, "r", encoding="utf-8") as f:
        md_text = f.read()

    html_content = build_styled_html(md_text)

    # Write temporary HTML file
    temp_dir = Path(tempfile.gettempdir())
    temp_html = temp_dir / f"docupulse_report_{os.getpid()}.html"

    with open(temp_html, "w", encoding="utf-8") as f:
        f.write(html_content)

    print(f"[PDF] Staged styled HTML: {temp_html} ({len(html_content):,} bytes)")

    # Ensure output parent directory exists
    output_pdf_path.parent.mkdir(parents=True, exist_ok=True)

    cmd = [
        browser_bin,
        "--headless=new",
        "--disable-gpu",
        "--no-sandbox",
        "--allow-file-access-from-files",
        f"--print-to-pdf={output_pdf_path.resolve()}",
        str(temp_html.resolve()),
    ]

    print(f"[PDF] Compiling PDF via headless browser...")
    result = subprocess.run(cmd, capture_output=True, text=True)

    if result.returncode != 0:
        print(f"[PDF] STDERR: {result.stderr}", file=sys.stderr)
        raise RuntimeError(f"PDF compilation failed with code {result.returncode}")

    if not output_pdf_path.exists() or output_pdf_path.stat().st_size == 0:
        raise RuntimeError(f"Output PDF file was not generated or is empty: {output_pdf_path}")

    print(f"[PDF] [SUCCESS] Generated: {output_pdf_path} ({output_pdf_path.stat().st_size:,} bytes)")

    # Clean up temporary HTML
    try:
        temp_html.unlink()
    except Exception:
        pass

    return output_pdf_path


if __name__ == "__main__":
    base_dir = Path(__file__).resolve().parent.parent
    src_md = base_dir / "docs" / "ENGINEERING_REPORT.md"
    dst_pdf = base_dir / "docs" / "DocuPulse_Enterprise_AP_v4.2_Engineering_Report.pdf"

    try:
        out = compile_pdf(src_md, dst_pdf)
        print(f"Report exported successfully to: {out}")
    except Exception as exc:
        print(f"Error generating PDF: {exc}", file=sys.stderr)
        sys.exit(1)
