"""Command-line entry point and recovery orchestration."""

import argparse
import http.client
import math
import subprocess
import sys
import time
import zipfile
import zlib
from pathlib import Path
from urllib.error import URLError

from . import EubError, __version__
from .compatibility import CompatibilityOverride
from .doctor import diagnose
from .downloads import (
    INDEX_URL,
    PACK_SHA256,
    PACK_URL,
    Cache,
    default_cache,
    select_image,
)
from .images import (
    Sources,
    bounded_read,
    load_bootloader,
    load_directory,
    load_recovery,
    parse_fbpk,
)
from .models import MODELS, choose_model, model_for, soc_for
from .port_access import PortAccessRecovery
from .protocol import Session, probe, serve
from .transport import (
    Fastboot,
    NoDevice,
    PortUnavailable,
    disconnected,
    find_port,
    open_port,
    ports,
)


class CommandParser(argparse.ArgumentParser):
    def __init__(self, *args, **kwargs):
        kwargs.setdefault("formatter_class", argparse.ArgumentDefaultsHelpFormatter)
        super().__init__(*args, **kwargs)


class FullHelpParser(CommandParser):
    command_parsers = None

    def format_help(self):
        overview = super().format_help()
        if not self.command_parsers:
            return overview
        details = "\n".join(
            command.format_help() for command in self.command_parsers.values()
        )
        return (
            overview
            + "\nCommand arguments (place options after the command):\n"
            + 'Example: pixel-eub boot --device "Pixel 7 Pro" --pack auto --wait\n\n'
            + details
        )


def positive(value):
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise argparse.ArgumentTypeError("must be a finite positive number")
    return number


def cache_options(parser):
    parser.add_argument(
        "--cache-dir",
        type=Path,
        default=default_cache(),
        help="download/cache directory (or PIXEL_EUB_CACHE)",
    )
    parser.add_argument(
        "--offline", action="store_true", help="use verified cached downloads only"
    )


def factory_options(parser):
    parser.add_argument(
        "--device", required=True, help='codename or model, e.g. "Pixel 8 Pro"'
    )
    parser.add_argument(
        "--build",
        default="latest",
        help="full build ID, or latest generic stable build",
    )
    parser.add_argument(
        "--refresh", action="store_true", help="refresh the cached factory index"
    )
    cache_options(parser)


