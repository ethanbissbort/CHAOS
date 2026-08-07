"""The asset and point registry.

The registry is the identity layer of the digital twin: it owns what a thing
*is*, how it relates to the rest of the property, what can be measured or
commanded on it, and how the currently installed device happens to expose that
(SDD sections 25-29 and 43-47).

Three modules:

``loader``
    Reads the versioned machine-readable design package and converges the
    database onto it, idempotently.
``points``
    Turns an asset plus its class, profiles and bindings into concrete point
    instances and historian pointers.
``service``
    Read-side queries shared by the API, the EMS, the alarm engine and the
    simulator.
"""

from homestead_twin.registry.loader import (
    LoadResult,
    RegistryLoadError,
    RegistryPackage,
    load_package,
    read_package,
)
from homestead_twin.registry.points import (
    SOURCE_BINDING,
    SOURCE_CLASS,
    SOURCE_PROFILE,
    collect_point_names,
    materialize_points,
)
from homestead_twin.registry.service import (
    AssetPage,
    RelationshipView,
    get_asset,
    get_asset_points,
    get_asset_relationships,
    get_asset_tree,
    get_dependencies,
    get_dependents,
    get_point,
    list_assets,
    list_points,
    registry_summary,
    resolve_topic,
)

__all__ = [
    "SOURCE_BINDING",
    "SOURCE_CLASS",
    "SOURCE_PROFILE",
    "AssetPage",
    "LoadResult",
    "RegistryLoadError",
    "RegistryPackage",
    "RelationshipView",
    "collect_point_names",
    "get_asset",
    "get_asset_points",
    "get_asset_relationships",
    "get_asset_tree",
    "get_dependencies",
    "get_dependents",
    "get_point",
    "list_assets",
    "list_points",
    "load_package",
    "materialize_points",
    "read_package",
    "registry_summary",
    "resolve_topic",
]
