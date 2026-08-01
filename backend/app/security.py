"""
Security utilities.

File validation (magic bytes, zip bombs, content types) plus client-IP
resolution for per-IP rate limiting behind reverse proxies.
"""
import ipaddress
import logging
import struct
from typing import Optional

logger = logging.getLogger(__name__)

# Magic bytes (file signatures) for supported media formats
FILE_SIGNATURES: dict[str, list[tuple[int, bytes, str]]] = {
    ".mp4": [
        (0, b"\x00\x00\x00\x18\x66\x74\x79\x70", "MP4 base media"),
        (0, b"\x00\x00\x00\x20\x66\x74\x79\x70", "MP4 base media"),
        (4, b"\x66\x74\x79\x70", "MP4 ftyp box"),  # ftyp anywhere after first 4 bytes
    ],
    ".mov": [
        (4, b"\x66\x74\x79\x70\x71\x74\x20\x20", "MOV QuickTime"),
        (4, b"\x66\x74\x79\x70\x6d\x70\x34\x32", "MOV MPEG4"),
    ],
    ".webm": [
        (0, b"\x1a\x45\xdf\xa3", "WebM/Matroska EBML header"),
    ],
    ".mkv": [
        (0, b"\x1a\x45\xdf\xa3", "MKV/Matroska EBML header"),
    ],
    ".ogv": [
        (0, b"\x4f\x67\x67\x53", "OGG container"),
    ],
    ".ogg": [
        (0, b"\x4f\x67\x67\x53", "OGG audio"),
    ],
    ".mp3": [
        (0, b"\x49\x44\x33", "MP3 ID3 tag"),
        (0, b"\xff\xfb", "MP3 frame sync"),
        (0, b"\xff\xf3", "MP3 frame sync"),
        (0, b"\xff\xf2", "MP3 frame sync"),
    ],
    ".wav": [
        (0, b"\x52\x49\x46\x46", "WAV RIFF header"),
    ],
    ".m4a": [
        (4, b"\x66\x74\x79\x70\x4d\x34\x41\x20", "M4A audio"),
        (4, b"\x66\x74\x79\x70\x69\x73\x6f\x6d", "M4A ISO media"),
    ],
    ".avi": [
        (0, b"\x52\x49\x46\x46", "AVI RIFF header"),
    ],
}

# Known dangerous/archive signatures — reject if found in ANY uploaded file
BLOCKED_SIGNATURES: list[tuple[int, bytes, str]] = [
    (0, b"\x50\x4b\x03\x04", "ZIP archive"),
    (0, b"\x50\x4b\x05\x06", "ZIP archive (empty)"),
    (0, b"\x50\x4b\x07\x08", "ZIP archive (spanned)"),
    (0, b"\x1f\x8b", "GZIP compressed"),
    (0, b"\x1f\x9d", "Z compress"),
    (0, b"\x42\x5a\x68", "BZIP2 compressed"),
    (0, b"\x89\x50\x4e\x47", "PNG image"),
    (0, b"\xff\xd8\xff", "JPEG image"),
    (0, b"\x47\x49\x46\x38", "GIF image"),
    (0, b"\x25\x50\x44\x46", "PDF document"),
    (0, b"\x7f\x45\x4c\x46", "ELF binary"),
    (0, b"\x4d\x5a", "Windows executable"),
    (0, b"\xcf\xfa\xed\xfe", "Mach-O binary"),
    (0, b"\xca\xfe\xba\xbe", "Java class"),
    (0, b"\xab\x0b\x03\x04", "RAR archive"),
    (0, b"\x52\x61\x72\x21", "RAR archive (v1.5+)"),
    (0, b"\x37\x7a\xbc\xaf\x27\x1c", "7z archive"),
    (257, b"\x75\x73\x74\x61\x72", "TAR archive"),
]

# Map extensions to expected MIME types
EXTENSION_MIME_MAP: dict[str, set[str]] = {
    ".mp4": {"video/mp4", "video/mpeg4"},
    ".webm": {"video/webm"},
    ".ogv": {"video/ogg", "application/ogg"},
    ".mov": {"video/quicktime"},
    ".mp3": {"audio/mpeg", "audio/mp3"},
    ".ogg": {"audio/ogg", "application/ogg"},
    ".wav": {"audio/wav", "audio/wave", "audio/x-wav"},
    ".m4a": {"audio/mp4", "audio/x-m4a"},
    ".avi": {"video/x-msvideo", "video/avi"},
    ".mkv": {"video/x-matroska", "video/mkv"},
}

# Maximum compression ratio before flagging as potential zip bomb
MAX_COMPRESSION_RATIO = 50


def read_magic_bytes(file_stream, num_bytes: int = 16) -> Optional[bytes]:
    """Read magic bytes from a file stream without advancing position."""
    try:
        pos = file_stream.tell()
        magic = file_stream.read(num_bytes)
        file_stream.seek(pos)
        return magic
    except Exception as e:
        logger.warning("Failed to read magic bytes: %s", e)
        return None


