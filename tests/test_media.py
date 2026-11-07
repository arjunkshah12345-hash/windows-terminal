import hashlib
import io
import struct
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from xml.etree import ElementTree

from windows_terminal import media
from windows_terminal.cli import fetched_media, parser

CATALOG = b"""<?xml version="1.0"?><MCT><Catalogs><Catalog><PublishedMedia><Files>
<File id="1"><FileName>26100.1_CLIENTCONSUMER_RET_A64FRE_en-us.esd</FileName><LanguageCode>en-us</LanguageCode>
<Edition>Professional</Edition><Architecture>ARM64</Architecture><Size>10</Size>
<Sha1>c78fd344e845d3b17cb91c40bf4a856459da1b6c</Sha1>
<FilePath>http://dl.delivery.mp.microsoft.com/files/a/26100.1_A64FRE_en-us.esd</FilePath></File>
<File id="2"><FileName>x.esd</FileName><LanguageCode>de-de</LanguageCode><Edition>Professional</Edition>
<Architecture>x64</Architecture><Size>10</Size><Sha1>c78fd344e845d3b17cb91c40bf4a856459da1b6c</Sha1>
<FilePath>http://evil.example.com/x.esd</FilePath></File>
</Files></PublishedMedia></Catalog></Catalogs></MCT>"""


class FakeResponse(io.BytesIO):
    status = 206

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def opener_for(blob, log=None):
    def opener(url, offset=0, end=None):
        if log is not None:
            log.append((offset, end))
        return FakeResponse(blob[offset:end])
    return opener


class CatalogTests(unittest.TestCase):
    def test_picks_matching_arm64_entry(self):
        entry = media.parse_catalog(CATALOG, "aarch64", "en-US")
        self.assertEqual(entry["size"], 10)
        self.assertTrue(entry["url"].startswith("http://dl.delivery.mp.microsoft.com/"))

    def test_rejects_non_microsoft_urls(self):
        with self.assertRaisesRegex(ValueError, "not a Microsoft CDN"):
            media.parse_catalog(CATALOG, "x86_64", "de-de")

    def test_unknown_language_lists_available(self):
        with self.assertRaisesRegex(ValueError, "available: en-us"):
            media.parse_catalog(CATALOG, "aarch64", "fr-fr")


class DownloadTests(unittest.TestCase):
    def setUp(self):
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.dir = Path(scratch.name)
        self.blob = b"windows!" * 1000
        self.entry = {"name": "w.esd", "url": "http://x.microsoft.com/w.esd", "size": len(self.blob),
                      "sha1": hashlib.sha1(self.blob).hexdigest()}

    def test_resumes_partial_and_verifies(self):
        target = self.dir / "w.esd"
        (self.dir / "w.esd.part").write_bytes(self.blob[:3000])
        log = []
        media.download(self.entry, target, opener=opener_for(self.blob, log))
        self.assertEqual(log, [(3000, None)])
        self.assertEqual(target.read_bytes(), self.blob)

    def test_sha1_mismatch_deletes_download(self):
        target = self.dir / "w.esd"
        with self.assertRaisesRegex(RuntimeError, "SHA-1 mismatch"):
            media.download(dict(self.entry, sha1="0" * 40), target, opener=opener_for(self.blob))
        self.assertFalse(target.exists())
        self.assertFalse((self.dir / "w.esd.part").exists())


def reshdr(stored, flags, offset, size):
    return stored.to_bytes(7, "little") + bytes([flags]) + struct.pack("<QQ", offset, size)


def synthetic_esd(chunk=65536):
    """Header | solid A | meta 1 | solid B | meta 2 | blob table | integrity table."""
    body = bytearray(b"\0" * 208)
    layout = {}
    for name, length in (("solidA", 100), ("meta1", 20), ("solidB", 4 << 20), ("meta2", 20)):
        layout[name] = (len(body), length)
        body += bytes([len(layout)]) * length
    table = b""
    for name, flags in (("solidA", 0x14), ("meta1", 0x06), ("solidB", 0x14), ("meta2", 0x06)):
        offset, length = layout[name]
        table += reshdr(length, flags, offset, 0x100000000 if flags & 0x10 else length) + b"\0" * 26
    table_offset = len(body)
    body += table
    region = bytes(body[208:])
    hashes = [hashlib.sha1(region[i:i + chunk]).digest() for i in range(0, len(region), chunk)]
    integrity = struct.pack("<III", 12 + 20 * len(hashes), len(hashes), chunk) + b"".join(hashes)
    integrity_offset = len(body)
    body += integrity
    header = bytearray(b"MSWIM\0\0\0" + b"\0" * 200)
    header[48:72] = reshdr(len(table), 0x02, table_offset, len(table))
    header[124:148] = reshdr(len(integrity), 0, integrity_offset, len(integrity))
    body[:208] = header
    return bytes(body), layout


