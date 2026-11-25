"""Try a bridge from the command line: make credentials, or be the other side and send an event.

    python -m meydan.bridge secret                 # a whsec_ shared secret (v1)
    python -m meydan.bridge keypair                # a whsk_ signing key and its whpk_ public key
    python -m meydan.bridge send http://localhost:8000/connections/acme/webhooks \\
        --type payment.succeeded --data '{"ref": "..."}'

`send` signs with `--secret` / `$MEYDAN_BRIDGE_SECRET` (v1) and/or `--signing-key` /
`$MEYDAN_BRIDGE_SIGNING_KEY` with `--endpoint-id` (v2a); prefer the environment, so secrets stay
out of shell history. It prints the answer's status and body. `--id` repeats an event id (to see
a repeat acknowledged once).
"""

import argparse
import json
import os
import sys
from datetime import UTC, datetime

from meydan.bridge import headers, new_keypair, new_secret


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m meydan.bridge", description=__doc__.split("\n")[0])
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("secret", help="print a new whsec_ shared secret")
    sub.add_parser("keypair", help="print a new whsk_ signing key and its whpk_ public key")
    s = sub.add_parser("send", help="sign and POST one event, as the other side would")
    s.add_argument("url")
    s.add_argument("--type", required=True, help="the event type, e.g. payment.succeeded")
    s.add_argument("--data", default="{}", help="the event's data, a JSON object")
    s.add_argument("--id", help="the webhook-id (default: a new one)")
    s.add_argument("--secret", default=os.environ.get("MEYDAN_BRIDGE_SECRET"))
    s.add_argument("--signing-key", default=os.environ.get("MEYDAN_BRIDGE_SIGNING_KEY"))
    s.add_argument("--endpoint-id", default=os.environ.get("MEYDAN_BRIDGE_ENDPOINT_ID"))
    s.add_argument("--insecure", action="store_true", help="accept a self-signed certificate")
    args = p.parse_args(argv)

    if args.command == "secret":
        print(new_secret())
        return 0
    if args.command == "keypair":
        signing, public = new_keypair()
        print(f"signing key (keep, the sender's): {signing}")
        print(f"public key (give to the receiver): {public}")
        return 0

    import httpx

    try:
        data = json.loads(args.data)
    except ValueError:
        p.error("--data is not JSON")
    if not isinstance(data, dict):
        p.error("--data is a JSON object")
    payload = {"type": args.type, "timestamp": datetime.now(UTC).isoformat(), "data": data}
    body = json.dumps(payload, separators=(",", ":")).encode()
    try:
        head = headers(args.secret, body, event_id=args.id, signing_key=args.signing_key,
                       endpoint_id=args.endpoint_id)  # fmt: skip
    except ValueError as e:
        p.error(str(e))
    head["content-type"] = "application/json"
    r = httpx.post(args.url, content=body, headers=head, timeout=30, verify=not args.insecure)
    print(f"{r.status_code} {r.text}")
    print(f"webhook-id: {head['webhook-id']}", file=sys.stderr)
    return 0 if r.is_success else 1


if __name__ == "__main__":
    sys.exit(main())
