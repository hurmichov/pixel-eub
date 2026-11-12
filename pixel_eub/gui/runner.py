"""Run pixel-eub CLI commands for the GUI and turn their output into events.

The GUI never reimplements recovery: every action runs the same CLI in a child
process. YES/NO prompts are detected on stdout and answered through stdin.
This module has no GTK dependency so it can be tested anywhere.
"""

import os
import re
import signal
import subprocess
import sys
import threading
from dataclasses import dataclass, field
from pathlib import Path

PROMPT_SUFFIX = "[YES/NO] (NO): "
PACKAGE_ROOT = Path(__file__).resolve().parents[2]


def cli_command(*args: str) -> list[str]:
    return [sys.executable, "-m", "pixel_eub", *args]


def cli_environment() -> dict[str, str]:
    env = dict(os.environ, PYTHONUNBUFFERED="1", PYTHONIOENCODING="utf-8")
    # A source checkout is not installed; let the child import the same package.
    env["PYTHONPATH"] = os.pathsep.join(
        filter(None, (str(PACKAGE_ROOT), os.environ.get("PYTHONPATH")))
    )
    return env


@dataclass
class Line:
    text: str


@dataclass
class Prompt:
    kind: str  # "override" or "panic"
    question: str
    context: list[str]


@dataclass
class Phase:
    number: int
    text: str


@dataclass
class Stage:
    name: str
    state: str  # requested, sending, forced, uploaded, acknowledged
    size: int | None = None


@dataclass
class Identity:
    soc: str


@dataclass
class Hint:
    text: str


@dataclass
class Waiting:
    text: str


@dataclass
class Outcome:
    success: bool
    text: str


@dataclass
class Exit:
    code: int


STAGE_PATTERNS = (
    (re.compile(r"ROM requests (\w+)$"), "requested"),
    (re.compile(r"Sending (\w+): (\d+) bytes"), "sending"),
    (
        re.compile(
            r"Experimental forced upload \(no ROM request/C\) (\w+): (\d+) bytes"
        ),
        "forced",
    ),
    (re.compile(r"Uploaded (\w+): (\d+) bytes"), "uploaded"),
    (re.compile(r"ROM acknowledged (\w+)$"), "acknowledged"),
)


@dataclass
class OutputParser:
    """Split child output into lines, progress events and pending prompts."""

    partial: str = ""
    context: list[str] = field(default_factory=list)

    def feed(self, text: str) -> list:
        events = []
        self.partial += text.replace("\r\n", "\n").replace("\r", "\n")
        *lines, self.partial = self.partial.split("\n")
        for line in lines:
            events.extend(self.line(line))
        if self.partial.endswith(PROMPT_SUFFIX):
            question, self.partial = self.partial, ""
            events.append(Line(question))
            kind = "panic" if "panic" in question.lower() else "override"
            # Explain the question with the recent lines from its first WARNING.
            warnings = [
                i for i, x in enumerate(self.context) if x.startswith("WARNING")
            ]
            context = self.context[warnings[0] :] if warnings else self.context[-4:]
            events.append(Prompt(kind, question.strip(), context))
            self.context = []
        return events

    def finish(self) -> list:
        rest, self.partial = self.partial, ""
        return self.line(rest) if rest else []

    def line(self, line: str) -> list:
        events = [Line(line)]
        self.context = [*self.context, line][-12:]
        if match := re.match(r"\[(\d)/3\] (.*)", line):
            events.append(Phase(int(match[1]), match[2]))
        if match := re.match(r"ROM identity(?: verified)?: \S+ \((\w+)\)", line):
            events.append(Identity(match[1]))
        for pattern, state in STAGE_PATTERNS:
            if match := pattern.match(line):
                size = int(match[2]) if match.lastindex >= 2 else None
                events.append(Stage(match[1].upper(), state, size))
        if line.startswith("Waiting for the ROM after "):
            events.append(Waiting(line))
        if "you can release the buttons now" in line:
            events.append(Hint("You can release the buttons now."))
        if line.startswith("Bootloader fastboot confirmed:"):
            events.append(Outcome(True, line))
        elif line.startswith("pixel-eub: "):
            events.append(Outcome(False, line.removeprefix("pixel-eub: ")))
        return events