def parser():
    root = FullHelpParser(
        prog="pixel-eub",
        description="Boot compatible Tensor Pixels from ROM Recovery into fastboot using signed stages in RAM.",
    )
    root.add_argument(
        "--version", action="version", version=__version__, help="show program version"
    )
    commands = root.add_subparsers(
        dest="command", required=True, parser_class=CommandParser
    )
    root.command_parsers = commands.choices
    commands.add_parser(
        "models", help="model aliases and actual recovery-pack availability"
    )
    detect = commands.add_parser("detect", help="find Pixel ROM Recovery USB CDC ports")
    detect.add_argument("--all", action="store_true", help="include other serial ports")
    doctor = commands.add_parser(
        "doctor",
        help="diagnose dependencies, USB permissions and cache without changes",
    )
    doctor.add_argument(
        "--fastboot",
        default="fastboot",
        help="Platform-Tools fastboot executable to locate",
    )
    doctor.add_argument(
        "--cache-dir",
        type=Path,
        default=default_cache(),
        help="cache directory to check (or PIXEL_EUB_CACHE)",
    )
    builds = commands.add_parser(
        "builds", help="list official factory builds and SHA-256"
    )
    factory_options(builds)
    fetch = commands.add_parser(
        "fetch", help="download and verify a factory ZIP for later normal flashing"
    )
    factory_options(fetch)
    boot = commands.add_parser(
        "boot", help="serve ROM requests and verify bootloader fastboot"
    )
    boot.add_argument(
        "--device", help="model name/codename; omitted: probe ROM and select model"
    )
    boot.add_argument("--port", help="serial port, e.g. /dev/ttyACM0 or COM3")
    boot.add_argument(
        "--wait", action="store_true", help="wait for ROM Recovery USB to appear"
    )
    boot.add_argument(
        "--wait-timeout",
        type=positive,
        default=60,
        help="seconds to wait for ROM USB when --wait is enabled",
    )
    boot.add_argument(
        "--reconnect-wait",
        type=positive,
        default=15,
        help="seconds to follow USB re-enumeration",
    )
    boot.add_argument(
        "--verify-timeout",
        type=positive,
        default=20,
        help="seconds to confirm fastboot",
    )
    boot.add_argument(
        "--idle-timeout",
        type=positive,
        default=15,
        help="seconds without ROM activity before the serial session times out",
    )
    boot.add_argument(
        "--no-port-recovery",
        action="store_true",
        help="disable Linux Pixel-port permission repair and release of processes holding that port",
    )
    boot.add_argument(
        "--image", type=Path, help="local factory ZIP or FBPK bootloader image"
    )
    boot.add_argument(
        "--dir", type=Path, help="directory of signed *.img stages (highest priority)"
    )
    boot.add_argument(
        "--pack",
        default="auto",
        help="auto (default), none, gs201, zuma/shiba, zuma/husky, local recovery ZIP or directory",
    )
    boot.add_argument(
        "--override",
        action="store_true",
        help="allow model/SoC incompatibilities only after an explicit YES/NO confirmation",
    )
    boot.add_argument(
        "--build",
        help="also fetch a specific official factory ZIP (or latest); requires --device",
    )
    boot.add_argument(
        "--refresh", action="store_true", help="refresh the cached factory index"
    )
    boot.add_argument(
        "--epbl",
        choices=("full", "split"),
        help="override EPBL slicing for a known chip",
    )
    boot.add_argument(
        "--checksum",
        choices=("ffff", "sum16"),
        default="ffff",
        help="upload checksum: constant ffff or sum16 byte sum",
    )
    boot.add_argument(
        "--fastboot", default="fastboot", help="Platform-Tools fastboot executable"
    )
    boot.add_argument(
        "--fastboot-serial",
        help="select the target fastboot serial for the final check",
    )
    boot.add_argument(
        "--allow-experimental",
        action="store_true",
        help="allow a sibling model's recovery pack",
    )
    boot.add_argument(
        "--dry-run",
        action="store_true",
        help="prepare sources without opening USB; requires --device",
    )
    boot.add_argument(
        "--verbose",
        action="store_true",
        help="print raw ROM messages, including the device serial number",
    )
    cache_options(boot)
    unpack = commands.add_parser(
        "unpack", help="inspect a raw FBPK bootloader, verify CRC32"
    )
    unpack.add_argument("image", type=Path, help="path to a raw FBPK bootloader image")
    return root


def factory(cache, model, build, refresh=False):
    image = select_image(cache.catalog(refresh), model.codename, build)
    print(f"Factory image: {model.name} / {image.build}")
    print(f"Google factory image terms apply: {INDEX_URL}", file=sys.stderr)
    return cache.download(image.url, image.sha256)


PACK_MODELS = {"gs201": "panther", "zuma/shiba": "shiba", "zuma/husky": "husky"}


def directory_model(path):
    path = Path(path)
    key = "zuma/" + path.name if path.parent.name == "zuma" else path.name
    return model_for(PACK_MODELS[key]) if key in PACK_MODELS else None


def check_pack_model(pack_model, model, compatibility, label):
    if pack_model.soc != model.soc:
        compatibility.check(
            f"{label} targets {pack_model.soc}; selected model {model.name} requires {model.soc}."
        )
    elif model.pack and pack_model.pack != model.pack:
        compatibility.check(
            f"{label} is for {pack_model.name} ({pack_model.pack}); "
            f"selected model {model.name} normally uses {model.pack}."
        )


