"""Image embeddings for the scanner: DINOv2-small (ONNX, Apache-2.0) turns a slot picture into a
vector that describes what is drawn, so the same item matches even when the background, size or
position differ a little. Much better at telling items apart than the pixel features in scanner.py:
on real 1080p stash scans 87 of 97 slots right and ~2.5 wrong vs 60 right and 9 wrong (cross-validated).

Model: assets/dinov2s_q.onnx (Xenova/dinov2-small, quantized). Reference vectors:
assets/refs_dino.npz, built by tools/build_web_refs.py (game slot renders + slots drawn from wiki /
MetaForge item art for items the icon set lacks).
"""
from __future__ import annotations

import os

import cv2
import numpy as np

MEAN = np.array([0.485, 0.456, 0.406], np.float32)
STD = np.array([0.229, 0.224, 0.225], np.float32)
ICON_ROWS = 0.72          # top part of a slot; the strip below holds the count / category icon


class Embedder:
    def __init__(self, model_path: str):
        import onnxruntime as ort
        so = ort.SessionOptions()
        so.log_severity_level = 3
        self.sess = ort.InferenceSession(model_path, sess_options=so, providers=["CPUExecutionProvider"])
        self.inp = self.sess.get_inputs()[0].name

    @staticmethod
    def _prep(bgr):
        b = bgr[:int(bgr.shape[0] * ICON_ROWS)]
        rgb = cv2.cvtColor(cv2.resize(b, (224, 224), interpolation=cv2.INTER_AREA), cv2.COLOR_BGR2RGB)
        return ((rgb.astype(np.float32) / 255 - MEAN) / STD).transpose(2, 0, 1)

    def embed(self, imgs, batch=1):
        """List of BGR slot pictures -> (N, 768) unit vectors. One picture per run: the quantized
        model scales activations per batch, so a picture's vector would depend on its batch-mates."""
        out = []
        for s in range(0, len(imgs), batch):
            x = np.stack([self._prep(i) for i in imgs[s:s + batch]])
            hs = self.sess.run(None, {self.inp: x})[0]             # [N, 1 + patches, 384]
            out.append(np.concatenate([hs[:, 0], hs[:, 1:].mean(1)], 1))
        v = np.concatenate(out) if out else np.zeros((0, 768), np.float32)
        return (v / np.maximum(np.linalg.norm(v, axis=1, keepdims=True), 1e-6)).astype(np.float32)


def load(assets: str):
    """Embedder + reference vectors, or None when the model files are missing."""
    model, refs = os.path.join(assets, "dinov2s_q.onnx"), os.path.join(assets, "refs_dino.npz")
    if not (os.path.exists(model) and os.path.exists(refs)):
        return None
    r = np.load(refs)
    return Embedder(model), [str(x) for x in r["ids"]], r["emb"].astype(np.float32), [str(x) for x in r["kind"]]
