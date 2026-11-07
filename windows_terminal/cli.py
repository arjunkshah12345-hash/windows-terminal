import argparse
import json
import os
import platform
import shutil
import sys
from pathlib import Path

from . import __version__
from .control import Computer
from .qmp import QMPError
from .render import detect_protocol
from .vm import VMConfig, VMManager


def parser():
    root = argparse.ArgumentParser(prog="windows-terminal", description="A real Windows desktop inside your terminal. Separate VM. Shared view. Agent-ready.")
    root.add_argument("--version", action="version", version=__version__)
    root.add_argument("--root", type=Path, default=os.environ.get("WINDOWS_TERMINAL_ROOT") or None, help="Override VM storage directory (env: WINDOWS_TERMINAL_ROOT)")
    commands = root.add_subparsers(dest="command", required=True)
    init = commands.add_parser("init", help="Create a VM using your own Windows installation ISO")
    init.add_argument("name", nargs="?", default="windows")
    init.add_argument("--iso", type=Path, required=True)
    init.add_argument("--arch", choices=["aarch64", "x86_64"], default="aarch64" if platform.machine().lower() in {"arm64", "aarch64"} else "x86_64")
    init.add_argument("--memory", type=int, default=4096, metavar="MB")
    init.add_argument("--cpus", type=int, default=4)
    init.add_argument("--disk", type=int, default=64, metavar="GB")
    init.add_argument("--firmware", type=Path)
    init.add_argument("--network", choices=["isolated", "nat"], default="isolated", help="isolated blocks external connectivity; nat enables guest internet and host/LAN access")
    init.add_argument("--tpm", action="store_true", help="Provide TPM 2.0 with swtpm (needed for Windows 11)")
    init.add_argument("--boot", choices=["uefi", "bios"], default="uefi", help="bios: legacy SeaBIOS + IDE for older x86 guests (Windows 7 and earlier, ReactOS)")
    init.add_argument("--unattended", action="store_true", help="Hands-free install: attach an autounattend.xml that wipes the VM disk and creates a local account")
    init.add_argument("--user", default="agent", help="Local account for --unattended (default: agent)")
    init.add_argument("--password", help="Password for --unattended (default: read $WINDOWS_TERMINAL_PASSWORD, else prompt)")
    init.add_argument("--edition", default="Windows 11 Pro", help="Image name to install with --unattended")
    fetch = commands.add_parser("fetch", help="Download genuine Windows 11 from Microsoft and build a bootable ISO")
    fetch.add_argument("--arch", choices=["aarch64", "x86_64"], default="aarch64" if platform.machine().lower() in {"arm64", "aarch64"} else "x86_64")
    fetch.add_argument("--language", default="en-us", help="Media language code, e.g. en-us, en-gb, de-de")
    fetch.add_argument("--edition", default="Windows 11 Pro", help="Image to put on the ISO (Windows 11 Pro or Windows 11 Home)")
    fetch.add_argument("--out", type=Path, default=Path("."), help="Directory for the ISO (default: current directory)")
    fetch.add_argument("--setup-only", action="store_true", help="Boot media only (~0.9 GB): real Windows PE + Setup, cannot install")
    fetch.add_argument("--keep-download", action="store_true", help="Keep the 4.5 GB ESD after building the ISO")
    for command, help_text in [("up", "Start a configured VM"), ("down", "Request guest shutdown"), ("status", "Show VM status"), ("view", "Watch and control the guest in your terminal"), ("screenshot", "Save a PNG of the real guest display"), ("click", "Click a guest pixel"), ("move", "Move the guest pointer"), ("type", "Type US-layout ASCII into the guest"), ("key", "Send a key chord, e.g. win+r"), ("scroll", "Scroll in the guest"), ("mcp", "Serve computer-use tools over MCP stdio"), ("command", "Print the generated QEMU command as JSON"), ("eject", "Eject the installation ISO from the running VM")]:
        sub = commands.add_parser(command, help=help_text)
        sub.add_argument("--vm", default=os.environ.get("WINDOWS_TERMINAL_VM") or "windows", metavar="NAME")
        if command == "up":
            sub.add_argument("--view", action="store_true", help="Attach terminal viewer after starting")
            sub.add_argument("--protocol", choices=["auto", "kitty", "iterm", "blocks"], default="auto")
            sub.add_argument("--fps", type=int, default=5)
        elif command == "down":
            sub.add_argument("--force", action="store_true", help="Hard power off via QMP; unsaved guest work is lost")
        elif command == "view":
            sub.add_argument("--protocol", choices=["auto", "kitty", "iterm", "blocks"], default="auto")
            sub.add_argument("--fps", type=int, default=5)
        elif command == "screenshot":
            sub.add_argument("output", type=Path)
        elif command in {"click", "move"}:
            sub.add_argument("x", type=int)
            sub.add_argument("y", type=int)
            if command == "click":
                sub.add_argument("--button", choices=["left", "middle", "right"], default="left")
                sub.add_argument("--count", type=int, choices=[1, 2], default=1)
        elif command == "type":
            sub.add_argument("text", nargs="?", help="Omit to read UTF-8 text from stdin")
        elif command == "key":
            sub.add_argument("chord")
        elif command == "scroll":
            sub.add_argument("direction", choices=["up", "down"])
            sub.add_argument("--steps", type=int, default=3)
    commands.add_parser("list", help="List configured VMs")
    commands.add_parser("doctor", help="Check host prerequisites without modifying the machine")
    checkpoint = commands.add_parser("checkpoint", help="Save offline state or restore it into a new VM")
    operations = checkpoint.add_subparsers(dest="checkpoint_action", required=True)
    for action in ("save", "list", "restore"):
        operation = operations.add_parser(action)
        operation.add_argument("--vm", default=os.environ.get("WINDOWS_TERMINAL_VM") or "windows", metavar="NAME")
        if action != "list":
            operation.add_argument("checkpoint_name", metavar="CHECKPOINT")
        if action == "restore":
            operation.add_argument("--as", dest="target_name", required=True, metavar="NEW_VM")
    return root


