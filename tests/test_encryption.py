import base64
import json
import os
import unittest
from unittest.mock import patch

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from security.encryption import (
    DecryptionError,
    EncryptedPayload,
    EncryptedStore,
    EncryptionService,
    EncryptionUnavailableError,
)


def _service(key=None, key_id="v1"):
    if key is None:
        key = AESGCM.generate_key(bit_length=256)
    return EncryptionService(key=key, key_id=key_id), key


class EncryptionTests(unittest.TestCase):

    def test_a_ciphertext_is_not_plaintext(self):
        service, _ = _service()
        payload = service.encrypt("secret")
        self.assertNotEqual(payload.serialize(), "secret")
        self.assertNotIn("secret", payload.serialize())

    def test_b_round_trip(self):
        service, _ = _service()
        self.assertEqual(service.decrypt(service.encrypt("hello panda")), "hello panda")

    def test_c_same_plaintext_different_ciphertext(self):
        service, _ = _service()
        first = service.encrypt("same")
        second = service.encrypt("same")
        self.assertNotEqual(first.serialize(), second.serialize())
        self.assertEqual(service.decrypt(first), "same")
        self.assertEqual(service.decrypt(second), "same")

    def test_d_wrong_key_fails(self):
        first, _ = _service()
        second, _ = _service()
        payload = first.encrypt("classified")
        with self.assertRaises(DecryptionError):
            second.decrypt(payload)

    def test_e_tampered_ciphertext_fails(self):
        service, _ = _service()
        payload = service.encrypt("classified")
        raw = bytearray(base64.urlsafe_b64decode(payload.ciphertext_b64))
        raw[0] ^= 0x01
        tampered = EncryptedPayload(
            version=payload.version,
            key_id=payload.key_id,
            algorithm=payload.algorithm,
            nonce_b64=payload.nonce_b64,
            ciphertext_b64=base64.urlsafe_b64encode(bytes(raw)).decode("ascii"),
        )
        with self.assertRaises(DecryptionError):
            service.decrypt(tampered)

    def test_f_missing_key_fails_closed(self):
        with patch.dict(os.environ, {"PANDA_ENCRYPTION_KEY": ""}, clear=False):
            service = EncryptionService.from_env()
            with self.assertRaises(EncryptionUnavailableError):
                service.encrypt("secret")

    def test_j_payload_has_version_and_key_id_not_plaintext(self):
        service, _ = _service(key_id="k-rotate")
        payload = service.encrypt("top-secret-value")
        data = payload.to_dict()
        self.assertEqual(data["v"], 1)
        self.assertEqual(data["kid"], "k-rotate")
        self.assertEqual(data["alg"], "AESGCM")
        self.assertIn("n", data)
        self.assertIn("ct", data)
        serialized = json.dumps(data)
        self.assertNotIn("top-secret-value", serialized)

    def test_k_base64_alone_is_not_encryption(self):
        service, _ = _service()
        payload = service.encrypt("secret")
        encoded = base64.b64encode(b"secret").decode("ascii")
        self.assertNotEqual(payload.ciphertext_b64, encoded)
        self.assertEqual(payload.algorithm, "AESGCM")

    def test_encrypted_store_round_trip(self):
        service, _ = _service()
        store = EncryptedStore(service)
        store.put("note", "sensitive-business", "sensitive")
        self.assertEqual(store.get("note"), "sensitive-business")
        stored = store._items["note"][1]
        self.assertNotIn("sensitive-business", stored.serialize())


if __name__ == "__main__":
    unittest.main()
