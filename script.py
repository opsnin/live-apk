#!/usr/bin/env python3
"""
clearkey.py — convert hex ClearKey kid:key into the base64url form used by
inputstream.adaptive (Kodi / StreamVault / TiviMate) ClearKey license_key JSON.

Usage:
    # from a hex kid:key pair (the --key format from key dumpers)
    python3 clearkey.py --key 74d126f8677f414498f59367e41a3d28:9da339ee59d23b3027b65dfcfbdcdd93

    # multiple keys
    python3 clearkey.py --key KID1:K1 --key KID2:K2

    # also decode a PSSH and verify the KID matches the supplied key
    python3 clearkey.py --key KID:K --pssh AAAAMnBzc2gAAAAA7e+LqXnWSs6jyCfc1R0h7Q...

    # PSSH only (just pull the KID out, no key to pair)
    python3 clearkey.py --pssh AAAAMnBzc2gAAAAA...
"""
import argparse
import base64
import json
import sys

CLEARKEY_SYSTEM_ID = "edef8ba979d64acea3c827dcd51d21ed"


def hex_to_b64url(hexstr: str) -> str:
    hexstr = hexstr.strip().replace("-", "").lower()
    return base64.urlsafe_b64encode(bytes.fromhex(hexstr)).decode().rstrip("=")


def parse_key(arg: str):
    if ":" not in arg:
        sys.exit(f"error: --key must be in KID:KEY hex form, got: {arg}")
    kid_hex, k_hex = arg.split(":", 1)
    kid_hex = kid_hex.strip().replace("-", "").lower()
    k_hex = k_hex.strip().replace("-", "").lower()
    if len(kid_hex) != 32 or len(k_hex) != 32:
        sys.exit(f"error: KID and KEY must each be 32 hex chars (16 bytes): {arg}")
    return kid_hex, k_hex


def decode_pssh(pssh_b64: str):
    """Return (system_id_hex, [kid_hex,...]) for a ClearKey/CENC PSSH box."""
    raw = base64.b64decode(pssh_b64.strip())
    if raw[4:8] != b"pssh":
        sys.exit("error: not a PSSH box (missing 'pssh' signature)")
    system_id = raw[12:28].hex()
    version = raw[8]
    kids = []
    if version > 0:
        # v1 carries a KID count + KID list before the data field
        kid_count = int.from_bytes(raw[28:32], "big")
        off = 32
        for _ in range(kid_count):
            kids.append(raw[off:off + 16].hex())
            off += 16
    else:
        # v0: data field is system-specific. For ClearKey it's commonly the
        # 16-byte KID; grab the trailing 16 bytes of the data as a best effort.
        data_size = int.from_bytes(raw[28:32], "big")
        data = raw[32:32 + data_size]
        if len(data) >= 16:
            kids.append(data[-16:].hex())
    return system_id, kids


def main():
    ap = argparse.ArgumentParser(description="ClearKey hex -> base64url converter")
    ap.add_argument("--key", action="append", default=[],
                    help="hex KID:KEY pair (repeatable)")
    ap.add_argument("--pssh", help="base64 PSSH box to decode/verify")
    ap.add_argument("--json-only", action="store_true",
                    help="print only the license_key JSON line")
    args = ap.parse_args()

    if not args.key and not args.pssh:
        ap.error("provide at least --key or --pssh")

    pssh_kids = []
    if args.pssh:
        system_id, pssh_kids = decode_pssh(args.pssh)
        if not args.json_only:
            ck = " (ClearKey)" if system_id == CLEARKEY_SYSTEM_ID else ""
            print(f"PSSH system id : {system_id}{ck}")
            for k in pssh_kids:
                print(f"PSSH KID       : {k}")
            print()

    keys_json = []
    for raw in args.key:
        kid_hex, k_hex = parse_key(raw)
        kid_b64 = hex_to_b64url(kid_hex)
        k_b64 = hex_to_b64url(k_hex)
        keys_json.append({"kty": "oct", "kid": kid_b64, "k": k_b64})
        if not args.json_only:
            match = ""
            if pssh_kids:
                match = "  [matches PSSH]" if kid_hex in pssh_kids else "  [!! NOT in PSSH]"
            print(f"kid hex : {kid_hex}")
            print(f"k   hex : {k_hex}")
            print(f"kid b64 : {kid_b64}{match}")
            print(f"k   b64 : {k_b64}")
            print()

    if keys_json:
        license_key = json.dumps({"keys": keys_json, "type": "temporary"},
                                 separators=(",", ":"))
        line = f"#KODIPROP:inputstream.adaptive.license_key={license_key}"
        if args.json_only:
            print(line)
        else:
            print("Paste into your M3U entry:")
            print(line)


if __name__ == "__main__":
    main()