def detached(manager, name):
    state = manager.status(name)["state"]
    if state == "running":
        print(f"Detached from {name}; it is still running. Reattach: windows-terminal view --vm {name}", file=sys.stderr)
    else:
        print(f"{name} is {state}.", file=sys.stderr)
    return None


def fetched_media(iso):
    """ISOs built by `fetch` boot with efisys_noprompt.bin and need no key."""
    try:
        with open(iso, "rb") as stream:
            stream.seek(16 * 2048 + 40)
            return stream.read(32).startswith(b"WT_WIN11")
    except OSError:
        return False


def press_boot_key(computer, seconds=12.0):
    """Microsoft ISOs wait for "Press any key to boot from CD or DVD"; answer it on a blank disk only."""
    import time
    print("Blank disk: answering the installer's \"Press any key to boot from CD\" prompt…", file=sys.stderr)
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            computer.key("enter")
        except (OSError, QMPError):
            pass
        time.sleep(0.5)


def fetch_media(args):
    from . import media
    out = args.out.expanduser().resolve()
    out.mkdir(parents=True, exist_ok=True)
    say = lambda message: print(f"\r\033[K{message}", end="" if "%" in message else "\n", file=sys.stderr, flush=True)
    family = "Professional"  # consumer ESD: Home, Home Single Language, Pro
    entry = media.catalog(args.arch, args.language, family)
    say(f"Microsoft catalog: {entry['name']} ({entry['size'] / 1e9:.1f} GB, SHA-1 {entry['sha1'][:12]}…)")
    media.check_space(entry, out, args.setup_only)
    esd = (media.download_setup_only(entry, out / (entry["name"] + ".setup"), say) if args.setup_only
           else media.download(entry, out / entry["name"], say))
    suffix = "setup" if args.setup_only else args.edition.lower().replace("windows ", "win").replace(" ", "-")
    build = ".".join(entry["name"].split(".")[:2])  # e.g. 26100.4349
    iso = out / f"win11-{build}-{'arm64' if args.arch == 'aarch64' else 'x64'}-{suffix}.iso"
    media.build_iso(esd, iso, args.edition, args.setup_only, say, delete_esd=not args.keep_download)
    return {"iso": str(iso), "size": iso.stat().st_size, "source": entry["url"], "verified": "per-chunk WIM integrity table + per-file SHA-1" if args.setup_only else f"SHA-1 {entry['sha1']} (Microsoft catalog)",
            "setup_only": args.setup_only,
            "next": f"windows-terminal init --iso {iso} --arch {args.arch} --tpm" + ("" if args.setup_only else " --unattended")}


