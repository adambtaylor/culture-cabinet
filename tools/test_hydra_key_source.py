"""Offline synthetic fixtures; tests never invoke OpenSSL or access a network.

Vectors independently generated with /usr/bin/openssl 3.6.5:
  printf %s '<synthetic plaintext>' | /usr/bin/openssl enc -aes-128-cbc \
    -K 0c15c36e90b6e10f0a7dfef169fe3cbc \
    -iv 4e5a4248796472613249563132333435 | /usr/bin/openssl base64 -A
The public AES key is SHA-256(MASTER_KEY)[:16], per the supplied Java source.
Empty plaintext uses printf %s ''; invalid UTF-8 uses printf '\\377'.
Bad padding uses printf '0123456789abcde\\000' and enc's -nopad option.
The control fixture uses printf 'synthetic\\nfixture'. No provider keys exist here.
"""

import unittest
from urllib.parse import parse_qs, urlencode

from hydra_key_source import effective_key


ASCII = "{OBF}UFIwI2O8P4SqK549AaxY1VTLlQZsa8TDYKqJVsPzGMw="
UNICODE = "{OBF}rzRXG3K6Qpv3bjDuqIP4ZNfo0gzKc2xVmSnNfn7X6vU="
SPACED = "{OBF}dIiw3c3mO3Ud4SXUnCVpevi7I6hRU2Ug3zmLwBjIvtg="


class EffectiveKeyTests(unittest.TestCase):
    def assert_invalid(self, value):
        with self.assertRaises(ValueError) as caught:
            effective_key(value)
        self.assertEqual(str(caught.exception), "Invalid credential source")
        self.assertIsNone(caught.exception.__cause__)
        if caught.exception.__context__ is not None:
            self.assertTrue(caught.exception.__suppress_context__)

    def test_openssl_known_vectors(self):
        for stored, expected in (
            (ASCII, "synthetic-caps-fixture"),
            (UNICODE, "合成-é +/&?=#%"),
            (SPACED, " synthetic fixture "),
        ):
            with self.subTest(vector=stored):
                self.assertEqual(effective_key(stored), expected)
                self.assertNotEqual(effective_key(stored), stored)

    def test_caps_url_uses_exact_decoded_key(self):
        query = urlencode({"t": "caps", "apikey": effective_key(UNICODE)})
        self.assertEqual(
            query, "t=caps&apikey=%E5%90%88%E6%88%90-%C3%A9+%2B%2F%26%3F%3D%23%25"
        )
        url = "https://synthetic.invalid/api?" + query
        self.assertEqual(parse_qs(url.split("?", 1)[1]),
                         {"t": ["caps"], "apikey": ["合成-é +/&?=#%"]})
        self.assertNotEqual(query, urlencode({"t": "caps", "apikey": UNICODE}))

    def test_legacy_is_preserved(self):
        for value in ("synthetic-caps-fixture", " 合成-é +/&?=#% ",
                      "x  y", "change_me-with-suffix", "{obf}literal", "a\u00a0b"):
            with self.subTest(value=value):
                self.assertEqual(effective_key(value), value)

    def test_empty_and_wrong_types(self):
        for value in (None, "", b"synthetic", 42, True, [], {}):
            with self.subTest(value=value):
                self.assert_invalid(value)

    def test_placeholders(self):
        for value in ("changeme", "CHANGE_ME", "replace_me", "your_api_key",
                      "your-api-key", "<api_key>", "<apikey>", "${API_KEY}",
                      "REDACTED", "********", "__UNCHANGED__",
                      "{OBF}JbjaW3HAMWh3hPatlJ5Dcg=="):
            with self.subTest(value=value):
                self.assert_invalid(value)

    def test_hydra_unchanged_placeholder(self):
        for value in ("***UNCHANGED***", "***unchanged***", "***UnChAnGeD***",
                      "{OBF}g8A2EOpKs0iFAsK1Rs+8rw=="):
            with self.subTest(value=value):
                self.assert_invalid(value)

    def test_whitespace_and_controls(self):
        for value in (" ", "\t\n", "\u2003", "x\ny", "x\x00y", "x\x7fy",
                      "x\x85y", "\ud800",
                      "{OBF}4pmrntI01KD+L+hUFRxHAovreIqEXVOXgGCKSvJfSDA="):
            with self.subTest(value=value):
                self.assert_invalid(value)

    def test_malformed_obfuscation(self):
        for value in ("{OBF}", "{OBF}!!!!", "{OBF}é", "{OBF}AA==",
                      "{OBF}AAAA", ASCII + "=", ASCII + "\n", ASCII[:-1],
                      ASCII.replace("=", " ="),
                      "{OBF}LGGGTTVSKwB26KWGH0yWGQ==",  # Empty decoded value.
                      "{OBF}6oQZX17dc+E/yfkRKUlhnQ==",  # Invalid UTF-8.
                      "{OBF}EqS6K5HC8SnugzhF7TNiWw==",  # Zero padding byte.
                      "{OBF}LGGGTTVSKwB26KWGH0yWGR=="):  # Noncanonical pad bits.
            with self.subTest(value=value):
                self.assert_invalid(value)

    def test_bounds_in_bytes_and_stored_characters(self):
        self.assertEqual(effective_key("x" * 4096), "x" * 4096)
        self.assertEqual(effective_key("é" * 2048), "é" * 2048)
        for value in ("x" * 4097, "é" * 2049, "x" * 5490,
                      "{OBF}" + "A" * 5485):
            self.assert_invalid(value)


if __name__ == "__main__":
    unittest.main()
