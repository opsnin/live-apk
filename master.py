#!/usr/bin/env python3
"""
mkmaster.py — DishHome Go HLS master rewriter + playlist updater

Interactive flow:
  1. Paste your account Bearer JWT (hidden input).
  2. Paste a stream/content ID for each channel.
For each ID it resolves the hdnea-signed master via the playback API, fetches it,
keeps one audio language (default eng) + one video rung (default highest), writes a
standalone <title>.m3u8, and then refreshes the auto-managed block in playlist.m3u
with an IPTV entry per file (pointing at your GitHub raw URL).

Re-running replaces ONLY the block the script manages (fenced by marker comments);
your hand-added channels are left alone.

Flag mode (for cron / one-offs) is still available — see --help.
"""

import argparse
import base64
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from urllib.parse import urljoin

HOST = "https://livemedia-web.dishhomego.com.np"
PLAYBACK_HOST = "https://ent.dishhomego.com.np"

# Defaults for the IPTV entries written into playlist.m3u
RAW_BASE_DEFAULT = "https://raw.githubusercontent.com/opsnin/live-apk/refs/heads/main"
GROUP_DEFAULT = "Sports"
REFERER = "https://www.watchdgo.com/"
ORIGIN = "https://www.watchdgo.com"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36")

# Fence for the auto-managed section of playlist.m3u
MARK_START = "#--- mkmaster auto block START (do not edit by hand) ---"
MARK_END = "#--- mkmaster auto block END ---"

DEFAULT_HEADERS = {
    "accept": "*/*",
    "accept-language": "en-US,en;q=0.9",
    "cache-control": "no-cache",
    "origin": ORIGIN,
    "pragma": "no-cache",
    "referer": REFERER,
    "user-agent": UA,
}

ATTR_RE = re.compile(r'([A-Z0-9-]+)=("[^"]*"|[^,]*)')


def parse_attrs(line):
    body = line.split(":", 1)[1] if ":" in line else ""
    return {m.group(1): m.group(2) for m in ATTR_RE.finditer(body)}


def fetch_master(url, deviceid=None):
    headers = dict(DEFAULT_HEADERS)
    if deviceid:
        headers["deviceid"] = deviceid
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=20) as r:
        return r.read().decode("utf-8", "replace")


def build_master_url(channel, slug, token):
    return f"{HOST}/hls/live/{channel}/{slug}/master.m3u8?hdnea={token}"


def _clean_jwt(s):
    """Strip quotes / 'Bearer ' / 'authorization:' that often ride along on paste."""
    s = s.strip().strip('"').strip("'").strip()
    if s.lower().startswith("authorization:"):
        s = s.split(":", 1)[1].strip()
    if s.lower().startswith("bearer "):
        s = s[7:].strip()
    return s


def _jwt_payload(jwt):
    """Best-effort decode of a JWT payload (no signature check). Returns dict or None."""
    try:
        part = jwt.split(".")[1]
        part += "=" * (-len(part) % 4)
        return json.loads(base64.urlsafe_b64decode(part))
    except Exception:
        return None