class SetupOnlyTests(unittest.TestCase):
    def test_downloads_only_leading_images_and_verifies_chunks(self):
        blob, layout = synthetic_esd()
        entry = {"name": "s.esd", "url": "http://x.microsoft.com/s.esd", "size": len(blob), "sha1": "0" * 40}
        log = []
        with tempfile.TemporaryDirectory() as scratch:
            target = Path(scratch) / "s.esd"
            media.download_setup_only(entry, target, opener=opener_for(blob, log), images_needed=1)
            data = target.read_bytes()
        self.assertEqual(len(data), len(blob))
        a_start, a_len = layout["solidA"]
        self.assertEqual(data[a_start:a_start + a_len], blob[a_start:a_start + a_len])
        b_start, b_len = layout["solidB"]
        fetched = sum((end or len(blob)) - start for start, end in log)
        self.assertLess(fetched, len(blob) - (2 << 20))  # most of solid B was never downloaded
        self.assertEqual(data[b_start + b_len - 1000:b_start + b_len], b"\0" * 1000)

    def test_tampered_chunk_is_rejected(self):
        blob, layout = synthetic_esd()
        tampered = bytearray(blob)
        tampered[layout["solidA"][0] + 5] ^= 0xFF
        entry = {"name": "s.esd", "url": "http://x.microsoft.com/s.esd", "size": len(blob), "sha1": "0" * 40}
        with tempfile.TemporaryDirectory() as scratch:
            with self.assertRaisesRegex(RuntimeError, "Integrity check failed"):
                media.download_setup_only(entry, Path(scratch) / "s.esd", opener=opener_for(bytes(tampered)), images_needed=1)
            self.assertFalse((Path(scratch) / "s.esd").exists())


class UnattendTests(unittest.TestCase):
    def test_xml_is_well_formed_and_escapes_secrets(self):
        xml = media.unattend_xml("aarch64", "agent", 'p<&>"w', "Windows 11 Pro")
        root = ElementTree.fromstring(xml.encode())
        values = [element.text for element in root.iter("{urn:schemas-microsoft-com:unattend}Value")]
        self.assertIn('p<&>"w', values)
        self.assertIn("Windows 11 Pro", values)
        self.assertIn('processorArchitecture="arm64"', xml)
        self.assertIn("BypassTPMCheck", xml)
        self.assertIn("VK7JG-NPHTM-C97JM-9MPGT-3V66T", xml)

    def test_rejects_bad_accounts(self):
        for user in ("Administrator", "has space", ""):
            with self.assertRaises(ValueError):
                media.unattend_xml("x86_64", user, "pw")
        with self.assertRaises(ValueError):
            media.unattend_xml("x86_64", "agent", "")


def fat12_image(sectors=720):
    """Blank 360 KB FAT12 floppy: 512-byte sectors, 2 per cluster, 2 FATs of 2 sectors, 112 root entries."""
    disk = bytearray(sectors * 512)
    disk[11:24] = struct.pack("<HBHBHHBH", 512, 2, 1, 2, 112, sectors, 0xFD, 2)
    disk[54:62] = b"FAT12   "
    for copy in range(2):
        start = 512 + copy * 2 * 512
        disk[start:start + 3] = b"\xfd\xff\xff"  # media descriptor + reserved cluster 1
    return bytes(disk)


class Fat12Tests(unittest.TestCase):
    def test_adds_multi_cluster_file_readable_back(self):
        data = bytes(range(256)) * 9  # 2304 bytes = 3 clusters of 1024
        disk = media.fat12_add_file(fat12_image(), "STARTUP NSH", data)
        root = (1 + 2 * 2) * 512
        self.assertEqual(disk[root:root + 11], b"STARTUP NSH")
        first, size = struct.unpack("<HI", disk[root + 26:root + 32])
        self.assertEqual(size, len(data))
        fat = disk[512:512 + 1024]

        def next_cluster(n):
            word = struct.unpack("<H", fat[n * 3 // 2:n * 3 // 2 + 2])[0]
            return word >> 4 if n & 1 else word & 0xFFF

        chain, cluster = [], first
        while cluster < 0xFF8:
            chain.append(cluster)
            cluster = next_cluster(cluster)
        self.assertEqual(len(chain), 3)
        data_start = root + 112 * 32
        content = b"".join(disk[data_start + (c - 2) * 1024:data_start + (c - 1) * 1024] for c in chain)
        self.assertEqual(content[:size], data)
        self.assertEqual(disk[512:512 + 1024], disk[512 + 1024:512 + 2048])  # both FAT copies updated

    def test_refuses_duplicates_and_non_fat12(self):
        disk = media.fat12_add_file(fat12_image(), "STARTUP NSH", b"x")
        with self.assertRaisesRegex(ValueError, "already exists"):
            media.fat12_add_file(disk, "STARTUP NSH", b"y")
        with self.assertRaisesRegex(ValueError, "FAT12"):
            media.fat12_add_file(bytes(4096), "STARTUP NSH", b"x")


class CliTests(unittest.TestCase):
    def test_fetch_and_unattended_flags(self):
        args = parser().parse_args(["fetch", "--setup-only", "--arch", "aarch64"])
        self.assertTrue(args.setup_only)
        args = parser().parse_args(["init", "--iso", "w.iso", "--unattended", "--user", "bot"])
        self.assertTrue(args.unattended)
        self.assertEqual(args.user, "bot")

    def test_fetched_media_detected_by_volume_label(self):
        with tempfile.NamedTemporaryFile() as iso:
            iso.write(b"\0" * (16 * 2048 + 40) + b"WT_WIN11_A64".ljust(32))
            iso.flush()
            self.assertTrue(fetched_media(iso.name))
        self.assertFalse(fetched_media("/nonexistent.iso"))


if __name__ == "__main__":
    unittest.main()
