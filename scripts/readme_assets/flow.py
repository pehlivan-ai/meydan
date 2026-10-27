"""Write the animated runtime flow diagram for the README, in a light and a dark variant.

    uv run python scripts/readme_assets/flow.py

Stages are isometric blocks; message dots travel between them. Three bubbles from one
person enter the worker and leave as one reply (debounce). Animation is SMIL, which GitHub
keeps in SVGs shown through <img>.
"""

from pathlib import Path

OUT = Path(__file__).resolve().parents[2] / "docs" / "assets" / "readme"

STAGES = [
    ("Webhook", "verify, store, 200 OK"),
    ("Inbox", "once per message id"),
    ("Worker", "one turn per conversation"),
    ("Outbox", "same transaction as state"),
    ("Sender", "paced, retried, dead letter"),
]

THEMES = {
    "light": {
        "top": "#f8f5ef",
        "left": "#ece6db",
        "right": "#ddd4c5",
        "edge": "#a39a8a",
        "text": "#1f2328",
        "muted": "#59636e",
        "line": "#c9bfae",
        "msg": "#1a9e8f",
        "reply": "#d4789a",
    },
    "dark": {
        "top": "#2d323a",
        "left": "#23272e",
        "right": "#1b1e24",
        "edge": "#565d68",
        "text": "#e6edf3",
        "muted": "#9198a1",
        "line": "#3a4049",
        "msg": "#3fd0bd",
        "reply": "#e597b4",
    },
}

W, H = 960, 230
XS = [96 + i * 192 for i in range(len(STAGES))]
Y = 92  # top-face center of each block
HW, HH, DEPTH = 52, 30, 30  # half width / half height of the top rhombus, side height
LINE_Y = Y + DEPTH // 2 + 6
DUR = "4.8s"


def block(x: int, t: dict) -> str:
    top = f"{x},{Y - HH} {x + HW},{Y} {x},{Y + HH} {x - HW},{Y}"
    left = f"{x - HW},{Y} {x},{Y + HH} {x},{Y + HH + DEPTH} {x - HW},{Y + DEPTH}"
    right = f"{x + HW},{Y} {x},{Y + HH} {x},{Y + HH + DEPTH} {x + HW},{Y + DEPTH}"
    return "".join(
        f'<polygon points="{p}" fill="{t[face]}" stroke="{t["edge"]}" stroke-width="1" '
        f'stroke-linejoin="round"/>'
        for p, face in ((left, "left"), (right, "right"), (top, "top"))
    )


def dot(x0: int, x1: int, color: str, r: float, shown: tuple[float, float], delay: float) -> str:
    """A dot that moves x0 -> x1 during the `shown` fraction of the loop and is hidden otherwise."""
    a, b = shown
    return (
        f'<circle r="{r}" fill="{color}" opacity="0">'
        f'<animateMotion dur="{DUR}" begin="{delay}s" repeatCount="indefinite" calcMode="linear" '
        f'path="M{x0},{LINE_Y} L{x1},{LINE_Y}" keyPoints="0;0;1;1" keyTimes="0;{a};{b};1"/>'
        f'<animate attributeName="opacity" dur="{DUR}" begin="{delay}s" repeatCount="indefinite" '
        f'values="0;0;1;1;0;0" keyTimes="0;{a};{a + 0.01};{b - 0.01};{b};1"/>'
        "</circle>"
    )


def render(t: dict) -> str:
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" viewBox="0 0 {W} {H}" '
        "font-family=\"-apple-system, BlinkMacSystemFont, 'Segoe UI', Helvetica, Arial, "
        'sans-serif">',
        f'<line x1="{XS[0] - 70}" y1="{LINE_Y}" x2="{XS[-1] + 70}" y2="{LINE_Y}" '
        f'stroke="{t["line"]}" stroke-width="2" stroke-dasharray="2 6" stroke-linecap="round"/>',
    ]
    for x, (name, note) in zip(XS, STAGES, strict=True):
        parts.append(block(x, t))
        parts.append(
            f'<text x="{x}" y="{Y + HH + DEPTH + 30}" text-anchor="middle" font-size="15" '
            f'font-weight="600" fill="{t["text"]}">{name}</text>'
            f'<text x="{x}" y="{Y + HH + DEPTH + 50}" text-anchor="middle" font-size="12" '
            f'fill="{t["muted"]}">{note}</text>'
        )
    # Drawn over the blocks: a burst of three messages from one person reaches the worker...
    for i in range(3):
        parts.append(dot(XS[0] - 70, XS[2], t["msg"], 5, (0.0, 0.4), i * 0.45))
    # ...and one reply leaves it.
    parts.append(dot(XS[2], XS[-1] + 70, t["reply"], 7, (0.5, 0.92), 0.9))
    parts.append("</svg>")
    return "\n".join(parts) + "\n"


if __name__ == "__main__":
    for name, theme in THEMES.items():
        path = OUT / f"flow-{name}.svg"
        path.write_text(render(theme))
        print(path.relative_to(OUT.parents[2]))
