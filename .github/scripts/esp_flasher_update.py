#!/usr/bin/env python3
"""Update the ESP firmware images bundled with ESP Flasher.

Wardriver is not updated here: its last upstream release (2023) is older than
the bundled build.
"""
import argparse
import hashlib
import io
import json
import os
import re
import subprocess
import tarfile
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ASSETS = ROOT / "esp_flasher/resources/apps_data/esp_flasher/assets"
VERSIONS = ROOT / ".github/esp_flasher_versions.json"

CHIP_IDS = {"esp32": 0, "esp32s2": 2, "esp32s3": 9}

# Marauder release asset suffix: (destination under ASSETS, chip)
MARAUDER_FILES = {
    "flipper": ("marauder/s2/esp32_marauder.flipper.bin", "esp32s2"),
    "multiboardS3": ("marauder/s3/esp32_marauder.multiboardS3.bin", "esp32s3"),
    "marauder_dev_board_pro": ("marauder/wroom/esp32_marauder.dev_board_pro.bin", "esp32"),
}

# FlipperHTTP file: chip (None marks a partition table)
FLIPPERHTTP_FILES = {
    "flipper_http_bootloader.bin": "esp32s2",
    "flipper_http_firmware_a.bin": "esp32s2",
    "flipper_http_partitions.bin": None,
}

# Black Magic archive member: chip (None marks a partition table)
BLACKMAGIC_FILES = {
    "blackmagic.bin": "esp32s2",
    "bootloader.bin": "esp32s2",
    "partition-table.bin": None,
}


class Refused(Exception):
    pass


def fetch(url, token=None):
    request = urllib.request.Request(url, headers={"User-Agent": "esp-flasher-update"})
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(request, timeout=60) as response:
        return response.read()


def api(path):
    return json.loads(fetch(f"https://api.github.com/{path}", os.environ.get("GITHUB_TOKEN")))


def version_key(version):
    match = re.fullmatch(r"v?(\d+(?:\.\d+)*)(-.+)?", version)
    if not match:
        raise ValueError(f"unrecognized version {version!r}")
    numbers = tuple(int(n) for n in match[1].split("."))
    return numbers, match[2] is None  # a prerelease sorts below its stable release


def validate(data, chip):
    if len(data) < 16:
        return "file too short"
    if chip is None:
        return None if data[:2] == b"\xaa\x50" else "not a partition table (expected AA 50)"
    if data[0] != 0xE9:
        return "not an ESP image (expected E9 as first byte)"
    chip_id = int.from_bytes(data[12:14], "little")
    if chip_id != CHIP_IDS[chip]:
        return f"chip ID {chip_id}, expected {CHIP_IDS[chip]} for {chip}"
    return None


# Each source returns (version, release URL, author, download function).
# download() returns {destination under ASSETS: (data, chip)}.


def marauder():
    release = api("repos/justcallmekoko/ESP32Marauder/releases/latest")
    urls = {a["name"]: a["browser_download_url"] for a in release["assets"]}

    def download():
        files = {}
        for suffix, (dest, chip) in MARAUDER_FILES.items():
            pattern = re.compile(rf"esp32_marauder_v[\d_]+_\d{{8}}_{suffix}\.bin")
            names = [name for name in urls if pattern.fullmatch(name)]
            if len(names) != 1:
                raise Refused(f"expected one release asset ending in _{suffix}.bin, found {len(names)}")
            files[dest] = (fetch(urls[names[0]]), chip)
        return files

    return release["tag_name"], release["html_url"], "justcallmekoko", download


def flipperhttp():
    release = api("repos/jblanked/FlipperHTTP/releases/latest")
    tag = release["tag_name"]

    def download():
        files = {}
        for name, chip in FLIPPERHTTP_FILES.items():
            path = urllib.parse.quote(f"WiFi Developer Board (ESP32S2)/{name}")
            data = fetch(f"https://raw.githubusercontent.com/jblanked/FlipperHTTP/{tag}/{path}")
            files[f"flipperhttp/s2/{name}"] = (data, chip)
        return files

    return tag, release["html_url"], "jblanked", download