class Job:
    """One CLI child process. Callbacks run on the reader thread."""

    def __init__(self, args, on_event, *, popen=subprocess.Popen):
        self.args = list(args)
        self.on_event = on_event
        self.parser = OutputParser()
        self.process = popen(
            cli_command(*self.args),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            env=cli_environment(),
            # Own process group: Stop interrupts the CLI, not the GUI.
            start_new_session=os.name == "posix",
        )
        self.reader = threading.Thread(target=self._read, daemon=True)
        self.reader.start()

    def _read(self):
        stream = self.process.stdout
        decoder = _Utf8Decoder()
        while chunk := stream.read1(4096):
            for event in self.parser.feed(decoder.decode(chunk)):
                self.on_event(event)
        for event in self.parser.feed(decoder.decode(b"", final=True)):
            self.on_event(event)
        for event in self.parser.finish():
            self.on_event(event)
        code = self.process.wait()
        for pipe in (self.process.stdout, self.process.stdin):
            try:
                pipe.close()
            except OSError:
                pass
        self.on_event(Exit(code))

    def answer(self, yes: bool):
        try:
            self.process.stdin.write(b"YES\n" if yes else b"NO\n")
            self.process.stdin.flush()
        except (BrokenPipeError, OSError, ValueError):
            pass

    def interrupt(self):
        """Send Ctrl+C semantics; the CLI reports 'Interrupted' and exits 130."""
        if self.process.poll() is not None:
            return
        try:
            if os.name == "posix":
                os.killpg(self.process.pid, signal.SIGINT)
            else:
                self.process.terminate()
        except (ProcessLookupError, OSError):
            pass

    @property
    def running(self):
        return self.process.poll() is None


class _Utf8Decoder:
    def __init__(self):
        import codecs

        self.decoder = codecs.getincrementaldecoder("utf-8")("replace")

    def decode(self, data: bytes, final=False) -> str:
        return self.decoder.decode(data, final)


@dataclass
class Options:
    """GUI settings that map one-to-one onto CLI options."""

    device: str = ""
    gs101_profile: str = ""
    pack: str = "auto"
    pack_path: str = ""
    image: str = ""
    directory: str = ""
    build: str = ""
    port: str = ""
    wait: bool = True
    wait_timeout: float = 60
    reconnect_wait: float = 15
    idle_timeout: float = 15
    verify_timeout: float = 20
    ack_timeout: float = 600
    fastboot: str = "fastboot"
    fastboot_serial: str = ""
    epbl: str = ""
    checksum: str = "ffff"
    override: bool = False
    allow_experimental: bool = False
    allow_dpm_first: bool = False
    no_usb_check: bool = False
    research_dir: str = ""
    dump_usb: bool = False
    capture: bool = False
    listen_after_failure: float = 0
    port_recovery: bool = False
    verbose: bool = False
    cache_dir: str = ""
    offline: bool = False
    refresh: bool = False
    dark: bool = True  # GUI appearance only; not a CLI option


def settings_path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config"
    return Path(base) / "pixel-eub" / "gui.json"


def load_options(path: Path | None = None) -> Options:
    """Unknown keys and wrong types are ignored; risky switches never persist."""
    import json

    options = Options()
    try:
        data = json.loads((path or settings_path()).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return options
    if not isinstance(data, dict):
        return options
    for name, default in vars(Options()).items():
        value = data.get(name)
        if name in UNSAFE_TO_PERSIST or value is None:
            continue
        if isinstance(default, bool) and isinstance(value, bool):
            setattr(options, name, value)
        elif isinstance(default, float | int) and not isinstance(default, bool):
            if isinstance(value, int | float) and not isinstance(value, bool):
                if value > 0:
                    setattr(options, name, float(value))
        elif isinstance(default, str) and isinstance(value, str):
            setattr(options, name, value)
    return options


def save_options(options: Options, path: Path | None = None):
    import json

    path = path or settings_path()
    data = {k: v for k, v in vars(options).items() if k not in UNSAFE_TO_PERSIST}
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(data, indent=2), encoding="utf-8")
    os.replace(temporary, path)


# Each run must opt in again to overrides, sibling packs and privileged repair.
UNSAFE_TO_PERSIST = {
    "override",
    "allow_experimental",
    "allow_dpm_first",
    "port_recovery",
    "no_usb_check",
}


def cache_args(options: Options) -> list[str]:
    args = []
    if options.cache_dir:
        args += ["--cache-dir", options.cache_dir]
    if options.offline:
        args.append("--offline")
    return args


def gs101_args(options: Options, *, dry_run=False) -> list[str]:
    if options.device not in ("bluejay", "oriole", "raven"):
        raise ValueError("Select a GS101 phone model first.")
    if not options.gs101_profile:
        raise ValueError("Choose the prepared GS101 profile folder for this phone.")
    args = ["gs101", "--device", options.device, "--profile", options.gs101_profile]
    args.append("--check-profile" if dry_run else "--acknowledge-risk")
    return args