def prepare(args, model, cache, compatibility=None):
    compatibility = compatibility or CompatibilityOverride()
    layers = []
    if args.dir:
        inferred = directory_model(args.dir)
        if inferred:
            check_pack_model(
                inferred, model, compatibility, f"Stage directory {args.dir}"
            )
        layers.append(load_directory(args.dir))
    use_pack = args.pack != "none" and (args.pack != "auto" or not args.dir)
    if use_pack:
        pack_model = (
            model_for(PACK_MODELS[args.pack]) if args.pack in PACK_MODELS else model
        )
        if args.pack not in ("auto", "none") and args.pack not in PACK_MODELS:
            pack_model = directory_model(args.pack) or model
        check_pack_model(
            pack_model,
            model,
            compatibility,
            f"Recovery pack {pack_model.pack or args.pack}",
        )
        if not pack_model.pack and args.pack == "auto":
            if not (args.image or args.build):
                raise EubError(
                    f"No public USB-bootable recovery pack for {model.name} ({model.soc}). A factory image alone cannot replace a signed USB BL1. Supply compatible signed stages with --dir/--pack none when available."
                )
        else:
            experimental = pack_model.experimental or (
                model.experimental and pack_model.pack == model.pack
            )
            if experimental and not args.allow_experimental:
                compatibility.check(
                    f"{model.name} uses a sibling model's recovery pack; hardware compatibility is unverified here. Use --allow-experimental only if you intend to try that pack."
                )
            path = (
                cache.download(PACK_URL, PACK_SHA256, max_size=256 * 1024**2)
                if args.pack == "auto" or args.pack in PACK_MODELS
                else Path(args.pack)
            )
            layers.append(load_recovery(path, pack_model))
            print(f"Recovery pack: {pack_model.pack or path}")
    image = args.image
    if args.build:
        image = factory(cache, model, args.build, args.refresh)
    if image:
        layers.append(load_bootloader(image, model, compatibility).images)
    if not layers:
        raise EubError(
            "No boot stages selected; provide --image, --dir or --pack auto."
        )
    return Sources(layers, model.soc, args.epbl)


def boot(args):
    if args.build and (not args.device or args.image):
        raise EubError("--build requires --device and cannot be combined with --image.")
    if args.dry_run and not args.device:
        raise EubError("--dry-run requires --device so USB does not need to be opened.")
    model = model_for(args.device) if args.device else None
    compatibility = CompatibilityOverride(args.override)
    session, selected = Session(compatibility=compatibility), None
    recovery = (
        None
        if args.no_port_recovery
        else PortAccessRecovery(lambda message: print(message, flush=True))
    )
    cache = Cache(args.cache_dir.expanduser(), args.offline)
    if not args.dry_run:
        print(
            "[1/3] Checking Pixel ROM Recovery USB connection (18d1:4f00)…", flush=True
        )
        selected = find_port(args.port, wait=args.wait_timeout if args.wait else 0)
        print(f"ROM USB device found: {selected.device}", flush=True)
    # An override may ask a human to decide. Probe and close the link first,
    # then confirm outside the live ROM upload/clear-to-send exchange.
    if not model or (args.override and not args.dry_run):
        with open_port(selected, recovery=recovery) as link:
            serial = probe(link, args.idle_timeout)
        session.note_identity(serial, model.soc if model else None)
        print(f"ROM identity: {serial} ({soc_for(serial)})")
        if not model:
            model = choose_model(session.soc, sys.stdin.isatty())
    print(f"Selected {model.name} ({model.codename}, {model.soc})")
    sources = prepare(args, model, cache, compatibility)
    if session.soc:
        sources.use_soc(session.soc)
    if args.dry_run:
        print(
            "Sources ready. USB was not opened; no hardware recovery has been tested."
        )
        for stage in ("BL1", "DPM", "EPBL", "BL2", "ABL", "BL3B"):
            try:
                print(f"  {stage}: {len(sources.payload(stage))} bytes")
            except EubError as exc:
                print(f"  {stage}: {exc}")
                raise
        return 0
    print(
        "[2/3] Serving signed IMG stages requested by ROM. Uploads go to RAM.",
        flush=True,
    )
    # The phone can change its device node while sources are downloaded after
    # the automatic identity probe. Resolve the original USB identity again.
    original = selected
    if not args.device or args.override:
        original = find_port(original=selected, wait=args.wait_timeout)
    current = original
    for connection in range(12):
        opened = None
        if connection:
            print("Waiting for ROM USB re-enumeration to continue stage uploads…")
            deadline = time.monotonic() + args.reconnect_wait
            current = None
            while time.monotonic() < deadline:
                try:
                    current = find_port(original=original)
                    opened = open_port(current, recovery=recovery)
                except (NoDevice, PortUnavailable):
                    current = None
                if opened is not None:
                    break
                time.sleep(0.25)
            if current is None:
                break
        with (
            opened
            if opened is not None
            else open_port(current, recovery=recovery) as link
        ):
            print(f"ROM serial port opened: {current.device}", flush=True)
            try:
                serve(
                    link,
                    sources,
                    session,
                    model.soc,
                    idle_timeout=args.idle_timeout,
                    checksum=args.checksum,
                    verbose=args.verbose,
                    report=lambda message: print(message, flush=True),
                )
            except OSError as exc:
                if not disconnected(exc):
                    raise
                print(f"USB disconnected ({exc}); following device handover.")
        # A ROM can re-enumerate after consuming the last byte, racing the
        # host's final tcdrain/flush. That attempt needs discovery even though
        # it could not be recorded as completely flushed. Success still
        # requires independent fastboot product verification.
        if not session.attempts:
            raise EubError(
                "ROM disconnected before any signed stage was sent; no recovery completed."
            )
    else:
        raise EubError("Too many USB reconnects; stopping recovery loop.")

    # Only check the outcome after all ROM connections have been serviced.
    # Running fastboot during BL2 handover can delay the next ROM request.
    print("[3/3] ROM serial session ended. Checking bootloader fastboot…", flush=True)
    try:
        fastboot = Fastboot(args.fastboot, args.fastboot_serial)
        deadline = time.monotonic() + args.verify_timeout
        while time.monotonic() < deadline:
            confirmed = fastboot.verify(model)
            if confirmed:
                print(
                    f"Bootloader fastboot confirmed: {confirmed} ({model.codename}). You can now use normal flashing tools."
                )
                return 0
            time.sleep(0.25)
    except (EubError, OSError, subprocess.SubprocessError, UnicodeError) as exc:
        raise EubError(
            f"ROM serial session ended after {len(session.sent)} completed stage uploads. "
            f"Bootloader fastboot is NOT confirmed: {exc} "
            "Check the phone's screen and run 'fastboot devices' when Platform-Tools is available."
        ) from exc
    raise EubError(
        "ROM connection ended, but bootloader fastboot was not confirmed. "
        "Check the phone, signed stage compatibility and 'fastboot devices'."
    )


