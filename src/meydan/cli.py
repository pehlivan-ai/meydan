"""meydan's operator CLI: our own numbers and email addresses.

    python -m meydan.cli numbers list --account WABA_ID
    python -m meydan.cli numbers add --account WABA_ID --cc 90 --number 5551234567 --name "Brand"
    python -m meydan.cli numbers request-code PHONE_NUMBER_ID [--voice] [--language tr]
    python -m meydan.cli numbers verify PHONE_NUMBER_ID CODE
    python -m meydan.cli numbers register PHONE_NUMBER_ID          # asks for the 6-digit PIN
    python -m meydan.cli numbers profile PHONE_NUMBER_ID [--about ... --website ... ...]
    python -m meydan.cli numbers copy-profile FROM_ID TO_ID
    python -m meydan.cli email check hello@example.com --selector google [--nameserver 1.1.1.1]

Numbers need a token with whatsapp_business_management in WHATSAPP_ACCESS_TOKEN (never printed).
Bringing a number in: add -> request-code -> verify -> register -> profile; then subscribe the
app to the account's webhooks if it isn't yet, and sync it into the app (endpoints.sync).
"""

import argparse
import asyncio
import getpass
import os
import sys
from collections.abc import Sequence

from meydan.channels.base import EndpointUpdate
from meydan.runtime.endpoints import checks_of, worst

PROFILE_FIELDS = ("about", "address", "description", "email", "vertical")


def _show(u: EndpointUpdate) -> None:
    checks = checks_of(u.details)
    display = u.display if u.display != u.address.value else ""
    head = [u.address.value, display or "", u.name or "", u.status or "", u.quality or ""]
    if u.limit:
        head.append(u.limit)
    print(f"[{worst(checks) or '?'}] " + "  ".join(h for h in head if h))
    for c in checks:
        if c.level != "ok":
            print(f"    {c.level:4} {c.name}: {c.detail}")


def _cloud(args: argparse.Namespace):
    from meydan.channels.whatsapp import WhatsAppCloud

    token = os.environ.get("WHATSAPP_ACCESS_TOKEN", "")
    if not token:
        sys.exit("WHATSAPP_ACCESS_TOKEN is not set")
    return WhatsAppCloud(token, api_version=args.api_version)


async def _numbers(args: argparse.Namespace) -> None:
    cloud = _cloud(args)
    try:
        if args.cmd == "list":
            for u in await cloud.list_numbers(args.account):
                _show(u)
        elif args.cmd == "add":
            number = "".join(ch for ch in args.number if ch.isdigit())
            new_id = await cloud.add_number(args.account, args.cc.lstrip("+"), number, args.name)
            print(f"added: {new_id}  next: request-code {new_id}")
        elif args.cmd == "request-code":
            await cloud.request_code(args.id, "VOICE" if args.voice else "SMS", args.language)
            print(f"code sent by {'voice call' if args.voice else 'SMS'}: next: verify {args.id}")
        elif args.cmd == "verify":
            await cloud.verify_code(args.id, args.code)
            print(f"verified: next: register {args.id}")
        elif args.cmd == "register":
            pin = os.environ.get("WHATSAPP_PIN") or getpass.getpass("6-digit PIN (keep it): ")
            if not (len(pin) == 6 and pin.isdigit()):
                sys.exit("the PIN is six digits")
            await cloud.register(args.id, pin)
            print("registered on the Cloud API")
        elif args.cmd == "profile":
            fields = {f: getattr(args, f) for f in PROFILE_FIELDS if getattr(args, f)}
            if args.website:
                fields["websites"] = args.website
            if fields:
                await cloud.set_profile(args.id, **fields)
            for key, value in (await cloud.profile(args.id)).items():
                print(f"{key}: {value}")
        elif args.cmd == "copy-profile":
            source = await cloud.profile(args.source)
            fields = {k: v for k, v in source.items() if k in (*PROFILE_FIELDS, "websites") and v}
            await cloud.set_profile(args.target, **fields)
            print("copied: " + ", ".join(sorted(fields)))
    finally:
        await cloud.aclose()


def _email(args: argparse.Namespace) -> None:
    from meydan.channels.email import dns_health, dns_lookup

    for address in args.address:
        _show(dns_health(address, selectors=args.selector, lookup=dns_lookup(args.nameserver)))


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m meydan.cli", description=__doc__.split("\n")[0])
    p.add_argument("--api-version", default="v25.0")
    top = p.add_subparsers(dest="area", required=True)

    numbers = top.add_parser("numbers", help="WhatsApp numbers of a business account")
    n = numbers.add_subparsers(dest="cmd", required=True)
    n.add_parser("list").add_argument("--account", required=True, help="WABA id")
    add = n.add_parser("add")
    add.add_argument("--account", required=True, help="WABA id")
    add.add_argument("--cc", required=True, help="country code, e.g. 90")
    add.add_argument("--number", required=True, help="national number, e.g. 5551234567")
    add.add_argument("--name", required=True, help="display name (reviewed by the channel)")
    code = n.add_parser("request-code")
    code.add_argument("id")
    code.add_argument("--voice", action="store_true", help="a call instead of SMS (landlines)")
    code.add_argument("--language", default="en_US")
    verify = n.add_parser("verify")
    verify.add_argument("id")
    verify.add_argument("code")
    n.add_parser("register").add_argument("id")
    profile = n.add_parser("profile")
    profile.add_argument("id")
    for f in PROFILE_FIELDS:
        profile.add_argument(f"--{f}")
    profile.add_argument("--website", action="append", help="repeat for two")
    copy = n.add_parser("copy-profile")
    copy.add_argument("source")
    copy.add_argument("target")

    email = top.add_parser("email", help="our email addresses")
    e = email.add_subparsers(dest="cmd", required=True)
    check = e.add_parser("check", help="MX, SPF, DKIM, DMARC of each address's domain")
    check.add_argument("address", nargs="+")
    check.add_argument("--selector", action="append", default=[], help="DKIM selector")
    check.add_argument("--nameserver", help="ask this DNS server (e.g. behind a VPN)")
    return p


def main(argv: Sequence[str] | None = None) -> None:
    args = parser().parse_args(argv)
    if args.area == "numbers":
        from meydan.channels.whatsapp import GraphError

        try:
            asyncio.run(_numbers(args))
        except GraphError as e:
            sys.exit(f"Graph API: {e}")
    else:
        _email(args)


if __name__ == "__main__":
    main()
