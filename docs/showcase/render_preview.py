"""Render a static, synthetic portfolio preview without starting the application.

Uses the committed Django template and CSS. No database, secrets, account,
provider request or inference is involved. Output is an inert local HTML file.
"""
import argparse
import ast
from pathlib import Path
import re

from django.conf import settings
from django.template import Context, Engine


def render(output: Path) -> None:
    root = Path(__file__).resolve().parents[2]
    source = ast.parse((root / "backend/shelf/enhancement_views.py").read_text())
    base = next(
        ast.literal_eval(node.value)
        for node in source.body
        if isinstance(node, ast.Assign)
        and any(isinstance(target, ast.Name) and target.id == "_PORTABLE_ENHANCE_BASE"
                for target in node.targets)
    )
    css = "\n".join((root / "static" / name).read_text()
                    for name in ("cabinet.css", "enhancement.css"))
    base = re.sub(r'<link rel="stylesheet"[^>]+>', "", base)
    base = base.replace("</head>", f"<style>{css}</style></head>")
    base = base.replace("<body>", '<body><div class="demo-banner">'
                        'UI preview · fictional sample data · no live AI or provider calls</div>')
    if not settings.configured:
        settings.configure(USE_I18N=False, USE_TZ=True)
    engine = Engine(dirs=[str(root / "templates")])
    categories = []
    samples = [
        ("book", "Books", [
            ("The Glass Observatory", "Fiction · invented example",
             "An illustrative connection between a reflective story and an interest in unusual places."),
            ("Notes from the River", "Nonfiction · invented example",
             "An illustrative connection between close observation, landscapes and everyday life."),
        ]),
        ("recording", "Music", [
            ("A Room Full of Echoes", "Recording · invented example",
             "A sample explanation linking quiet textures and a deliberately chosen musical preference."),
            ("Small Hours, Wide Sky", "Recording · invented example",
             "A sample explanation for a different direction to explore, without treating a suggestion as a like."),
        ]),
    ]
    for key, label, rows in samples:
        categories.append({
            "category": key, "label": label, "url": "#preview",
            "visible": True,
            "status": "Fictional fixtures for this UI preview. Not retrieved recommendations.",
            "cards": [{
                "title": title, "medium": medium,
                "provider": "Synthetic portfolio fixture",
                "facts": [], "source_url": "",
                "reasons": [{"label": "Illustrative connection", "text": reason,
                             "evidence_count": 0}],
                "feedback": {"signals": [{"signal": "interest", "value": None,
                                         "can_set": False, "can_undo": False}]},
            } for title, medium, reason in rows],
        })
    context = {
        "enhancement_base": engine.from_string(base),
        "profile_label": "A weekend of discoveries",
        "profile_ref": "fictional-preview",
        "published": True, "publication_revision": "synthetic",
        "selection_token": "inert-preview-no-authority",
        "enhance_url": "#preview",
        "feedback_available": True, "feedback_url": "#preview",
        "all_url": "#preview", "categories": categories,
    }
    html = engine.get_template("cabinet/portable_cabinet.html").render(Context(context))
    # Make the preview inert. These presentation-only changes never touch app source.
    html = re.sub(r"<script\b[^>]*>.*?</script>", "", html, flags=re.S)
    html = re.sub(r'(<a\b[^>]*\bhref=)"[^"]*"', r'\1"#preview"', html)
    html = re.sub(r"<form\b[^>]*>", '<form inert>', html)
    html = html.replace("<button ", "<button disabled ")
    html = html.replace("<button>", "<button disabled>")
    assert "<script" not in html
    assert 'href="http' not in html
    assert "<link" not in html
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(html)
    print(f"Rendered synthetic preview: {output.name}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    render(parser.parse_args().output)
