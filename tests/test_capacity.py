"""Purpose: test small-cluster memory accounting and joint volume placement with private synthetic evidence."""

import sys
import unittest
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from capacity import assess, pod_memory, quantity_mib


# Inputs: node name, measured memory/disk and optional allocatable memory. Outputs: Node and measurement.
# Purpose: supply realistic independent scheduler and actual-capacity evidence. Dependencies: Kubernetes quantity syntax.
def node(name, memory, disk, allocatable="8Gi"):
    return (
        {"metadata": {"name": name}, "status": {"allocatable": {"memory": allocatable}}},
        {"name": name, "memory_available_mib": memory, "disk_available_mib": disk},
    )


class CapacityTests(unittest.TestCase):
    # Inputs: the recorded three-node capacity pattern. Outputs: valid worker volume and split Kafka placement.
    # Purpose: prevent whole-stage memory and every-node disk checks from rejecting a feasible local deployment. Dependencies: assess.
    def test_small_services_fit_workers_despite_low_control_node_disk(self):
        evidence = [
            node("minikube", 2542, 1487),
            node("minikube-m02", 563, 13712),
            node("minikube-m03", 817, 11832),
        ]
        workloads = [
            {"name": "postgres", "memory_mib": 320, "storage_mib": 1024},
            {"name": "kafka", "memory_mib": 512, "storage_mib": 1024},
            {"name": "entity-operator", "memory_mib": 256, "storage_mib": 0},
        ]
        report = assess(
            [item[0] for item in evidence],
            [],
            [item[1] for item in evidence],
            workloads,
            ["minikube-m02", "minikube-m03"],
            512,
        )
        self.assertTrue(all(report["checks"].values()), report)
        placements = {item["name"]: item["node"] for item in report["allocations"]}
        self.assertEqual(placements["kafka"], "minikube-m03")
        self.assertEqual(placements["postgres"], "minikube-m02")
        self.assertEqual(report["new_memory_requests_mib"], 1088)
        self.assertEqual(report["new_storage_mib"], 2048)

    # Inputs: two volumes that each fit separately but exceed shared disk capacity. Outputs: blocked disk allocation.
    # Purpose: prevent counting the same disk twice. Dependencies: cumulative placement budgets.
    def test_volumes_cannot_reuse_the_same_disk_headroom(self):
        machine, measurement = node("worker", 4096, 2048)
        workloads = [
            {"name": name, "memory_mib": 128, "storage_mib": 1024} for name in ("one", "two")
        ]
        report = assess([machine], [], [measurement], workloads, ["worker"], 512)
        self.assertFalse(report["checks"]["measured_disk"])
        self.assertIsNone(report["allocations"])

    # Inputs: free physical memory but almost fully requested allocatable memory. Outputs: scheduler-capacity blocker.
    # Purpose: distinguish scheduling reservations from measured memory use. Dependencies: assigned Pod requests.
    def test_assigned_requests_can_block_an_otherwise_idle_node(self):
        machine, measurement = node("worker", 3000, 4096, "1Gi")
        pod = {
            "spec": {
                "nodeName": "worker",
                "containers": [{"resources": {"requests": {"memory": "900Mi"}}}],
            }
        }
        report = assess(
            [machine],
            [pod],
            [measurement],
            [{"name": "new", "memory_mib": 128, "storage_mib": 0}],
            [],
            512,
        )
        self.assertTrue(report["checks"]["measured_memory"])
        self.assertFalse(report["checks"]["scheduler_memory"])
        self.assertEqual(report["nodes"][0]["memory_schedulable_mib"], 124)

    # Inputs: init containers, a restartable sidecar and Pod overhead. Outputs: the peak effective memory request.
    # Purpose: account for scheduler-relevant startup memory. Dependencies: Kubernetes init-container semantics.
    def test_init_sidecars_and_overhead_are_counted(self):
        pod = {
            "spec": {
                "containers": [{"resources": {"requests": {"memory": "200Mi"}}}],
                "initContainers": [
                    {"restartPolicy": "Always", "resources": {"requests": {"memory": "64Mi"}}},
                    {"resources": {"requests": {"memory": "600Mi"}}},
                ],
                "overhead": {"memory": "16Mi"},
            }
        }
        self.assertEqual(pod_memory(pod), 680)

    # Inputs: a completed Pod's reservation. Outputs: capacity available to new work.
    # Purpose: avoid charging terminated workloads against scheduler capacity. Dependencies: Pod phase.
    def test_completed_pods_do_not_reserve_memory(self):
        machine, measurement = node("worker", 2048, 4096, "1Gi")
        pod = {
            "status": {"phase": "Succeeded"},
            "spec": {
                "nodeName": "worker",
                "containers": [{"resources": {"requests": {"memory": "1Gi"}}}],
            },
        }
        report = assess(
            [machine],
            [pod],
            [measurement],
            [{"name": "new", "memory_mib": 256, "storage_mib": 0}],
            [],
            512,
        )
        self.assertTrue(all(report["checks"].values()))

    # Inputs: an assignment where greedy memory placement consumes the only large-disk node. Outputs: a feasible alternative.
    # Purpose: jointly respect disk and memory constraints. Dependencies: bounded backtracking.
    def test_joint_placement_can_reconsider_a_memory_only_choice(self):
        first, first_stats = node("a", 628, 4096)
        second, second_stats = node("b", 528, 1536)
        workloads = [
            {"name": "memory", "memory_mib": 400, "storage_mib": 512},
            {"name": "disk", "memory_mib": 300, "storage_mib": 0, "ephemeral_mib": 2048},
        ]
        report = assess([first, second], [], [first_stats, second_stats], workloads, [], 512)
        self.assertTrue(all(report["checks"].values()), report)
        self.assertEqual(
            {item["name"]: item["node"] for item in report["allocations"]},
            {"memory": "b", "disk": "a"},
        )

    # Inputs: an unschedulable node with ample resources. Outputs: no forecast placement.
    # Purpose: keep cordoned or NoSchedule nodes out of new allocations. Dependencies: Node specs.
    def test_unschedulable_nodes_are_excluded(self):
        for spec in (
            {"unschedulable": True},
            {"taints": [{"effect": "NoSchedule", "key": "dedicated"}]},
        ):
            machine, measurement = node("worker", 4096, 4096)
            machine["spec"] = spec
            report = assess(
                [machine],
                [],
                [measurement],
                [{"name": "new", "memory_mib": 128, "storage_mib": 0}],
                [],
                512,
            )
            self.assertIsNone(report["allocations"])

    # Inputs: live Kubernetes quantities and an invalid unit. Outputs: exact values or rejection.
    # Purpose: support real allocatable and Pod request formats without weakening user-input validation. Dependencies: Decimal.
    def test_kubernetes_quantities_and_limit_only_containers(self):
        self.assertEqual(quantity_mib("1024Ki"), 1)
        self.assertEqual(quantity_mib("1.5Gi"), 1536)
        self.assertEqual(quantity_mib("1e6"), Decimal(1000000) / 1048576)
        self.assertEqual(
            pod_memory({"spec": {"containers": [{"resources": {"limits": {"memory": "256M"}}}]}}),
            245,
        )
        with self.assertRaises(ValueError):
            quantity_mib("large")


if __name__ == "__main__":
    unittest.main()