def blackmagic():
    # The GitHub releases have no assets. Flipper hosts the builds on its update server.
    directory = json.loads(fetch("https://update.flipperzero.one/blackmagic-firmware/directory.json"))
    builds = [
        (version["version"], file)
        for channel in directory["channels"]
        if channel["id"] in ("release-candidate", "release")
        for version in channel["versions"]
        for file in version["files"]
        if file["target"] == "s2" and file["type"] == "full_tgz"
    ]
    if not builds:
        raise Refused("no s2 build in the update server directory")
    version, build = max(builds, key=lambda b: version_key(b[0]))
    release = api(f"repos/flipperdevices/blackmagic-esp32-s2/releases/tags/{version}")

    def download():
        archive = fetch(build["url"])
        if hashlib.sha256(archive).hexdigest() != build["sha256"]:
            raise Refused("archive does not match the sha256 in the directory")
        files = {}
        with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
            members = {Path(m.name).name: m for m in tar.getmembers() if m.isfile()}
            for name, chip in BLACKMAGIC_FILES.items():
                if name not in members:
                    raise Refused(f"{name} missing from the archive")
                files[f"blackmagic/s2/{name}"] = (tar.extractfile(members[name]).read(), chip)
        return files

    return version, release["html_url"], release["author"]["login"], download


SOURCES = [
    ("marauder", "Marauder", marauder),
    ("flipperhttp", "FlipperHTTP", flipperhttp),
    ("blackmagic", "Blackmagic", blackmagic),
]


def commit(paths, message):
    subprocess.run(["git", "-C", ROOT, "add", *paths], check=True)
    subprocess.run(["git", "-C", ROOT, "commit", "-q", "-m", message], check=True)


def warn(message):
    print(("::warning::" if os.environ.get("GITHUB_ACTIONS") else "") + message)


def update_source(key, name, source, versions, args):
    version, url, author, download = source()
    current = versions[key]
    if version_key(version) <= version_key(current):
        print(f"{name}: {current} is up to date")
        return
    print(f"{name}: {current} to {version} ({url})")

    files = download()
    errors = {dest: validate(data, chip) for dest, (data, chip) in files.items()}
    failed = {dest: error for dest, error in errors.items() if error}
    if failed:
        raise Refused("; ".join(f"{dest}: {error}" for dest, error in failed.items()))

    lines = [f"### {name} {current} to {version}", "", f"Release: {url}", ""]
    changed = []
    for dest, (data, chip) in files.items():
        path = ASSETS / dest
        is_changed = not path.exists() or path.read_bytes() != data
        result = "changed" if is_changed else "unchanged"
        what = "partition table" if chip is None else f"image for {chip}"
        print(f"  {dest}: {what} valid, {result}")
        lines.append(f"- `{dest}`: {what} valid, {result}")
        if is_changed:
            changed.append(path)
            if not args.dry_run:
                path.write_bytes(data)

    if not args.dry_run:
        versions[key] = version
        VERSIONS.write_text(json.dumps(versions, indent=2) + "\n")
        commit([*changed, VERSIONS], f"ESP Flasher: Bump {name} {version.lstrip('v')} (by {author})")
    if args.summary:
        with open(args.summary, "a") as f:
            f.write("\n".join(lines) + "\n\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="print what would change, write nothing")
    parser.add_argument("--summary", help="append a markdown summary of each bump to this file")
    args = parser.parse_args()

    versions = json.loads(VERSIONS.read_text())
    for key, name, source in SOURCES:
        try:
            update_source(key, name, source, versions, args)
        except (Refused, OSError, ValueError, KeyError, tarfile.TarError) as error:
            warn(f"{name}: refused, {error}")
            if args.summary:
                with open(args.summary, "a") as f:
                    f.write(f"### {name}: refused\n\n{error}\n\n")


if __name__ == "__main__":
    main()
