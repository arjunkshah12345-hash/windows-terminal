"""Fetch genuine Windows media from Microsoft and turn it into a bootable ISO.

The Media Creation Tool catalog (products.cab) lists every Windows 11 ESD that
Microsoft publishes on its update CDN, with size and SHA-1. We download the ESD
for the requested architecture/language, verify it, and rebuild a standard
installation ISO with wimlib + xorriso:

    index 1  Windows Setup Media   -> ISO root (bootmgr, efi/, sources/...)
    index 2  Windows PE            -> sources/boot.wim image 1
    index 3  Windows Setup         -> sources/boot.wim image 2 (bootable)
    edition  e.g. Windows 11 Pro   -> sources/install.esd

``setup_only`` skips the edition: only the leading ~0.56 GB of the ESD is
range-downloaded (see download_setup_only) and the ISO (~0.9 GB) boots real
Windows PE and Windows Setup but cannot install. The UEFI El Torito image is
efisys_noprompt.bin, so no "Press any key to boot from CD" is needed, plus a
startup.nsh for firmware that falls back to the UEFI shell.

Nothing here contacts anything but Microsoft's own catalog and CDN.
"""

from __future__ import annotations

import hashlib
import struct
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
from typing import Callable
import urllib.request
from xml.etree import ElementTree
from xml.sax.saxutils import escape

CATALOG_URL = "https://go.microsoft.com/fwlink/?LinkId=2156292"  # Windows 11 Media Creation Tool products.cab
ARCHITECTURES = {"aarch64": "ARM64", "x86_64": "x64"}
# Microsoft's published generic installation keys: they select an edition, they do not activate Windows.
GENERIC_KEYS = {
    "Windows 11 Pro": "VK7JG-NPHTM-C97JM-9MPGT-3V66T",
    "Windows 11 Home": "YTMG3-N6DKC-DKB77-7M9GH-8HVX7",
    "Windows 11 Education": "YNMGQ-8RYV3-4PGQ3-C8XTP-7CFBY",
}
USER_AGENT = "windows-terminal (+https://github.com/arjunkshah12345-hash/windows-terminal)"
Progress = Callable[[str], None]
STARTUP_NSH = """@echo -off
for %m in fs0 fs1 fs2 fs3 fs4 fs5 fs6 fs7
  if exist %m:\\efi\\boot\\{loader} then
    %m:\\efi\\boot\\{loader}
  endif
endfor
"""


def _quiet(_message: str) -> None:
    pass


def _tool(*names: str, install: str) -> str:
    for name in names:
        path = shutil.which(name)
        if path:
            return path
    raise RuntimeError(f"{names[0]} not found. Install it: {install}")


def _run(command: list[str]) -> str:
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    if result.returncode:
        raise RuntimeError(f"{Path(command[0]).name} failed: {(result.stderr or result.stdout).strip()[-800:]}")
    return result.stdout


def parse_catalog(xml: bytes | str, arch: str, language: str = "en-us", edition: str = "Professional") -> dict:
    """Pick one ESD from products.xml. Consumer ESDs hold Home and Pro; the
    business ESD holds Enterprise/Education."""
    if arch not in ARCHITECTURES:
        raise ValueError("arch must be aarch64 or x86_64")
    root = ElementTree.fromstring(xml)
    available = set()
    for item in root.iter("File"):
        field = {child.tag: (child.text or "").strip() for child in item}
        if field.get("Architecture") != ARCHITECTURES[arch]:
            continue
        available.add(field.get("LanguageCode", ""))
        if field.get("LanguageCode", "").lower() == language.lower() and field.get("Edition") == edition:
            url = field.get("FilePath", "")
            if not re.match(r"^https?://[a-z0-9.-]+\.microsoft\.com/", url):
                raise ValueError(f"Catalog URL is not a Microsoft CDN address: {url}")
            if not re.fullmatch(r"[0-9a-f]{40}", field.get("Sha1", "").lower()):
                raise ValueError("Catalog entry has no valid SHA-1")
            return {"name": field["FileName"], "url": url, "size": int(field["Size"]),
                    "sha1": field["Sha1"].lower(), "language": field["LanguageCode"], "arch": arch,
                    "edition": edition}
    if language.lower() not in {code.lower() for code in available}:
        raise ValueError(f"No {arch} media for language {language!r}; available: {', '.join(sorted(available))}")
    raise ValueError(f"No {edition} {arch} media for {language}")