def run(args):
    if args.command == "doctor":
        return {"host": platform.system(), "architecture": platform.machine(), "python": platform.python_version(), "qemu_aarch64": shutil.which("qemu-system-aarch64"), "qemu_x86_64": shutil.which("qemu-system-x86_64"), "qemu_img": shutil.which("qemu-img"), "swtpm": shutil.which("swtpm"), "wimlib": shutil.which("wimlib-imagex"), "xorriso": shutil.which("xorriso"), "graphics": detect_protocol(), "guest_installed": "Use status --vm NAME to inspect a configured guest.", "next": "Install QEMU if missing. Get Windows with `fetch` (needs wimlib + xorriso) or bring your own ISO, then init --iso PATH."}
    manager = VMManager(root=args.root)
    if args.command == "fetch":
        return fetch_media(args)
    if args.command == "init":
        if args.unattended and args.boot != "uefi":
            raise ValueError("--unattended is for UEFI Windows installs")
        record = manager.create(VMConfig(name=args.name, arch=args.arch, memory_mb=args.memory, cpus=args.cpus, disk_gb=args.disk, iso=str(args.iso.expanduser().resolve()), firmware=str(args.firmware.expanduser().resolve()) if args.firmware else None, network=args.network, tpm=args.tpm, boot=args.boot))
        if args.unattended:
            from . import media
            password = args.password or os.environ.get("WINDOWS_TERMINAL_PASSWORD")
            if not password:
                import getpass
                password = getpass.getpass(f"Password for Windows account {args.user!r}: ")
            try:
                xml = media.unattend_xml(args.arch, args.user, password, args.edition)
                media.answer_iso(xml, manager.root / args.name / "answer.iso")
            except BaseException:
                shutil.rmtree(manager.root / args.name, ignore_errors=True)
                raise
            record["unattended"] = {"user": args.user, "edition": args.edition}
        return record
    if args.command == "list":
        return manager.list()
    if args.command == "checkpoint":
        from .checkpoints import Checkpoints
        checkpoints = Checkpoints(manager)
        if args.checkpoint_action == "list":
            return checkpoints.list(args.vm)
        if args.checkpoint_action == "save":
            return checkpoints.save(args.vm, args.checkpoint_name)
        return checkpoints.restore(args.vm, args.checkpoint_name, args.target_name)
    if args.command in {"status", "command"}:
        return getattr(manager, args.command)(args.vm)
    if args.command == "up":
        blank = manager.status(args.vm)["state"] == "stopped" and manager.load(args.vm).get("iso") and manager.disk_is_blank(args.vm)
        result = manager.start(args.vm)
        if blank and not fetched_media(manager.load(args.vm)["iso"]):
            press_boot_key(Computer(manager.qmp_path(args.vm)))
        if args.view:
            from .viewer import view
            view(Computer(manager.qmp_path(args.vm)), args.vm, args.protocol, args.fps)
            return detached(manager, args.vm)
        return result
    if args.command == "down":
        return manager.stop(args.vm, force=args.force)
    if args.command == "mcp":
        from .mcp import serve
        serve(manager, args.vm)
        return None
    computer = Computer(manager.qmp_path(args.vm))
    if args.command == "eject":
        from .qmp import QMPClient
        with QMPClient(manager.qmp_path(args.vm)) as client:
            client.execute("eject", {"device": "install", "force": False})
        return {"ok": True, "note": "ISO ejected for this boot. Disk is first in the persistent boot order."}
    if args.command == "view":
        from .viewer import view
        view(computer, args.vm, args.protocol, args.fps)
        return detached(manager, args.vm)
    if args.command == "screenshot":
        computer.screenshot().save(args.output, format="PNG")
        return {"path": str(args.output.resolve())}
    if args.command == "type":
        text = args.text if args.text is not None else sys.stdin.read(20001)
        computer.type(text)
    elif args.command == "key":
        computer.key(args.chord)
    elif args.command == "move":
        computer.move(args.x, args.y)
    elif args.command == "click":
        computer.click(args.x, args.y, args.button, args.count)
    elif args.command == "scroll":
        computer.scroll(args.direction, args.steps)
    return {"ok": True}


def main():
    args = parser().parse_args()
    try:
        result = run(args)
        if result is not None:
            print(json.dumps(result, indent=2))
    except (OSError, ValueError, RuntimeError, QMPError) as error:
        print(f"windows-terminal: {error}", file=sys.stderr)
        raise SystemExit(1) from None
    except KeyboardInterrupt:
        raise SystemExit(130) from None
