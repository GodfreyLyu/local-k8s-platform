"""Purpose: check pinned image platforms, vendored image references, and the node platform gate offline."""

import json
import sys
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import prerequisites
import runtime
from config import load_config
from io_utils import read_json

VERSIONS = read_json(ROOT / "assets" / "infra" / "versions.json")


class ImageTests(unittest.TestCase):
    # Inputs: none. Outputs: assertions. Purpose: keep every pin runnable on common Minikube hosts.
    # Dependencies: recorded registry platforms in versions.json.
    def test_every_pinned_image_supports_amd64_and_arm64(self):
        for name, item in VERSIONS["images"].items():
            with self.subTest(image=name):
                self.assertIn("@sha256:", item["reference"])
                self.assertTrue({"linux/amd64", "linux/arm64"} <= set(item["platforms"]))

    # Inputs: none. Outputs: assertions. Purpose: keep vendored manifests on the recorded pins.
    # Dependencies: monitoring.json and versions.json.
    def test_monitoring_bundle_uses_recorded_images(self):
        bundle = read_json(ROOT / "assets" / "prerequisites" / "monitoring.json")
        self.assertEqual(bundle["version"], VERSIONS["prerequisites"]["monitoring"]["version"])
        deployment = next(item for item in bundle["items"] if item["kind"] == "Deployment")
        container = deployment["spec"]["template"]["spec"]["containers"][0]
        self.assertEqual(container["image"], VERSIONS["images"]["prometheus_operator"]["reference"])
        reloader = VERSIONS["images"]["prometheus_config_reloader"]["reference"]
        self.assertIn(f"--prometheus-config-reloader={reloader}", container["args"])
        self.assertNotIn("0.95.1", json.dumps(bundle))

    # Inputs: arm64 nodes and an image recorded as amd64-only. Outputs: a blocked check naming it.
    # Purpose: catch platform-specific pins before any Pod is created. Dependencies: runtime gate.
    def test_node_platform_gate_names_unsupported_images(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "environment.json"
            source.write_text(json.dumps({"environment_id": "atlas", "storage": {"class": "x"}}))
            config = load_config(source)
        nodes = [
            {"status": {"nodeInfo": {"operatingSystem": "linux", "architecture": "arm64"}}},
            {"status": {"nodeInfo": {"operatingSystem": "linux", "architecture": "arm64"}}},
        ]
        passed, detail = runtime._image_platforms(config, nodes)
        self.assertTrue(passed)
        self.assertEqual(detail["node_platforms"], ["linux/arm64"])
        images = deepcopy(runtime.IMAGES)
        images["prometheus_operator"]["platforms"] = ["linux/amd64"]
        with patch.object(runtime, "IMAGES", images):
            passed, detail = runtime._image_platforms(config, nodes)
        self.assertFalse(passed)
        self.assertEqual(detail["unsupported_images"], {"prometheus_operator": ["linux/arm64"]})
        config["monitoring"]["enabled"] = False
        with patch.object(runtime, "IMAGES", images):
            self.assertTrue(runtime._image_platforms(config, nodes)[0])

    # Inputs: a Deployment as stored by the API server. Outputs: equivalence assertions.
    # Purpose: avoid false drift after quantity canonicalization (1000m stored as 1). Dependencies: matcher.
    def test_canonical_quantities_match_desired_manifest(self):
        desired = {
            "kind": "Deployment",
            "spec": {
                "template": {
                    "spec": {
                        "containers": [
                            {"resources": {"limits": {"cpu": "1000m", "memory": "1024Mi"}}}
                        ]
                    }
                }
            },
        }
        live = deepcopy(desired)
        live["spec"]["template"]["spec"]["containers"][0]["resources"]["limits"] = {
            "cpu": "1",
            "memory": "1Gi",
        }
        self.assertTrue(prerequisites._matches_document(live, desired))
        live["spec"]["template"]["spec"]["containers"][0]["resources"]["limits"]["cpu"] = "2"
        self.assertFalse(prerequisites._matches_document(live, desired))
        self.assertEqual(
            desired["spec"]["template"]["spec"]["containers"][0]["resources"]["limits"]["cpu"],
            "1000m",
        )


if __name__ == "__main__":
    unittest.main()
