#!/usr/bin/env python3
"""
tedge-pipeline-runner — Generic ONNX inference pipeline for thin-edge.io

This is the use-case-agnostic orchestrator. It:
  1. Reads pipeline.json config
  2. Loads preprocessor.py and postprocessor.py dynamically
  3. Loads the ONNX model
  4. Runs the loop: preprocess → inference → postprocess
  5. Publishes metrics, health, and lifecycle events via thin-edge MQTT

Install once via Cumulocity Software Management (apt).
Push use-case-specific files via Cumulocity Configuration Management.
"""

import os
import sys
import json
import time
import hashlib
import logging
import argparse
import importlib.util
import traceback
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

try:
    import onnxruntime as ort
    HAS_ORT = True
except ImportError:
    HAS_ORT = False

try:
    import paho.mqtt.client as mqtt
    HAS_MQTT = True
except ImportError:
    HAS_MQTT = False

# ═══════════════════════════════════════════════════════════════
# LOGGING
# ═══════════════════════════════════════════════════════════════
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("pipeline-runner")

# ═══════════════════════════════════════════════════════════════
# DEFAULTS
# ═══════════════════════════════════════════════════════════════
BASE_DIR = Path("/opt/tedge-pipeline")
DEFAULT_CONFIG = {
    "pipeline_name": "unnamed-pipeline",
    "capture_interval_sec": 10,
    "preprocessor_path": str(BASE_DIR / "processors" / "preprocessor.py"),
    "postprocessor_path": str(BASE_DIR / "processors" / "postprocessor.py"),
    "model_path": str(BASE_DIR / "models" / "model.onnx"),
    "data_dir": str(BASE_DIR / "data"),
    "mqtt_host": "localhost",
    "mqtt_port": 1883,
    "service_name": "tedge-pipeline-runner",
    "settings": {},
}


# ═══════════════════════════════════════════════════════════════
# MODULE LOADER — loads preprocessor.py / postprocessor.py
# ═══════════════════════════════════════════════════════════════
def load_module(path, module_name):
    """Dynamically load a Python module from a file path."""
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"Module not found: {path}")
    spec = importlib.util.spec_from_file_location(module_name, str(path))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def file_hash(path):
    """Get MD5 hash of a file for change detection."""
    try:
        return hashlib.md5(Path(path).read_bytes()).hexdigest()[:12]
    except Exception:
        return None


# ═══════════════════════════════════════════════════════════════
# MQTT PUBLISHER — thin-edge measurements, events, service health
# ═══════════════════════════════════════════════════════════════
class MQTTPublisher:
    """Publishes to thin-edge.io local MQTT broker."""

    def __init__(self, host="localhost", port=1883, service_name="tedge-pipeline-runner",
                 dry_run=False):
        self.dry_run = dry_run
        self.service_name = service_name
        self.client = None

        if not dry_run:
            if not HAS_MQTT:
                raise ImportError("paho-mqtt not installed. Run: pip3 install paho-mqtt")
            self.client = mqtt.Client()
            self.client.connect(host, port)
            self.client.loop_start()
            log.info(f"MQTT connected: {host}:{port}")
        else:
            log.info("MQTT: dry-run mode")

    def publish_measurement(self, measurement_type, values, timestamp=None):
        """Publish a measurement to Cumulocity via thin-edge, attached to the
        top-level device (no service/externalId lookup needed to find it)."""
        ts = timestamp or datetime.now(timezone.utc).isoformat()
        # thin-edge's c8y measurement flow silently drops values that carry
        # an inline "unit" key (units are registered separately via a /meta
        # topic) — strip it so metrics with units aren't dropped.
        payload = {
            k: {"value": v["value"]} if isinstance(v, dict) and "value" in v else v
            for k, v in values.items()
        }
        payload["time"] = ts
        self._pub(f"te/device/main///m/{measurement_type}", payload, f"measurement:{measurement_type}")

    def publish_event(self, event_type, text, extras=None):
        """Publish an event to Cumulocity via thin-edge."""
        payload = {
            "type": event_type,
            "text": text,
            "time": datetime.now(timezone.utc).isoformat(),
        }
        if extras:
            payload[event_type] = extras
        self._pub("te/device/main///e/", payload, f"event:{event_type}")

    def publish_service_health(self, status="up"):
        """Publish service health status (shows in Services tab)."""
        topic = f"te/device/main/service/{self.service_name}/status/health"
        payload = {"status": status, "time": datetime.now(timezone.utc).isoformat()}
        self._pub(topic, payload, f"health:{status}")

    def publish_alarm(self, alarm_type, severity, text, extras=None):
        """Raise (or update) a Cumulocity alarm via thin-edge, attached to the
        top-level device. Alarms are stateful, so this is published retained —
        it stays active until clear_alarm() is called."""
        payload = {
            "text": text,
            "severity": severity,
            "time": datetime.now(timezone.utc).isoformat(),
        }
        if extras:
            payload.update(extras)
        topic = f"te/device/main///a/{alarm_type}"
        self._pub(topic, payload, f"alarm:{alarm_type}", retain=True)

    def clear_alarm(self, alarm_type):
        """Clear a previously raised alarm (empty retained message, per thin-edge convention)."""
        topic = f"te/device/main///a/{alarm_type}"
        if self.dry_run:
            log.info(f"[MQTT DRY] clear_alarm:{alarm_type} → {topic}")
        else:
            self.client.publish(topic, "", retain=True)

    def _pub(self, topic, payload, label="", retain=False):
        msg = json.dumps(payload)
        if self.dry_run:
            log.info(f"[MQTT DRY] {label} → {topic}")
            log.debug(f"  {msg[:200]}")
        else:
            self.client.publish(topic, msg, retain=retain)

    def stop(self):
        if self.client:
            self.client.loop_stop()
            self.client.disconnect()


