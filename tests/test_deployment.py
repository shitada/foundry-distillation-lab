from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
import shutil
import sys
import time
import unittest
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from foundry_distillation_lab.io import read_json, sha256, write_json
from foundry_distillation_lab.training import deployment


class Arm:
    def __init__(self, plan, existing=False):
        self.plan, self.existing = plan, existing
        self.calls = []
        self.body = {"id": plan["resource_id"], **deepcopy(plan["body"]),
                     "systemData": {"createdAt": "2026-01-01T00:00:00Z"}}

    def request(self, method, resource_id, body=None, headers=None):
        self.calls.append((method, resource_id, body, headers))
        if method == "GET":
            return {"http_status": 200 if self.existing else 404,
                    "body": deepcopy(self.body) if self.existing else {}, "etag": '"one"'}
        if method == "PUT":
            self.existing = True
            return {"http_status": 201, "body": deepcopy(self.body), "etag": '"one"'}
        self.existing = False
        return {"http_status": 202, "body": {}, "etag": None}


class DeploymentTests(unittest.TestCase):
    def setUp(self):
        self.work = ROOT / "runs" / ("test-deployment-" + uuid.uuid4().hex)
        self.work.mkdir(parents=True)
        self.plan = deployment.build_plan(read_json(ROOT / "configs" / "examples" /
                                                   "cloud-deployment.json"))
        self.path = self.work / "deployment.json"
        write_json(self.path, self.plan)

    def tearDown(self):
        for retry in range(6):
            try:
                shutil.rmtree(self.work)
                return
            except PermissionError:
                if retry == 5:
                    raise
                time.sleep(0.1 * 2 ** retry)

    def approval(self, source, operation):
        path = self.work / (operation + "-approval.json")
        write_json(path, {"approved": True, "operation": operation, "input_sha256": sha256(source),
                         "target": self.plan["resource_id"], "max_requests": 2,
                         "max_cost": 100, "currency": "JPY",
                         "expires_at": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()})
        return path

    def test_offline_payload_has_unique_owner_and_explicit_model(self):
        self.assertIn(self.plan["owner"][:12], self.plan["resource_id"])
        self.assertEqual(self.plan["body"]["properties"]["versionUpgradeOption"], "NoAutoUpgrade")
        self.assertEqual(self.plan["api_version"], "2024-10-01")

    def test_no_execute_no_transport(self):
        arm = Arm(self.plan)
        with self.assertRaises(ValueError):
            deployment.deploy(self.path, run_dir=self.work, transport=arm)
        self.assertFalse(arm.calls)

    def test_existing_resource_never_overwritten(self):
        arm = Arm(self.plan, existing=True)
        approval = self.approval(self.path, "deploy")
        with self.assertRaises(ValueError):
            deployment.deploy(self.path, execute=True, approval_path=approval,
                              run_dir=self.work, transport=arm)
        self.assertEqual([c[0] for c in arm.calls], ["GET"])

    def test_create_cleanup_with_ownership(self):
        arm = Arm(self.plan)
        approval = self.approval(self.path, "deploy")
        deployment.deploy(self.path, execute=True, approval_path=approval,
                          run_dir=self.work, transport=arm)
        ownership = self.work / "ownership.json"
        cleanup_approval = self.approval(ownership, "cleanup")
        deployment.cleanup(ownership, execute=True, approval_path=cleanup_approval,
                           run_dir=self.work / "cleanup", transport=arm)
        self.assertEqual([c[0] for c in arm.calls], ["GET", "PUT", "GET", "DELETE"])
        self.assertEqual(arm.calls[1][3], {"If-None-Match": "*"})
        self.assertEqual(arm.calls[-1][3], {"If-Match": '"one"'})
        self.assertEqual(read_json(self.work / "cleanup" / "cleanup-receipt.json")["state"],
                         "deletion_requested_not_verified")

    def test_replaced_resource_not_deleted(self):
        arm = Arm(self.plan)
        approval = self.approval(self.path, "deploy")
        deployment.deploy(self.path, execute=True, approval_path=approval,
                          run_dir=self.work, transport=arm)
        ownership = self.work / "ownership.json"
        cleanup_approval = self.approval(ownership, "cleanup")
        arm.body["systemData"]["createdAt"] = "2026-01-02T00:00:00Z"
        with self.assertRaises(ValueError):
            deployment.cleanup(ownership, execute=True, approval_path=cleanup_approval,
                               run_dir=self.work / "cleanup", transport=arm)
        self.assertNotIn("DELETE", [c[0] for c in arm.calls])

    def test_status_only_get(self):
        arm = Arm(self.plan)
        approval = self.approval(self.path, "deploy")
        deployment.status(self.path, execute=True, approval_path=approval,
                          run_dir=self.work, observation_id="one", transport=arm)
        self.assertEqual([c[0] for c in arm.calls], ["GET"])

    def test_tampered_target_rejected(self):
        self.plan["resource_id"] = "/subscriptions/not-a-resource"
        with self.assertRaises(ValueError):
            deployment.validate_plan(self.plan)


if __name__ == "__main__":
    unittest.main()
