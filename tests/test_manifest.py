"""Manifest validation -- substitutes for `hassfest`.

hassfest itself lives only in a homeassistant *core git checkout*
(`script/hassfest`), not in the `homeassistant` PyPI wheel this project
depends on (confirmed during milestone 1: `pytest-homeassistant-custom-
component` pulls in `homeassistant` as a normal dependency, but
`homeassistant.scripts.hassfest` does not exist in that installed package).
Running the real hassfest would need a full core checkout as a second,
heavyweight dependency just for this one check.

This test covers hassfest's manifest-schema checks that matter for a
service-type custom integration: required keys, valid enum values, domain
matching the folder name, and (as an end-to-end check hassfest doesn't even
do) that Home Assistant's own loader can actually load the integration and
resolve its config flow. If a real hassfest run becomes available later
(e.g. once CI is set up in a later milestone, per the plan's step 9), this
file's checks are a subset of what it does and can stay as extra coverage
rather than being replaced outright.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from homeassistant.core import HomeAssistant
from homeassistant.loader import async_get_integration

# Home Assistant's own `import custom_components` (inside loader.py) resolves
# whichever `sys.modules["custom_components"]` happens to be cached already;
# if nothing has imported ours yet, that bare top-level name can resolve to
# pytest-homeassistant-custom-component's own bundled stub package instead
# (its testing_config/custom_components/, used for core's own tests) and our
# integration looks "not found". Every other test file in this suite imports
# `custom_components.whatsapp_waha.*` directly, which normally seeds the
# cache correctly before this runs -- but importing it here too makes this
# file's own loader assertion correct even run in isolation.
import custom_components.whatsapp_waha  # noqa: F401,E402

INTEGRATION_DIR = Path(__file__).resolve().parents[1] / "custom_components" / "whatsapp_waha"

VALID_INTEGRATION_TYPES = {
    "device",
    "entity",
    "hardware",
    "helper",
    "hub",
    "service",
    "system",
    "virtual",
}
VALID_IOT_CLASSES = {
    "assumed_state",
    "calculated",
    "cloud_polling",
    "cloud_push",
    "local_polling",
    "local_push",
}
REQUIRED_KEYS = {
    "domain",
    "name",
    "codeowners",
    "config_flow",
    "dependencies",
    "documentation",
    "integration_type",
    "iot_class",
    "issue_tracker",
    "requirements",
    "version",
}
DOMAIN_PATTERN = re.compile(r"^[a-z][a-z0-9_]*$")
VERSION_PATTERN = re.compile(r"^\d+\.\d+\.\d+$")


def _manifest() -> dict:
    return json.loads((INTEGRATION_DIR / "manifest.json").read_text())


def test_manifest_has_all_required_keys():
    manifest = _manifest()
    missing = REQUIRED_KEYS - manifest.keys()
    assert not missing, f"manifest.json is missing required keys: {missing}"


def test_domain_matches_folder_name_and_is_valid_snake_case():
    manifest = _manifest()
    assert manifest["domain"] == INTEGRATION_DIR.name
    assert DOMAIN_PATTERN.match(manifest["domain"]), "domain must be lowercase snake_case"


def test_version_is_a_valid_semver_string():
    manifest = _manifest()
    assert VERSION_PATTERN.match(manifest["version"]), f"invalid version: {manifest['version']!r}"


def test_codeowners_are_github_handles():
    manifest = _manifest()
    assert isinstance(manifest["codeowners"], list) and manifest["codeowners"]
    assert all(c.startswith("@") for c in manifest["codeowners"])


def test_integration_type_and_iot_class_are_known_values():
    manifest = _manifest()
    assert manifest["integration_type"] in VALID_INTEGRATION_TYPES
    assert manifest["iot_class"] in VALID_IOT_CLASSES


def test_dependencies_and_after_dependencies_are_lists():
    manifest = _manifest()
    assert isinstance(manifest["dependencies"], list)
    assert isinstance(manifest.get("after_dependencies", []), list)
    # WI-3: the manifest must declare `webhook` -- the /api/webhook/{id} route
    # only exists if that component is set up first (fixes scaffold bug S2).
    assert "webhook" in manifest["dependencies"]


def test_documentation_and_issue_tracker_are_urls():
    manifest = _manifest()
    assert manifest["documentation"].startswith("http")
    assert manifest["issue_tracker"].startswith("http")


def test_requirements_is_a_list():
    manifest = _manifest()
    assert isinstance(manifest["requirements"], list)


def test_config_flow_is_true():
    manifest = _manifest()
    assert manifest["config_flow"] is True


async def test_home_assistant_can_load_the_integration_and_its_config_flow(
    hass: HomeAssistant,
) -> None:
    """Beyond schema validation: prove HA's own loader accepts this manifest
    and can resolve the config flow class hassfest ultimately exists to protect."""
    integration = await async_get_integration(hass, "whatsapp_waha")
    assert integration.domain == "whatsapp_waha"
    flow_cls = await integration.async_get_platform("config_flow")
    assert hasattr(flow_cls, "WahaConfigFlow")