# ═══════════════════════════════════════════════════════════════
# PIPELINE ENGINE — the main orchestrator
# ═══════════════════════════════════════════════════════════════
class PipelineEngine:
    """
    Generic pipeline: preprocess → ONNX inference → postprocess.
    Use-case agnostic. Swap preprocessor/postprocessor/model for any scenario.
    """

    def __init__(self, config, dry_run=False):
        self.config = config
        self.dry_run = dry_run
        self.cycle_count = 0

        # Track file hashes for hot-reload detection
        self._file_hashes = {}

        # Initialize MQTT
        self.mqtt = MQTTPublisher(
            host=config.get("mqtt_host", "localhost"),
            port=config.get("mqtt_port", 1883),
            service_name=config.get("service_name", "tedge-pipeline-runner"),
            dry_run=dry_run,
        )

        # Load components
        self._load_all()

        # Publish startup event
        self.mqtt.publish_event(
            "c8y_PipelineLifecycle",
            f"Pipeline '{config['pipeline_name']}' started",
            {
                "pipeline_name": config["pipeline_name"],
                "model_path": config["model_path"],
                "preprocessor": Path(config["preprocessor_path"]).name,
                "postprocessor": Path(config["postprocessor_path"]).name,
                "action": "started",
            },
        )
        self.mqtt.publish_service_health("up")

    def _load_all(self):
        """Load or reload preprocessor, postprocessor, and model."""
        self._load_preprocessor()
        self._load_postprocessor()
        self._load_model()

    def _load_preprocessor(self):
        path = self.config["preprocessor_path"]
        self.preprocessor = load_module(path, "preprocessor")
        self._file_hashes["preprocessor"] = file_hash(path)
        log.info(f"Preprocessor loaded: {Path(path).name}")

    def _load_postprocessor(self):
        path = self.config["postprocessor_path"]
        self.postprocessor = load_module(path, "postprocessor")
        self._file_hashes["postprocessor"] = file_hash(path)
        log.info(f"Postprocessor loaded: {Path(path).name}")

    def _load_model(self):
        if not HAS_ORT:
            raise ImportError("onnxruntime not installed")
        path = self.config["model_path"]
        self.session = ort.InferenceSession(path)
        self.input_name = self.session.get_inputs()[0].name
        self.input_shape = self.session.get_inputs()[0].shape
        self.output_names = [o.name for o in self.session.get_outputs()]
        self._file_hashes["model"] = file_hash(path)

        model_size = Path(path).stat().st_size
        log.info(f"ONNX model loaded: {Path(path).name} ({model_size} bytes)")
        log.info(f"  Input:  {self.input_name} {self.input_shape}")
        log.info(f"  Outputs: {self.output_names}")

        self.mqtt.publish_event(
            "c8y_PipelineLifecycle",
            f"Model loaded: {Path(path).name}",
            {
                "action": "model_loaded",
                "model_file": Path(path).name,
                "model_size_bytes": model_size,
                "input_name": self.input_name,
                "input_shape": str(self.input_shape),
                "output_names": str(self.output_names),
            },
        )

    def check_for_updates(self):
        """Check if any files changed on disk (pushed via Configuration Management)."""
        reloaded = []

        for key, path_key in [
            ("preprocessor", "preprocessor_path"),
            ("postprocessor", "postprocessor_path"),
            ("model", "model_path"),
        ]:
            current_hash = file_hash(self.config[path_key])
            if current_hash and current_hash != self._file_hashes.get(key):
                log.info(f"Change detected in {key} — reloading...")
                try:
                    if key == "preprocessor":
                        self._load_preprocessor()
                    elif key == "postprocessor":
                        self._load_postprocessor()
                    elif key == "model":
                        self._load_model()
                    reloaded.append(key)
                except Exception as e:
                    log.error(f"Failed to reload {key}: {e}")

        return reloaded

    def run_cycle(self):
        """Execute one pipeline cycle: preprocess → inference → postprocess."""
        self.cycle_count += 1
        cycle_start = time.time()

        # ── Preprocess ──
        t0 = time.time()
        pre_result = self.preprocessor.get_input(self.config, self.cycle_count)
        preprocess_ms = (time.time() - t0) * 1000

        input_data = pre_result["input"]
        metadata = pre_result.get("metadata", {})

        # ── ONNX Inference ──
        t0 = time.time()
        model_outputs = self.session.run(None, {self.input_name: input_data})
        inference_ms = (time.time() - t0) * 1000

        # ── Postprocess ──
        t0 = time.time()
        post_result = self.postprocessor.handle_output(
            self.config, model_outputs, metadata, self.mqtt
        )
        postprocess_ms = (time.time() - t0) * 1000

        cycle_ms = (time.time() - cycle_start) * 1000

        # ── Publish performance metrics ──
        status = post_result.get("status", "unknown")
        custom_metrics = post_result.get("metrics", {})

        perf_values = {
            "inference_time": {"value": round(inference_ms, 2), "unit": "ms"},
            "cycle_time": {"value": round(cycle_ms, 2), "unit": "ms"},
        }

        # Add custom metrics from postprocessor
        for k, v in custom_metrics.items():
            if isinstance(v, dict):
                perf_values[k] = v
            else:
                perf_values[k] = {"value": v, "unit": ""}

        self.mqtt.publish_measurement("c8y_PipelineMetrics", perf_values)

        # ── Log ──
        log.info(
            f"Cycle {self.cycle_count} | {status.upper()} | "
            f"pre={preprocess_ms:.0f}ms inf={inference_ms:.0f}ms "
            f"post={postprocess_ms:.0f}ms total={cycle_ms:.0f}ms"
        )

        return post_result

    def run(self):
        """Main loop."""
        interval = self.config.get("capture_interval_sec", 10)
        log.info(f"Pipeline running: interval={interval}s")

        try:
            while True:
                # Check for file updates (hot reload)
                self.check_for_updates()

                # Run one cycle
                try:
                    self.run_cycle()
                except Exception as e:
                    log.error(f"Cycle error: {e}")
                    log.debug(traceback.format_exc())
                    self.mqtt.publish_event(
                        "c8y_PipelineLifecycle",
                        f"Pipeline error: {e}",
                        {"action": "error", "error": str(e)},
                    )

                time.sleep(interval)

        except KeyboardInterrupt:
            log.info("Shutting down...")
        finally:
            self.mqtt.publish_service_health("down")
            self.mqtt.publish_event(
                "c8y_PipelineLifecycle",
                f"Pipeline '{self.config['pipeline_name']}' stopped",
                {"action": "stopped", "total_cycles": self.cycle_count},
            )
            self.mqtt.stop()
            log.info(f"Stopped after {self.cycle_count} cycles.")


