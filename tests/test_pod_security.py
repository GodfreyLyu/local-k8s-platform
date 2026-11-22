"""Purpose: keep Terraform container security contexts compatible with the restricted Pod Security profile."""

import re
import unittest
from pathlib import Path

STACKS = Path(__file__).resolve().parents[1] / "assets" / "infra" / "stacks"


# Inputs: Terraform source lines and a block start index. Outputs: the brace-balanced block text.
# Purpose: read one nested HCL block without a parser. Dependencies: none.
def _block(lines, start):
    depth, collected = 0, []
    for line in lines[start:]:
        depth += line.count("{") - line.count("}")
        collected.append(line)
        if depth == 0:
            break
    return "\n".join(collected)


class PodSecurityTests(unittest.TestCase):
    # Inputs: every stack. Outputs: assertions. Purpose: the kubernetes provider v2 sends an omitted
    # container run_as_non_root as false, overriding the Pod value; restricted namespaces then reject the Pod.
    # Dependencies: stack sources only.
    def test_container_security_contexts_set_run_as_non_root(self):
        found = 0
        for path in sorted(STACKS.rglob("*.tf")):
            lines = path.read_text().splitlines()
            for index, line in enumerate(lines):
                if not re.match(r"\s*security_context\s*\{", line):
                    continue
                block = _block(lines, index)
                # Container-level blocks are the ones that drop capabilities.
                if "capabilities" not in block:
                    continue
                found += 1
                with self.subTest(file=str(path.relative_to(STACKS)), line=index + 1):
                    self.assertRegex(block, r"run_as_non_root\s*=\s*true")
        self.assertGreater(found, 0)


if __name__ == "__main__":
    unittest.main()