def boot_args(options: Options, *, dry_run=False) -> list[str]:
    if not options.device:
        raise ValueError("Select the phone model first.")
    pack = options.pack_path if options.pack == "path" else options.pack
    if not pack:
        raise ValueError("Choose a local recovery pack file or folder.")
    args = ["boot", "--device", options.device, "--pack", pack]
    for flag, value in (
        ("--image", options.image),
        ("--dir", options.directory),
        ("--build", options.build),
        ("--epbl", options.epbl),
        ("--fastboot-serial", options.fastboot_serial),
    ):
        if value:
            args += [flag, value]
    if options.checksum != "ffff":
        args += ["--checksum", options.checksum]
    if options.fastboot and options.fastboot != "fastboot":
        args += ["--fastboot", options.fastboot]
    for flag, enabled in (
        ("--override", options.override),
        ("--allow-experimental", options.allow_experimental),
        ("--allow-dpm-first", options.allow_dpm_first and not dry_run),
        ("--no-usb-check", options.no_usb_check),
        ("--refresh", options.refresh and bool(options.build)),
    ):
        if enabled:
            args.append(flag)
    if dry_run:
        args.append("--dry-run")
    else:
        if options.port:
            args += ["--port", options.port]
        if options.research_dir and options.dump_usb:
            args += ["--dump-usb", options.research_dir]
        if options.research_dir and options.capture:
            args += ["--capture", options.research_dir]
        if options.listen_after_failure > 0:
            args += ["--listen-after-failure", _number(options.listen_after_failure)]
        if options.wait:
            args += ["--wait", "--wait-timeout", _number(options.wait_timeout)]
        args += [
            "--reconnect-wait",
            _number(options.reconnect_wait),
            "--idle-timeout",
            _number(options.idle_timeout),
            "--verify-timeout",
            _number(options.verify_timeout),
            "--ack-timeout",
            _number(options.ack_timeout),
        ]
        args.append(
            "--port-recovery" if options.port_recovery else "--no-port-recovery"
        )
        if options.verbose:
            args.append("--verbose")
    return args + cache_args(options)


def factory_args(command: str, options: Options, device: str, build: str) -> list[str]:
    args = [command, "--device", device, "--build", build or "latest"]
    if options.refresh:
        args.append("--refresh")
    return args + cache_args(options)


def _number(value: float) -> str:
    return f"{value:g}"


@dataclass
class DoctorCheck:
    status: str
    message: str


def parse_doctor(lines: list[str]) -> list[DoctorCheck]:
    return [
        DoctorCheck(match[1], match[2])
        for line in lines
        if (match := re.match(r"\[(OK|INFO|WARN|FAIL)\] (.*)", line))
    ]


@dataclass
class FactoryBuild:
    build: str
    description: str
    sha256: str
    url: str


def parse_builds(lines: list[str]) -> list[FactoryBuild]:
    builds, current = [], None
    for line in lines:
        if line.startswith("  SHA-256 ") and current:
            current["sha256"] = line.split()[-1]
        elif line.startswith("  https://") and current:
            current["url"] = line.strip()
            builds.append(FactoryBuild(**current))
            current = None
        elif "\t" in line and not line.startswith(" "):
            build, description = line.split("\t", 1)
            current = {"build": build, "description": description}
    return builds


@dataclass
class InspectedStage:
    name: str
    stage: str
    size: int
    usb: bool = False
    family: str = ""
    flags: str = ""
    notes: list[str] = field(default_factory=list)

    @property
    def is_stage(self):
        return self.stage != "not a stage image"


def parse_inspect(lines: list[str]) -> list[InspectedStage]:
    stages = []
    for line in lines:
        if line.startswith("  ! ") and stages:
            stages[-1].notes.append(line[4:])
            continue
        parts = line.split(" | ")
        if len(parts) < 3 or not parts[2].endswith(" bytes"):
            continue
        stage = InspectedStage(parts[0], parts[1], int(parts[2].split()[0]))
        for part in parts[3:]:
            if part.startswith("USB boot: "):
                stage.usb = part.endswith("yes")
            elif part.startswith("family: "):
                family = part.removeprefix("family: ")
                stage.family = "" if family == "unknown" else family
            elif part.startswith("flags "):
                stage.flags = part
        stages.append(stage)
    return stages


@dataclass
class SerialPort:
    device: str
    usb: str
    serial: str
    description: str


def parse_detect(lines: list[str]) -> list[SerialPort]:
    return [
        SerialPort(*(line.split("\t", 3) + [""] * 4)[:4])
        for line in lines
        if line.count("\t") >= 2
    ]