def _extract_cab(cab: Path, destination: Path) -> Path:
    extractor = shutil.which("cabextract")
    if extractor:
        _run([extractor, "-q", "-d", str(destination), str(cab)])
    else:  # libarchive's bsdtar (macOS tar) reads CAB archives
        tar = _tool("bsdtar", "tar", install="brew install cabextract  /  apt install cabextract")
        _run([tar, "-xf", str(cab), "-C", str(destination)])
    found = destination / "products.xml"
    if not found.is_file():
        raise RuntimeError("products.cab did not contain products.xml")
    return found


def _open(url: str, offset: int = 0, end: int | None = None):
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    if offset or end is not None:
        request.add_header("Range", f"bytes={offset}-{'' if end is None else end - 1}")
    return urllib.request.urlopen(request, timeout=60)


def catalog(arch: str, language: str = "en-us", edition: str = "Professional", opener=_open) -> dict:
    with tempfile.TemporaryDirectory(prefix="wt-catalog-") as scratch:
        cab = Path(scratch) / "products.cab"
        with opener(CATALOG_URL) as response:
            cab.write_bytes(response.read())
        return parse_catalog(_extract_cab(cab, Path(scratch)).read_bytes(), arch, language, edition)


def download(entry: dict, destination: Path, progress: Progress = _quiet, opener=_open) -> Path:
    """Resumable download that is only renamed into place once size and SHA-1 match."""
    partial = destination.with_name(destination.name + ".part")
    if destination.is_file() and destination.stat().st_size == entry["size"]:
        if _sha1(destination) == entry["sha1"]:
            return destination
        destination.unlink()
    offset = partial.stat().st_size if partial.is_file() else 0
    if offset > entry["size"]:
        partial.unlink()
        offset = 0
    if offset < entry["size"]:
        with opener(entry["url"], offset) as response, partial.open("ab" if offset else "wb") as stream:
            if offset and getattr(response, "status", 206) != 206:
                stream.truncate(0)  # server ignored Range: restart
                offset = 0
            done, last = offset, -1
            while chunk := response.read(1 << 20):
                stream.write(chunk)
                done += len(chunk)
                percent = done * 100 // entry["size"]
                if percent != last:
                    progress(f"Downloading {entry['name']}: {percent}% of {entry['size'] / 1e9:.1f} GB")
                    last = percent
    if partial.stat().st_size != entry["size"]:
        raise RuntimeError("Download incomplete; run fetch again to resume")
    progress("Verifying SHA-1 against Microsoft's catalog")
    if _sha1(partial) != entry["sha1"]:
        partial.unlink()
        raise RuntimeError("SHA-1 mismatch: the download was corrupted or tampered with and has been deleted")
    partial.replace(destination)
    return destination


def _reshdr(data: bytes, offset: int = 0) -> tuple[int, int, int, int]:
    """WIM resource header: (stored size, flags, file offset, uncompressed size)."""
    raw = data[offset:offset + 24]
    return int.from_bytes(raw[:7], "little"), raw[7], *struct.unpack("<QQ", raw[8:24])


def _merge(ranges):
    merged = []
    for start, end in sorted(ranges):
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return merged