def main(argv=None) -> int:
    args = parser().parse_args(argv)
    try:
        if args.command == "models":
            print("Model              Codename    SoC       Recovery availability")
            for model in MODELS:
                status = (
                    "no public USB BL1"
                    if not model.pack
                    else (
                        "sibling pack: experimental"
                        if model.experimental
                        else "upstream recovery pack"
                    )
                )
                print(f"{model.name:18} {model.codename:11} {model.soc:9} {status}")
            print(
                "This implementation is not hardware-validated. ROM signatures and anti-rollback still apply."
            )
        elif args.command == "doctor":
            return diagnose(args.cache_dir, args.fastboot)
        elif args.command == "detect":
            found = ports(args.all)
            for port in found:
                usb = (
                    f"{port.vid:04x}:{port.pid:04x}"
                    if port.vid is not None and port.pid is not None
                    else "unknown USB ID"
                )
                print(f"{port.device}\t{usb}\t{port.serial or '-'}\t{port.description}")
            if not found:
                raise EubError("No matching serial devices found.")
        elif args.command in ("builds", "fetch"):
            model = model_for(args.device)
            cache = Cache(args.cache_dir.expanduser(), args.offline)
            if args.command == "fetch":
                print(factory(cache, model, args.build, args.refresh))
            else:
                images = [
                    i for i in cache.catalog(args.refresh) if i.device == model.codename
                ]
                if args.build.lower() != "latest":
                    images = [i for i in images if i.build == args.build.lower()]
                if not images:
                    raise EubError("No matching factory builds found.")
                for image in images:
                    print(
                        f"{image.build}\t{image.description}\n  SHA-256 {image.sha256}\n  {image.url}"
                    )
        elif args.command == "unpack":
            pack = parse_fbpk(bounded_read(args.image))
            print(
                f"FBPK v2: {pack.platform}; products: {', '.join(sorted(pack.products))}"
            )
            for name, image in pack.images.items():
                print(f"{name:16} {len(image):10} bytes (CRC32 verified when present)")
        else:
            return boot(args)
        return 0
    except KeyboardInterrupt:
        print("\nInterrupted. No automatic flashing was performed.", file=sys.stderr)
        return 130
    except (
        EubError,
        OSError,
        URLError,
        zipfile.BadZipFile,
        zlib.error,
        subprocess.SubprocessError,
        http.client.HTTPException,
        UnicodeError,
        EOFError,
        ValueError,
        RuntimeError,
    ) as exc:
        print(f"pixel-eub: {exc}", file=sys.stderr)
        return 1
