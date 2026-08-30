"""Deterministic perceptual fingerprinting."""

from __future__ import annotations

import io

from PIL import Image, ImageOps

from product_media.platform_models import content_hash_bytes


def compute_dhash(image_bytes: bytes, *, hash_size: int = 8) -> str:
    with Image.open(io.BytesIO(image_bytes)) as img:
        gray = ImageOps.exif_transpose(img).convert("L").resize(
            (hash_size + 1, hash_size), Image.Resampling.LANCZOS
        )
        pixels = list(gray.getdata())
        bits = []
        for row in range(hash_size):
            row_start = row * (hash_size + 1)
            for col in range(hash_size):
                left = pixels[row_start + col]
                right = pixels[row_start + col + 1]
                bits.append("1" if left > right else "0")
        return hex(int("".join(bits), 2))[2:].rjust(hash_size * hash_size // 4, "0")


def hamming_distance(a: str, b: str) -> int:
    if len(a) != len(b):
        return max(len(a), len(b)) * 4
    return bin(int(a, 16) ^ int(b, 16)).count("1")


def classify_similarity(distance: int, *, duplicate_threshold: int = 5, similar_threshold: int = 12) -> str:
    if distance <= duplicate_threshold:
        return "duplicate"
    if distance <= similar_threshold:
        return "similar"
    return "unrelated"