def validate_file_magic(file_stream, filename: str) -> tuple[bool, str]:
    """
    Validate that the file's magic bytes match its declared extension.
    Also checks for blocked/malicious signatures.
    Returns (is_valid, error_message).
    """
    ext = _get_ext(filename)
    if not ext:
        return False, "File has no extension"

    magic = read_magic_bytes(file_stream, 16)
    if magic is None:
        return False, "Could not read file header"

    # Check for blocked signatures FIRST
    for offset, signature, name in BLOCKED_SIGNATURES:
        # TAR signature is at offset 257 — need to read further
        if offset >= 16:
            file_bytes = _read_range(file_stream, offset, offset + len(signature))
            if file_bytes and file_bytes == signature:
                return False, f"File contains blocked format: {name}"
        elif offset < len(magic):
            if magic[offset:offset + len(signature)] == signature:
                return False, f"File contains blocked format: {name}"

    # Check extension-specific magic bytes
    ext_signatures = FILE_SIGNATURES.get(ext)
    if ext_signatures is None:
        return True, ""

    for offset, signature, _ in ext_signatures:
        if offset < len(magic):
            if magic[offset:offset + len(signature)] == signature:
                return True, ""

    return False, f"File header does not match expected format for {ext} files"


def validate_content_type(content_type: Optional[str], filename: str) -> tuple[bool, str]:
    """
    Validate that the declared Content-Type matches the file extension.
    Returns (is_valid, message).
    If Content-Type is provided and doesn't match, the upload is rejected.
    If no Content-Type is provided, the upload is allowed (browser may omit it).
    """
    if not content_type:
        # Browser may omit Content-Type for multipart file uploads
        return True, "No content type provided — skipping validation"

    ext = _get_ext(filename)
    if not ext:
        return True, "No extension — skipping content-type validation"

    ct_normalized = content_type.split(";")[0].strip().lower()
    expected_types = EXTENSION_MIME_MAP.get(ext, set())
    if not expected_types:
        return True, ""

    if ct_normalized in expected_types:
        return True, ""

    logger.warning("Content-Type mismatch: got '%s', expected one of %s for %s — rejecting",
                   content_type, expected_types, filename)
    return False, f"Content-Type '{content_type}' doesn't match expected type for {ext} files"


def estimate_compression_ratio(file_stream) -> Optional[float]:
    """
    Estimate compression ratio for potential zip bomb detection.
    Returns ratio or None if not applicable.
    """
    try:
        pos = file_stream.tell()
        magic = file_stream.read(4)
        file_stream.seek(pos)

        if magic[:2] == b"\x1f\x8b":  # GZIP
            return _check_gzip_ratio(file_stream)

        # Sample the first 64KB for entropy check
        sample = file_stream.read(64 * 1024)
        file_stream.seek(pos)

        if len(sample) < 1024:
            return None

        unique_bytes = len(set(sample))
        if unique_bytes <= 2:
            logger.warning(
                "File has extremely low entropy (%d unique bytes in %d sample) — possible compression bomb",
                unique_bytes, len(sample),
            )
            return MAX_COMPRESSION_RATIO + 1

        return None
    except Exception as e:
        logger.debug("Compression ratio check skipped: %s", e)
        return None


def _check_gzip_ratio(file_stream) -> Optional[float]:
    """For gzip files, check uncompressed vs compressed size."""
    try:
        pos = file_stream.tell()
        header = file_stream.read(10)
        if len(header) < 10 or header[0] != 0x1f or header[1] != 0x8b:
            file_stream.seek(pos)
            return None

        file_stream.seek(-4, 2)
        uncompressed_size_bytes = file_stream.read(4)
        file_stream.seek(pos)

        uncompressed_size = struct.unpack("<I", uncompressed_size_bytes)[0]
        compressed_size = _get_file_size(file_stream)

        if compressed_size and uncompressed_size > compressed_size:
            return uncompressed_size / compressed_size
        return None
    except Exception:
        return None


def _get_ext(filename: str) -> Optional[str]:
    """Extract the lowercase extension from a filename."""
    import os
    _, ext = os.path.splitext(filename)
    return ext.lower() if ext else None


def _read_range(file_stream, start: int, end: int) -> Optional[bytes]:
    """Read a byte range from the file and reset position."""
    try:
        pos = file_stream.tell()
        file_stream.seek(start)
        data = file_stream.read(end - start)
        file_stream.seek(pos)
        return data
    except Exception:
        return None


def _get_file_size(file_stream) -> Optional[int]:
    """Get total file size."""
    try:
        pos = file_stream.tell()
        file_stream.seek(0, 2)
        size = file_stream.tell()
        file_stream.seek(pos)
        return size
    except Exception:
        return None


def get_client_ip(request) -> str:
    """Best-effort real client IP for per-IP rate limiting.

    Behind the Vercel proxy, ``request.client.host`` is the proxy's edge IP,
    which varies per request — so per-IP limits keyed on it never fire reliably.
    ``X-Forwarded-For`` carries the original client IP as its first entry, so we
    prefer that when present.

    Security note: we trust the first XFF entry unconditionally. Vercel sets
    this header itself for proxied traffic, but a client connecting directly to
    the backend can forge it to rotate past per-IP limits. That is an accepted
    tradeoff for this app (the public surface is the Vercel domain); the value
    is still validated with :func:`ipaddress.ip_address` so forged junk can't
    poison rate-limit keys or in-memory stores, and IPv4 ports are stripped.
    Falls back to the direct peer when the header is absent or malformed.
    """
    peer = "unknown"
    if request and request.client and request.client.host:
        peer = request.client.host
    xff = (request.headers.get("x-forwarded-for") or "").strip()
    if not xff:
        return peer
    candidate = xff.split(",")[0].strip()
    # Strip an IPv4 port if present (e.g. "1.2.3.4:5678"). IPv6 has multiple
    # colons, so the count check keeps IPv6 addresses intact.
    if candidate.count(":") == 1:
        candidate = candidate.rsplit(":", 1)[0].strip()
    try:
        ipaddress.ip_address(candidate)
        return candidate
    except ValueError:
        return peer
