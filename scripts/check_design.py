"""Check the small authored catalogs without credentials or third-party packages."""
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def check():
    roles = json.loads((ROOT / "skills/factory/references/roles.json").read_text())["roles"]
    ids = [role["id"] for role in roles]
    assert len(ids) == len(set(ids)), "Duplicate specialty IDs"
    expected = {"foreman", "product", "ux-research", "ui-design", "accessibility",
                "architecture", "frontend", "backend", "mobile", "data-engineering",
                "security", "qa", "test-automation", "performance", "code-review",
                "sre", "integration", "release", "documentation", "investigator",
                "judge", "standby-reasoner", "observer", "optimizer"}
    assert set(ids) == expected, "Role coverage changed; review the design"
    assert all(role["instructions"] and role["decision_authority"] for role in roles)
    assert all(type(role.get("write_allowed")) is bool for role in roles), "Every role needs an explicit write permission"
    providers = json.loads((ROOT / "config/providers.json").read_text())["routes"]
    assert {route["id"] for route in providers} == {
        "claude-native", "codex-native", "grok-native",
        "antigravity-gemini", "antigravity-claude"}
    assert all(route["readiness"] == "unverified" and not route["models"]
               and route["quota_pool"] is None for route in providers), (
                   "Scaffold must not claim account/model/quota verification")
    questions = json.loads((ROOT / "config/coordination.json").read_text())["questions"]
    assert len({question["id"] for question in questions}) == len(questions)
    assert all(question["options"].get("uncertain") for question in questions), (
        "Every Jev question needs an explicit uncertain outcome")
    for path in (ROOT / "skills").rglob("SKILL.md"):
        text = path.read_text()
        assert text.startswith("---\n") and "name: " in text and "description: " in text
        for target in re.findall(r"\]\(([^)]+)\)", text):
            if "://" not in target:
                assert (path.parent / target).is_file(), f"Missing skill reference: {target}"
    print(f"Design catalogs valid: {len(roles)} roles, {len(providers)} routes, "
          f"{len(questions)} Jev questions; one factory skill.")


if __name__ == "__main__":
    check()