def download_setup_only(entry: dict, destination: Path, progress: Progress = _quiet, opener=_open,
                        images_needed: int = 3) -> Path:
    """Fetch only the parts of the ESD that hold Setup media, Windows PE and Windows Setup.

    Microsoft's ESDs store images in order, so the first three images live in the
    first ~0.5 GB of a 4.5 GB file. Everything else stays a hole in a sparse file.
    Each downloaded 10 MB chunk is checked against the WIM integrity table, and
    wimlib re-verifies the SHA-1 of every file it extracts.
    """
    size = entry["size"]

    def fetch(start: int, end: int) -> bytes:
        with opener(entry["url"], start, end) as response:
            data = response.read(end - start)
        if len(data) != end - start:
            raise RuntimeError("Microsoft's CDN returned a short range")
        return data

    partial = destination.with_name(destination.name + ".sparse")
    with partial.open("wb") as stream:
        stream.truncate(size)
        header = fetch(0, 208)
        if header[:8] != b"MSWIM\0\0\0":
            raise RuntimeError("Not a WIM/ESD file")
        stream.seek(0)
        stream.write(header)
        tables = [_reshdr(header, offset) for offset in (48, 72, 124)]  # blob table, XML, integrity
        for stored, _flags, offset, _size in tables:
            if stored:
                stream.seek(offset)
                stream.write(fetch(offset, offset + stored))
        table_size, _, table_offset, _ = tables[0]
        stream.flush()
        with partial.open("rb") as reader:
            reader.seek(table_offset)
            table = reader.read(table_size)
            integrity_size, _, integrity_offset, _ = tables[2]
            reader.seek(integrity_offset)
            integrity = reader.read(integrity_size)
        solid, metadata, loose = [], [], []
        for index in range(0, len(table), 50):
            stored, flags, offset, uncompressed = _reshdr(table, index)
            if flags & 0x10 and uncompressed == 0x100000000:
                solid.append((offset, offset + stored))
            elif flags & 0x02:
                metadata.append((offset, offset + stored))
            elif not flags & 0x10:
                loose.append((offset, offset + stored))
        if len(metadata) < images_needed:
            raise RuntimeError("ESD has fewer images than expected")
        cutoff = metadata[images_needed - 1][1]
        wanted = [(a, b) for a, b in solid + metadata + loose if a < cutoff]
        wanted += [(a, min(b, a + (1 << 20))) for a, b in solid if a >= cutoff]  # solid headers + chunk tables
        if not integrity or len(integrity) < 12:
            raise RuntimeError("ESD has no integrity table; use a full download")
        _, count, chunk = struct.unpack("<III", integrity[:12])
        hashes = [integrity[12 + 20 * i:32 + 20 * i] for i in range(count)]
        region_end = table_offset + table_size
        aligned = []
        for start, end in _merge(wanted):  # widen to whole integrity chunks so every byte is verified
            first, last = (max(start, 208) - 208) // chunk, (min(end, region_end) - 1 - 208) // chunk
            aligned.append((208 + first * chunk, min(region_end, 208 + (last + 1) * chunk)))
        ranges = _merge(aligned)
        total, done = sum(b - a for a, b in ranges), 0
        for start, end in ranges:
            for chunk_start in range(start, end, chunk):
                chunk_end = min(end, chunk_start + chunk)
                data = fetch(chunk_start, chunk_end)
                if hashlib.sha1(data).digest() != hashes[(chunk_start - 208) // chunk]:
                    raise RuntimeError("Integrity check failed for a downloaded chunk; nothing was kept")
                stream.seek(chunk_start)
                stream.write(data)
                done += len(data)
                progress(f"Downloading Windows PE + Setup ({total / 1e9:.2f} of {size / 1e9:.1f} GB): {done * 100 // total}%")
    partial.replace(destination)
    return destination


def _sha1(path: Path) -> str:
    digest = hashlib.sha1()
    with path.open("rb") as stream:
        while chunk := stream.read(1 << 22):
            digest.update(chunk)
    return digest.hexdigest()


def images(esd: Path) -> dict[str, int]:
    wimlib = _tool("wimlib-imagex", install="brew install wimlib  /  apt install wimtools")
    names, index = {}, None
    for line in _run([wimlib, "info", str(esd)]).splitlines():
        if match := re.match(r"^Index:\s+(\d+)", line):
            index = int(match.group(1))
        elif (match := re.match(r"^Name:\s+(.+)$", line)) and index is not None:
            names[match.group(1).strip()] = index
    return names


def build_iso(esd: Path, output: Path, image: str = "Windows 11 Pro", setup_only: bool = False,
              progress: Progress = _quiet, delete_esd: bool = False) -> Path:
    wimlib = _tool("wimlib-imagex", install="brew install wimlib  /  apt install wimtools")
    mkisofs = _tool("xorriso", install="brew install xorriso  /  apt install xorriso")
    found = images(esd)
    pe = next((name for name in found if name.startswith("Microsoft Windows PE")), None)
    setup = next((name for name in found if name.startswith("Microsoft Windows Setup")), None)
    if "Windows Setup Media" not in found or not pe or not setup:
        raise RuntimeError("This ESD does not contain Windows Setup media")
    if not setup_only and image not in found:
        editions = [name for name in found if name.startswith("Windows ")]
        raise ValueError(f"{image!r} is not in this ESD; choose one of: {', '.join(n for n in editions if n not in needed)}")
    stage = Path(tempfile.mkdtemp(prefix=".wt-iso-", dir=output.parent))
    try:
        progress("Extracting Windows Setup media")
        _run([wimlib, "apply", str(esd), str(found["Windows Setup Media"]), str(stage)])
        boot = stage / "sources/boot.wim"
        progress("Building boot.wim (Windows PE + Windows Setup)")
        _run([wimlib, "export", str(esd), str(found[pe]), str(boot), "--compress=LZX"])
        _run([wimlib, "export", str(esd), str(found[setup]), str(boot), "--boot"])
        if not setup_only:
            progress(f"Exporting {image} (this recompresses ~3 GB and takes several minutes)")
            install = stage / "sources/install.esd"
            _run([wimlib, "export", str(esd), str(found[image]), str(install), "--solid"])
            if install.stat().st_size >= 4 * 1024 ** 3:
                raise RuntimeError("install.esd exceeds 4 GiB and cannot be stored on an ISO 9660 disc")
        if delete_esd:
            esd.unlink()
        efi = stage / "efi/microsoft/boot/efisys_noprompt.bin"
        if not efi.is_file():
            raise RuntimeError("Setup media has no UEFI boot image")
        loader = next((path.name for path in (stage / "efi/boot").glob("boot*.efi")), None)
        if loader:
            # ArmVirt edk2 can build its boot list before a USB CD finishes enumerating and fall back
            # to the UEFI shell. The shell only sees the El Torito FAT image (fs0:), not ISO 9660,
            # so put a startup.nsh inside that image which hands over to Windows' own loader.
            os.chmod(efi, 0o644)
            script = STARTUP_NSH.format(loader=loader).replace("\n", "\r\n").encode()
            efi.write_bytes(fat12_add_file(efi.read_bytes(), "STARTUP NSH", script))
        progress("Writing bootable ISO")
        partial = output.with_name(output.name + ".part")
        command = [mkisofs, "-as", "mkisofs", "-quiet", "-iso-level", "3", "-J", "-joliet-long", "-r",
                   "-V", "WT_WIN11_" + ("A64" if "arm64" in pe.lower() else "X64")]
        bios = stage / "boot/etfsboot.com"
        if bios.is_file():  # x64 media is dual BIOS/UEFI bootable
            command += ["-b", "boot/etfsboot.com", "-no-emul-boot", "-boot-load-size", "8", "-eltorito-alt-boot"]
        command += ["-e", "efi/microsoft/boot/efisys_noprompt.bin", "-no-emul-boot", "-o", str(partial), str(stage)]
        _run(command)
        partial.replace(output)
        return output
    finally:
        shutil.rmtree(stage, ignore_errors=True)


def fat12_add_file(image: bytes, name: str, data: bytes) -> bytes:
    """Add one file to the root directory of a FAT12 image (the El Torito EFI boot floppy)."""
    if len(name) != 11:
        raise ValueError("name must be an 11-character 8.3 directory name, e.g. 'STARTUP NSH'")
    disk = bytearray(image)
    bps, spc, reserved, fats, root_entries, total, _media, fat_sectors = struct.unpack("<HBHBHHBH", disk[11:24])
    if disk[54:59] != b"FAT12":
        raise ValueError("not a FAT12 image")
    total = total or struct.unpack("<I", disk[32:36])[0]
    fat = reserved * bps
    root = (reserved + fats * fat_sectors) * bps
    data_start = root + root_entries * 32
    cluster_bytes = bps * spc
    clusters = (total * bps - data_start) // cluster_bytes

    def entry(index):
        word = struct.unpack("<H", disk[fat + index * 3 // 2:fat + index * 3 // 2 + 2])[0]
        return word >> 4 if index & 1 else word & 0xFFF

    def set_entry(index, value):
        for copy in range(fats):
            offset = fat + copy * fat_sectors * bps + index * 3 // 2
            word = struct.unpack("<H", disk[offset:offset + 2])[0]
            word = (word & 0x000F) | (value << 4) if index & 1 else (word & 0xF000) | value
            disk[offset:offset + 2] = struct.pack("<H", word)

    if any(disk[root + 32 * i:root + 32 * i + 11] == name.encode() for i in range(root_entries)):
        raise ValueError(f"{name.strip()} already exists in the image")
    slots = [i for i in range(root_entries) if disk[root + 32 * i] in (0x00, 0xE5)]
    needed = max(1, -(-len(data) // cluster_bytes))
    free = [index for index in range(2, clusters + 2) if entry(index) == 0][:needed]
    if not slots or len(free) < needed:
        raise ValueError("no room in the FAT image")
    for current, following in zip(free, free[1:] + [0xFFF]):
        set_entry(current, following)
    for position, cluster in enumerate(free):
        chunk = data[position * cluster_bytes:(position + 1) * cluster_bytes]
        start = data_start + (cluster - 2) * cluster_bytes
        disk[start:start + cluster_bytes] = chunk.ljust(cluster_bytes, b"\0")
    record = name.encode() + bytes([0x20]) + b"\0" * 14 + struct.pack("<HI", free[0], len(data))
    disk[root + 32 * slots[0]:root + 32 * slots[0] + 32] = record
    return bytes(disk)


def unattend_xml(arch: str, user: str, password: str, image: str = "Windows 11 Pro",
                 computer: str = "WT-PC", key: str | None = None) -> str:
    """Answer file for a hands-free install: wipes disk 0 (the VM's own disk),
    installs a local account, skips the Microsoft-account/network OOBE and
    keeps the machine awake for agents."""
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,19}", user) or user.lower() in {"administrator", "guest"}:
        raise ValueError("user must be 1–20 letters, digits, '.', '_' or '-' and not a built-in account")
    if not re.fullmatch(r"[A-Za-z0-9-]{1,15}", computer):
        raise ValueError("computer name must be 1–15 letters, digits or hyphens")
    if not password or len(password) > 127:
        raise ValueError("password must be 1–127 characters")
    key = key or GENERIC_KEYS.get(image)
    cpu = {"aarch64": "arm64", "x86_64": "amd64"}[arch]
    attrs = (f'processorArchitecture="{cpu}" publicKeyToken="31bf3856ad364e35" language="neutral" '
             'versionScope="nonSxS" xmlns:wcm="http://schemas.microsoft.com/WMIConfig/2002/State"')
    bypass = "".join(
        f'<RunSynchronousCommand wcm:action="add"><Order>{order}</Order>'
        f'<Path>reg add HKLM\\SYSTEM\\Setup\\LabConfig /v {value} /t REG_DWORD /d 1 /f</Path></RunSynchronousCommand>'
        for order, value in enumerate(("BypassTPMCheck", "BypassSecureBootCheck", "BypassRAMCheck",
                                       "BypassStorageCheck", "BypassCPUCheck"), 1))
    first_logon = [
        "powercfg /hibernate off",
        "powercfg /change standby-timeout-ac 0",
        "powercfg /change monitor-timeout-ac 0",
        'reg add "HKLM\\SOFTWARE\\Policies\\Microsoft\\Windows\\Personalization" /v NoLockScreen /t REG_DWORD /d 1 /f',
        'cmd /c echo ready> C:\\Windows\\Temp\\windows-terminal-ready.txt',
    ]
    logon = "".join(
        f'<SynchronousCommand wcm:action="add"><Order>{order}</Order><CommandLine>{escape(line)}</CommandLine></SynchronousCommand>'
        for order, line in enumerate(first_logon, 1))
    product_key = f"<ProductKey><Key>{key}</Key><WillShowUI>OnError</WillShowUI></ProductKey>" if key else ""
    user, password, image = escape(user), escape(password), escape(image)
    return f"""<?xml version="1.0" encoding="utf-8"?>
<!-- Generated by windows-terminal. Wipes the VM's virtual disk 0 and installs {image}. -->
<unattend xmlns="urn:schemas-microsoft-com:unattend">
  <settings pass="windowsPE">
    <component name="Microsoft-Windows-International-Core-WinPE" {attrs}>
      <SetupUILanguage><UILanguage>en-US</UILanguage></SetupUILanguage>
      <InputLocale>en-US</InputLocale><SystemLocale>en-US</SystemLocale><UILanguage>en-US</UILanguage><UserLocale>en-US</UserLocale>
    </component>
    <component name="Microsoft-Windows-Setup" {attrs}>
      <RunSynchronous>{bypass}</RunSynchronous>
      <DiskConfiguration>
        <Disk wcm:action="add">
          <DiskID>0</DiskID><WillWipeDisk>true</WillWipeDisk>
          <CreatePartitions>
            <CreatePartition wcm:action="add"><Order>1</Order><Type>EFI</Type><Size>260</Size></CreatePartition>
            <CreatePartition wcm:action="add"><Order>2</Order><Type>MSR</Type><Size>16</Size></CreatePartition>
            <CreatePartition wcm:action="add"><Order>3</Order><Type>Primary</Type><Extend>true</Extend></CreatePartition>
          </CreatePartitions>
          <ModifyPartitions>
            <ModifyPartition wcm:action="add"><Order>1</Order><PartitionID>1</PartitionID><Format>FAT32</Format><Label>System</Label></ModifyPartition>
            <ModifyPartition wcm:action="add"><Order>2</Order><PartitionID>3</PartitionID><Format>NTFS</Format><Label>Windows</Label><Letter>C</Letter></ModifyPartition>
          </ModifyPartitions>
        </Disk>
      </DiskConfiguration>
      <ImageInstall>
        <OSImage>
          <InstallFrom><MetaData wcm:action="add"><Key>/IMAGE/NAME</Key><Value>{image}</Value></MetaData></InstallFrom>
          <InstallTo><DiskID>0</DiskID><PartitionID>3</PartitionID></InstallTo>
          <Compact>true</Compact>
        </OSImage>
      </ImageInstall>
      <UserData><AcceptEula>true</AcceptEula>{product_key}</UserData>
    </component>
  </settings>
  <settings pass="specialize">
    <component name="Microsoft-Windows-Shell-Setup" {attrs}>
      <ComputerName>{computer}</ComputerName>
    </component>
    <component name="Microsoft-Windows-Deployment" {attrs}>
      <RunSynchronous>
        <RunSynchronousCommand wcm:action="add"><Order>1</Order><Path>reg add HKLM\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\OOBE /v BypassNRO /t REG_DWORD /d 1 /f</Path></RunSynchronousCommand>
      </RunSynchronous>
    </component>
  </settings>
  <settings pass="oobeSystem">
    <component name="Microsoft-Windows-International-Core" {attrs}>
      <InputLocale>en-US</InputLocale><SystemLocale>en-US</SystemLocale><UILanguage>en-US</UILanguage><UserLocale>en-US</UserLocale>
    </component>
    <component name="Microsoft-Windows-Shell-Setup" {attrs}>
      <OOBE>
        <HideEULAPage>true</HideEULAPage><HideOEMRegistrationScreen>true</HideOEMRegistrationScreen>
        <HideOnlineAccountScreens>true</HideOnlineAccountScreens><HideWirelessSetupInOOBE>true</HideWirelessSetupInOOBE>
        <ProtectYourPC>3</ProtectYourPC>
      </OOBE>
      <UserAccounts>
        <LocalAccounts>
          <LocalAccount wcm:action="add">
            <Name>{user}</Name><Group>Administrators</Group>
            <Password><Value>{password}</Value><PlainText>true</PlainText></Password>
          </LocalAccount>
        </LocalAccounts>
      </UserAccounts>
      <AutoLogon><Enabled>true</Enabled><Username>{user}</Username><LogonCount>9999999</LogonCount>
        <Password><Value>{password}</Value><PlainText>true</PlainText></Password></AutoLogon>
      <FirstLogonCommands>{logon}</FirstLogonCommands>
    </component>
  </settings>
</unattend>
"""


def answer_iso(xml: str, output: Path) -> Path:
    """A tiny CD holding autounattend.xml; Windows Setup scans removable media for it."""
    with tempfile.TemporaryDirectory(prefix="wt-answer-") as scratch:
        source = Path(scratch) / "autounattend.xml"
        source.write_text(xml, encoding="utf-8")
        partial = output.with_name(output.name + ".part")
        partial.unlink(missing_ok=True)
        tool = shutil.which("xorriso") or shutil.which("mkisofs") or shutil.which("genisoimage")
        if tool:
            prefix = [tool, "-as", "mkisofs"] if Path(tool).name == "xorriso" else [tool]
            _run([*prefix, "-quiet", "-J", "-r", "-V", "WT_ANSWER", "-o", str(partial), scratch])
        elif shutil.which("hdiutil"):
            _run(["hdiutil", "makehybrid", "-quiet", "-iso", "-joliet", "-default-volume-name", "WT_ANSWER",
                  "-o", str(partial), scratch])
        else:
            raise RuntimeError("Creating the answer disc needs xorriso, mkisofs or genisoimage")
        os.chmod(partial, 0o600)
        partial.replace(output)
    return output
