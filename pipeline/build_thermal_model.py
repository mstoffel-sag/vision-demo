#!/usr/bin/env python3
"""
build_thermal_model.py — Build the thermal feature-extraction ONNX model.

The model only extracts temperature features — it does NOT decide alerts.
Thresholding happens in postprocessor.py against the live pipeline.json
config, so the alert threshold can be changed (via Configuration push or
hot-reload) without rebuilding or re-pushing the model.

Run on your laptop. Push the resulting .onnx file to your thin-edge
device via Cumulocity Configuration Management (type: pipeline-model).

Usage:
    python3 build_thermal_model.py
    python3 build_thermal_model.py --height 240 --width 384 --output model.onnx
"""

import argparse
import numpy as np


# ── Protobuf helpers (builds ONNX without the onnx package) ──
def _v(v):
    if v == 0: return b'\x00'
    r = b''
    while v > 0x7f:
        r += bytes([(v & 0x7f) | 0x80]); v >>= 7
    r += bytes([v & 0x7f])
    return r

def _fv(fn, v): return _v((fn << 3) | 0) + _v(v)
def _fb(fn, d):
    if isinstance(d, str): d = d.encode('utf-8')
    return _v((fn << 3) | 2) + _v(len(d)) + d

def _vi(name, et, shape):
    sp = b''
    for d in shape: sp += _fb(1, _fv(1, d))
    return _fb(1, name) + _fb(2, _fb(1, _fv(1, et) + _fb(2, sp)))

def _ai(name, vals):
    a = _fb(1, name) + _fv(20, 7)
    for v in vals: a += _fv(8, v)
    return a

def _ai1(name, v):
    return _fb(1, name) + _fv(20, 2) + _fv(3, v)

def _nd(op, ins, outs, name="", attrs=None):
    n = b''
    for i in ins: n += _fb(1, i)
    for o in outs: n += _fb(2, o)
    if name: n += _fb(3, name)
    n += _fb(4, op)
    if attrs:
        for a in attrs: n += _fb(5, a)
    return n


def build_model(height=120, width=160, grid_rows=6, grid_cols=8, output="model.onnx"):
    """
    Build thermal feature-extraction ONNX model.

    Input:  float32 [1, 1, H, W]  (temperatures in °C)
    Output: max_temp [1, 1]                       — global max after smoothing
            heat_grid [1, 1, grid_rows, grid_cols] — average temp per grid cell

    Pipeline: AveragePool(smooth) → ReduceMax, AveragePool(grid)
    No threshold/alert logic here — postprocessor.py decides alerts.
    """
    cell_h = height // grid_rows
    cell_w = width // grid_cols

    nodes = [
        _nd("AveragePool", ["thermal_input"], ["smoothed"], "smooth", [
            _ai("kernel_shape", [5,5]), _ai("pads", [2,2,2,2]), _ai("strides", [1,1])]),
        _nd("ReduceMax", ["smoothed"], ["max_temp"], "get_max", [
            _ai("axes", [2,3]), _ai1("keepdims", 0)]),
        _nd("AveragePool", ["smoothed"], ["heat_grid"], "pool_grid", [
            _ai("kernel_shape", [cell_h, cell_w]), _ai("strides", [cell_h, cell_w])]),
    ]

    graph = b''
    for nd_data in nodes: graph += _fb(1, nd_data)
    graph += _fb(2, "thermal_feature_extractor")
    graph += _fb(11, _vi("thermal_input", 1, [1, 1, height, width]))
    graph += _fb(12, _vi("max_temp", 1, [1, 1]))
    graph += _fb(12, _vi("heat_grid", 1, [1, 1, grid_rows, grid_cols]))

    model = _fv(1, 8) + _fb(2, "thermal-pipeline") + _fb(8, _fv(2, 17)) + _fb(7, graph)

    with open(output, 'wb') as f:
        f.write(model)
    return len(model)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Build thermal feature-extraction ONNX model")
    parser.add_argument("--output", default="model.onnx")
    parser.add_argument("--height", type=int, default=120)
    parser.add_argument("--width", type=int, default=160)
    parser.add_argument("--grid-rows", type=int, default=6)
    parser.add_argument("--grid-cols", type=int, default=8)
    args = parser.parse_args()

    size = build_model(
        height=args.height, width=args.width,
        grid_rows=args.grid_rows, grid_cols=args.grid_cols,
        output=args.output,
    )
    print(f"Model saved: {args.output} ({size} bytes)")
    print(f"Input shape: [1, 1, {args.height}, {args.width}]")
    print("Thresholding happens in postprocessor.py, not in the model.")

    try:
        import onnxruntime as ort
        sess = ort.InferenceSession(args.output)
        test = np.full((1, 1, args.height, args.width), 50.0, dtype=np.float32)
        r = sess.run(None, {sess.get_inputs()[0].name: test})
        print(f"Validation: max_temp={r[0].flatten()[0]:.1f}°C, heat_grid shape={r[1].shape}")
        print("✅ Model validated!")
    except ImportError:
        print("(onnxruntime not installed — skipping validation)")
