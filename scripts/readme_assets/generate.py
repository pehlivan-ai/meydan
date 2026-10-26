# /// script
# requires-python = ">=3.12"
# dependencies = ["httpx>=0.28", "pillow>=11"]
# ///
"""Render the README visuals described in prompts.toml through OpenRouter's image API.

    uv run scripts/readme_assets/generate.py list
    uv run scripts/readme_assets/generate.py generate hero-light -n 2
    uv run scripts/readme_assets/generate.py promote hero-light <chosen.png>

`generate` writes candidates (and a JSON sidecar with prompt, model and cost) to the
gitignored .cache/readme_assets/; `promote` resizes the chosen one into docs/assets/readme/,
which is what the README references and what gets committed. Needs OPENROUTER_API_KEY.
"""

import argparse
import base64
import io
import json
import mimetypes
import os
import sys
import time
import tomllib
from pathlib import Path

import httpx
from PIL import Image

ROOT = Path(__file__).resolve().parents[2]
PROMPTS = Path(__file__).with_name("prompts.toml")
CACHE = ROOT / ".cache" / "readme_assets"
OUT = ROOT / "docs" / "assets" / "readme"
API = "https://openrouter.ai/api/v1/images"
PASSTHROUGH = ("aspect_ratio", "quality", "background", "resolution", "output_format")


def load() -> tuple[str, dict[str, dict]]:
    spec = tomllib.loads(PROMPTS.read_text())
    return spec["style"]["text"].strip(), spec["assets"]


def data_url(path: Path) -> str:
    mime = mimetypes.guess_type(path.name)[0] or "image/png"
    return f"data:{mime};base64,{base64.b64encode(path.read_bytes()).decode()}"


def generate(name: str, n: int, model: str | None) -> None:
    style, assets = load()
    asset = assets[name]
    body = {
        "model": model or asset["model"],
        "prompt": f"{style}\n\n{asset['prompt'].strip()}",
        "n": n,
        **{k: asset[k] for k in PASSTHROUGH if k in asset},
    }
    refs = [OUT / f"{ref}.webp" for ref in asset.get("references", [])]
    if missing := [str(r) for r in refs if not r.exists()]:
        sys.exit(f"promote these references first: {', '.join(missing)}")
    if refs:
        body["input_references"] = [
            {"type": "image_url", "image_url": {"url": data_url(r)}} for r in refs
        ]

    headers = {"Authorization": f"Bearer {os.environ['OPENROUTER_API_KEY']}"}
    resp = httpx.post(API, json=body, headers=headers, timeout=600)
    if resp.is_error:
        sys.exit(f"{resp.status_code}: {resp.text[:2000]}")
    payload = resp.json()

    folder = CACHE / name
    folder.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S") + f"-{os.getpid()}"
    slug = body["model"].split("/")[-1]
    for i, item in enumerate(payload["data"]):
        ext = mimetypes.guess_extension(item.get("media_type") or "image/png") or ".png"
        path = folder / f"{stamp}-{slug}-{i}{ext}"
        path.write_bytes(base64.b64decode(item["b64_json"]))
        print(path.relative_to(ROOT))
    meta = {k: v for k, v in body.items() if k != "input_references"}
    meta["references"] = asset.get("references", [])
    meta["usage"] = payload.get("usage")
    (folder / f"{stamp}-{slug}.json").write_text(json.dumps(meta, indent=2))
    print("usage:", payload.get("usage"))


def promote(name: str, source: Path) -> None:
    _, assets = load()
    width, height = assets[name]["size"]
    image = Image.open(source)
    image = image.convert("RGBA" if image.mode in ("RGBA", "LA", "P") else "RGB")
    image.thumbnail((width, height), Image.Resampling.LANCZOS)
    OUT.mkdir(parents=True, exist_ok=True)
    target = OUT / f"{name}.webp"
    buf = io.BytesIO()
    image.save(buf, "WEBP", quality=90, method=6)
    target.write_bytes(buf.getvalue())
    kb = len(buf.getvalue()) // 1024
    print(f"{target.relative_to(ROOT)} {image.size[0]}x{image.size[1]} {kb} KB")


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("list")
    gen = sub.add_parser("generate")
    gen.add_argument("name")
    gen.add_argument("-n", type=int, default=1)
    gen.add_argument("--model", help="override the model from prompts.toml")
    pro = sub.add_parser("promote")
    pro.add_argument("name")
    pro.add_argument("source", type=Path)
    args = parser.parse_args()

    if args.command == "list":
        _, assets = load()
        for name, asset in assets.items():
            done = (OUT / f"{name}.webp").exists()
            print(f"{'✓' if done else '·'} {name:16} {asset['model']}")
    elif args.command == "generate":
        generate(args.name, args.n, args.model)
    else:
        promote(args.name, args.source)


if __name__ == "__main__":
    main()
