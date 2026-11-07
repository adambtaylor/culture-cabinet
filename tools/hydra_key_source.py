"""Decode an approved Hydra credential field without performing any I/O.

Protocol provenance: supplied public NZBHydra2 v9.1.1 Java source,
commit 8ef3e6489aac8c8d776dca538127be38b2aff962,
SensitiveDataObfuscator.java (constants, SHA-256 truncation, AES/CBC/PKCS5Padding).
MainConfig.java marks apiKey sensitive; ConfigReaderWriter.java decrypts on load.
This fixed public codec is obfuscation, not protection for stored secrets.

Accept canonical padded Base64 emitted by the Java encoder. Limit effective
keys to 4096 UTF-8 bytes and stored text to 5489 characters (the corresponding
maximum padded ciphertext plus prefix). Reject empty/whitespace-only values,
Unicode control characters, and the exact placeholder tokens below (ignoring
case). Meaningful whitespace, Unicode, and URL-reserved characters are preserved.
Invalid UTF-8 is rejected rather than replaced as Java's String constructor does.
Malformed obfuscation never falls back to the stored representation.
"""

import base64
import binascii
import hashlib
import unicodedata

from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes


_PREFIX = "{OBF}"
_KEY = hashlib.sha256(b"NZBHydra2-Obfuscation-Key-2024!").digest()[:16]
_IV = b"NZBHydra2IV12345"
_MAX_KEY_BYTES = 4096
_MAX_STORED_CHARS = 5489
_ERROR = "Invalid credential source"
_PLACEHOLDERS = frozenset({
    "changeme", "change_me", "replace_me", "your_api_key", "your-api-key",
    "<api_key>", "<apikey>", "${api_key}", "redacted", "********",
    "__unchanged__", "***unchanged***",
})


def effective_key(stored):
    """Return the exact effective key, or raise a credential-free ValueError."""
    if not isinstance(stored, str) or not stored or len(stored) > _MAX_STORED_CHARS:
        raise ValueError(_ERROR)

    try:
        value = stored
        if stored.startswith(_PREFIX):
            encoded = stored[len(_PREFIX):]
            ciphertext = base64.b64decode(encoded, validate=True)
            if (not ciphertext or len(ciphertext) % 16
                    or base64.b64encode(ciphertext).decode("ascii") != encoded):
                raise ValueError(_ERROR)
            decryptor = Cipher(algorithms.AES(_KEY), modes.CBC(_IV)).decryptor()
            padded = decryptor.update(ciphertext) + decryptor.finalize()
            unpadder = padding.PKCS7(128).unpadder()
            cleartext = unpadder.update(padded) + unpadder.finalize()
            value = cleartext.decode("utf-8")

        if (not value or value.isspace()
                or len(value.encode("utf-8")) > _MAX_KEY_BYTES
                or value.casefold() in _PLACEHOLDERS
                or any(unicodedata.category(char) == "Cc" for char in value)):
            raise ValueError(_ERROR)
        return value
    except (ValueError, binascii.Error, UnicodeError):
        raise ValueError(_ERROR) from None