# ═══════════════════════════════════════════════════════════════
# ENTRY POINT
# ═══════════════════════════════════════════════════════════════
def main():
    parser = argparse.ArgumentParser(description="tedge-pipeline-runner")
    parser.add_argument(
        "--config",
        default=str(BASE_DIR / "config" / "pipeline.json"),
        help="Path to pipeline.json",
    )
    parser.add_argument("--dry-run", action="store_true", help="No MQTT/HTTP, just print")
    args = parser.parse_args()

    # Load config
    config = DEFAULT_CONFIG.copy()
    config_path = Path(args.config)
    if config_path.exists():
        with open(config_path) as f:
            user_config = json.load(f)
        config.update(user_config)
        log.info(f"Config loaded: {config_path}")
    else:
        log.warning(f"Config not found: {config_path} — using defaults")

    log.info("=" * 60)
    log.info("TEDGE PIPELINE RUNNER")
    log.info(f"  Pipeline:      {config['pipeline_name']}")
    log.info(f"  Model:         {config['model_path']}")
    log.info(f"  Preprocessor:  {config['preprocessor_path']}")
    log.info(f"  Postprocessor: {config['postprocessor_path']}")
    log.info(f"  Data dir:      {config['data_dir']}")
    log.info(f"  Interval:      {config['capture_interval_sec']}s")
    log.info(f"  Dry run:       {args.dry_run}")
    log.info("=" * 60)

    engine = PipelineEngine(config, dry_run=args.dry_run)
    engine.run()


if __name__ == "__main__":
    main()