def resolve_playback(content_id, jwt, deviceid=None):
    """POST the playback endpoint with the account JWT; return (master_url, title)."""
    url = f"{PLAYBACK_HOST}/dhome/web-app/playback/{content_id}"
    headers = dict(DEFAULT_HEADERS)
    headers["accept"] = "application/json"
    headers["content-type"] = "application/json"
    headers["securitylevel"] = "SW"
    headers["authorization"] = f"Bearer {jwt}"
    if deviceid:
        headers["deviceid"] = deviceid
    req = urllib.request.Request(url, data=b"{}", headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            resp = json.loads(r.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", "replace")
        except Exception:
            pass
        hint = ""
        if e.code == 401:
            hint = " (token expired/invalid, or pasted with extra characters)"
        raise RuntimeError(f"playback HTTP {e.code}{hint}: {body[:300] or e.reason}")
    except urllib.error.URLError as e:
        raise RuntimeError(f"network error reaching playback API: {e.reason}")

    if resp.get("access") and resp["access"] != "allow":
        raise RuntimeError(f"playback access denied: access={resp['access']!r}")

    pd = resp.get("playbackDetails", {})
    playurls = pd.get("playurls", [])
    for u in playurls:
        if u.get("streaming_format") == "HLS" and u.get("type") == "video":
            return u["url"], resp.get("title")
    if playurls:
        return playurls[0]["url"], resp.get("title")
    raise RuntimeError("no playurls in playback response")


def rewrite(master_text, base_url, lang="eng", quality="highest"):
    lines = master_text.replace("\r\n", "\n").split("\n")
    audio_tracks, variants = [], []

    i = 0
    while i < len(lines):
        line = lines[i].strip()
        if line.startswith("#EXT-X-MEDIA:") and 'TYPE=AUDIO' in line:
            audio_tracks.append((parse_attrs(line), line))
        elif line.startswith("#EXT-X-STREAM-INF:"):
            uri = ""
            j = i + 1
            while j < len(lines):
                cand = lines[j].strip()
                if cand and not cand.startswith("#"):
                    uri = cand
                    break
                j += 1
            variants.append((parse_attrs(line), line, uri))
            i = j
        i += 1

    chosen_audio = None
    if audio_tracks:
        want = lang.lower()
        for attrs, raw in audio_tracks:
            if attrs.get("LANGUAGE", "").strip('"').lower() == want:
                chosen_audio = (attrs, raw)
                break
        if chosen_audio is None:
            sys.stderr.write(
                f"warning: no audio LANGUAGE={lang!r}; available: "
                f"{[a.get('LANGUAGE') for a, _ in audio_tracks]}. Keeping first.\n")
            chosen_audio = audio_tracks[0]

    def res_area(attrs):
        m = re.match(r'(\d+)x(\d+)', attrs.get("RESOLUTION", "0x0"))
        return int(m.group(1)) * int(m.group(2)) if m else 0

    if not variants:
        raise SystemExit("error: no #EXT-X-STREAM-INF variants found in master")

    if quality == "highest":
        chosen_variant = max(variants, key=lambda v: res_area(v[0]))
    elif quality == "lowest":
        chosen_variant = min(variants, key=lambda v: res_area(v[0]))
    else:
        chosen_variant = next((v for v in variants if v[0].get("RESOLUTION") == quality), None)
        if chosen_variant is None:
            avail = [v[0].get("RESOLUTION") for v in variants]
            raise SystemExit(f"error: no variant RESOLUTION={quality}; available: {avail}")

    out = ["#EXTM3U", "#EXT-X-VERSION:7", "#EXT-X-INDEPENDENT-SEGMENTS"]
    if chosen_audio:
        attrs, _ = chosen_audio
        abs_uri = urljoin(base_url, attrs.get("URI", '""').strip('"'))
        group = attrs.get("GROUP-ID", '"Audio"').strip('"')
        name = attrs.get("NAME", f'"{lang}"').strip('"')
        chans = attrs.get("CHANNELS", '"2"').strip('"')
        language = attrs.get("LANGUAGE", f'"{lang}"').strip('"')
        out.append(
            f'#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="{group}",NAME="{name}",'
            f'DEFAULT=YES,AUTOSELECT=YES,CHANNELS="{chans}",'
            f'LANGUAGE="{language}",URI="{abs_uri}"')
    _, v_inf, v_uri = chosen_variant
    out.append(v_inf)
    out.append(urljoin(base_url, v_uri))
    return "\n".join(out) + "\n"


def _safe_filename(title, fallback):
    if not title:
        return f"{fallback}.m3u8"
    name = re.sub(r"[^\w\s-]", "", title).strip().lower()
    name = re.sub(r"\s+", "_", name)
    return f"{name or fallback}.m3u8"


def _slugify(title, fallback):
    s = re.sub(r"[^\w\s-]", "", (title or fallback)).strip().lower()
    return re.sub(r"\s+", "-", s) or fallback


def make_iptv_entry(title, filename, raw_base, deviceid, group=GROUP_DEFAULT):
    """Build one multi-line IPTV entry for playlist.m3u."""
    name = title or filename
    tvgid = _slugify(title, filename.replace(".m3u8", ""))
    http_headers = json.dumps({
        "Referer": REFERER,
        "Origin": ORIGIN,
        "User-Agent": UA,
        "deviceid": deviceid,
    }, separators=(",", ":"))
    url = f"{raw_base.rstrip('/')}/{filename}"
    return "\n".join([
        f'#EXTINF:-1 tvg-id="{tvgid}" tvg-name="{name}" group-title="{group}",{name}',
        f'#EXTVLCOPT:http-referrer={REFERER}',
        f'#EXTVLCOPT:http-user-agent={UA}',
        f'#EXTHTTP:{http_headers}',
        url,
    ])


def update_playlist(playlist_path, entries):
    """Replace the fenced auto block in playlist_path with `entries` (list of str).

    Everything outside the markers is preserved. If the file or markers don't
    exist yet, they're created (block appended at the end after #EXTM3U).
    """
    if os.path.exists(playlist_path):
        with open(playlist_path, "r", encoding="utf-8") as f:
            text = f.read()
    else:
        text = "#EXTM3U\n"

    if not text.lstrip().startswith("#EXTM3U"):
        text = "#EXTM3U\n" + text

    block = "\n".join([MARK_START] + entries + [MARK_END]) + "\n"

    if MARK_START in text and MARK_END in text:
        pre = text.split(MARK_START, 1)[0].rstrip("\n")
        post = text.split(MARK_END, 1)[1].lstrip("\n")
        parts = [pre, block.rstrip("\n")]
        if post.strip():
            parts.append(post)
        new_text = "\n".join(parts).rstrip("\n") + "\n"
    else:
        new_text = text.rstrip("\n") + "\n\n" + block

    with open(playlist_path, "w", encoding="utf-8") as f:
        f.write(new_text)


def git_push(directory, message):
    try:
        subprocess.run(["git", "-C", directory, "add", "-A"], check=True)
        subprocess.run(["git", "-C", directory, "commit", "-m", message], check=True)
        subprocess.run(["git", "-C", directory, "push"], check=True)
        print("pushed to GitHub.")
    except Exception as e:
        print(f"git push failed: {e}\nCommit & push manually when ready.")


def interactive():
    import getpass

    print("DishHome Go -> demuxed m3u8 + playlist updater")
    print("(Ctrl-C to quit)\n")

    jwt = getpass.getpass("Paste Bearer JWT (input hidden): ").strip()
    jwt = _clean_jwt(jwt)
    if not jwt:
        raise SystemExit("no JWT given")

    # Decode the token locally and warn if it's already expired (common cause of 401).
    info = _jwt_payload(jwt)
    if info is None:
        print("  note: couldn't decode that as a JWT — check you pasted the whole token.")
    else:
        exp = info.get("exp")
        who = info.get("subscribername") or info.get("email") or info.get("subscriberid")
        if exp:
            left = exp - time.time()
            from datetime import datetime
            when = datetime.fromtimestamp(exp).strftime("%Y-%m-%d %H:%M")
            if left <= 0:
                print(f"  WARNING: token EXPIRED at {when} ({-left/3600:.1f}h ago). "
                      "Grab a fresh one or every call will 401.")
            else:
                print(f"  token ok: {who}, expires {when} (in {left/3600:.1f}h)")

    lang = input("Audio language [eng]: ").strip() or "eng"
    quality = input("Quality (highest / lowest / WxH) [highest]: ").strip() or "highest"
    deviceid = input("deviceid [mq7n3pi3-n63on]: ").strip() or "mq7n3pi3-n63on"
    playlist = input("Playlist file [playlist.m3u]: ").strip() or "playlist.m3u"
    raw_base = input(f"Raw URL base [{RAW_BASE_DEFAULT}]: ").strip() or RAW_BASE_DEFAULT
    group = input(f"group-title [{GROUP_DEFAULT}]: ").strip() or GROUP_DEFAULT

    print("\nPaste a stream/content ID for each channel. Blank line = done.\n")
    entries, made = [], []
    while True:
        try:
            cid = input("Content ID: ").strip()
        except EOFError:
            break
        if not cid:
            break
        try:
            url, title = resolve_playback(cid, jwt, deviceid)
            if title:
                print(f"  -> {title}")
            text = fetch_master(url, deviceid)
            result = rewrite(text, url, lang=lang, quality=quality)
            outname = _safe_filename(title, cid)
            with open(outname, "w", encoding="utf-8") as f:
                f.write(result)
            entries.append(make_iptv_entry(title, outname, raw_base, deviceid, group))
            made.append(outname)
            print(f"  wrote {os.path.abspath(outname)}\n")
        except Exception as e:
            print(f"  FAILED for {cid}: {e}\n")

    if not entries:
        print("nothing built; playlist unchanged.")
        return

    update_playlist(playlist, entries)
    print(f"updated {os.path.abspath(playlist)} "
          f"({len(entries)} entr{'y' if len(entries)==1 else 'ies'} in auto block).")

    ans = input("\nCommit & push to GitHub now? [y/N]: ").strip().lower()
    if ans == "y":
        directory = os.path.dirname(os.path.abspath(playlist)) or "."
        from datetime import datetime
        git_push(directory, f"update live playlist {datetime.now():%Y-%m-%d %H:%M}")
    else:
        print("Skipped. Remember: the raw URLs only resolve after you push these files.")


def main():
    ap = argparse.ArgumentParser(description="DishHome Go HLS master rewriter + playlist updater")
    src = ap.add_mutually_exclusive_group(required=False)
    src.add_argument("--content-id", dest="content_id",
                     help="playback content id (needs a JWT). If it starts with '-', use --content-id=-LJL...")
    src.add_argument("--url", help="full master.m3u8 URL (with hdnea)")
    src.add_argument("--file", help="read master playlist from a saved file")
    src.add_argument("--channel", help="channel id, e.g. 20000917 (needs --slug, --token)")
    ap.add_argument("--slug", help="channel slug, e.g. FIFA56")
    ap.add_argument("--token", help="hdnea token value (everything after hdnea=)")
    ap.add_argument("--jwt", help="account Bearer JWT (for --content-id)")
    ap.add_argument("--jwt-file", help="file containing the Bearer JWT")
    ap.add_argument("--lang", default="eng", help="audio LANGUAGE to keep (default: eng)")
    ap.add_argument("--quality", default="highest", help="'highest' (default), 'lowest', or WxH")
    ap.add_argument("--deviceid", default="mq7n3pi3-n63on", help="deviceid header")
    ap.add_argument("-o", "--output", help="output .m3u8 file (default: stdout)")
    args = ap.parse_args()

    if not (args.content_id or args.url or args.file or args.channel):
        interactive()
        return

    if args.content_id:
        jwt = args.jwt or os.environ.get("DGO_JWT")
        if not jwt and args.jwt_file:
            with open(args.jwt_file, "r", encoding="utf-8") as f:
                jwt = f.read().strip()
        if not jwt:
            ap.error("--content-id requires a JWT via --jwt, --jwt-file, or DGO_JWT")
        jwt = _clean_jwt(jwt)
        url, title = resolve_playback(args.content_id, jwt, args.deviceid)
        if title:
            sys.stderr.write(f"resolved: {title}\n")
        base = url
        text = fetch_master(url, args.deviceid)
    elif args.channel:
        if not (args.slug and args.token):
            ap.error("--channel requires --slug and --token")
        url = build_master_url(args.channel, args.slug, args.token)
        base = url
        text = fetch_master(url, args.deviceid)
    elif args.url:
        base = args.url
        text = fetch_master(args.url, args.deviceid)
    else:
        with open(args.file, "r", encoding="utf-8") as f:
            text = f.read()
        m = re.search(r'(https://[^\s"]+/master\.m3u8[^\s"]*)', text)
        if not m:
            ap.error("--file needs the master to contain its own URL, or use --url")
        base = m.group(1)

    result = rewrite(text, base, lang=args.lang, quality=args.quality)
    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(result)
        sys.stderr.write(f"wrote {args.output}\n")
    else:
        sys.stdout.write(result)


if __name__ == "__main__":
    main()