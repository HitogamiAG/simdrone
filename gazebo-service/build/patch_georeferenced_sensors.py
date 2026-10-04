"""Compatibility fixes for Gazebo Sim 8.15.0's heading-blind sensor vectors.

Upstream sources are fetched at the pinned commit by Docker, never vendored.
Keep the legacy NED magnetometer convention expected by PX4 v1.16.
"""
from pathlib import Path
import sys

root = Path(sys.argv[1])
for name in ("NavSat", "Magnetometer"):
    path = root / f"{name}.cc"
    source = path.read_text()
    anchor = '#include "gz/sim/Util.hh"'
    assert source.count(anchor) == 1
    includes = '\n#include "gz/sim/components/SphericalCoordinates.hh"'
    if '#include "gz/sim/components/World.hh"' not in source:
        includes += '\n#include "gz/sim/components/World.hh"'
    source = source.replace(anchor, anchor + includes)
    if name == "NavSat":
        old = 'it->second->SetVelocity(_worldLinearVel->Data());'
        new = '''// World axes are LOCAL2, not geographic ENU when heading != 0.
        const auto world = _ecm.EntityByComponents(components::World());
        const auto spherical =
            _ecm.Component<components::SphericalCoordinates>(world);
        const auto velocity = spherical ? spherical->Data().VelocityTransform(
            _worldLinearVel->Data(), math::SphericalCoordinates::LOCAL2,
            math::SphericalCoordinates::GLOBAL) : _worldLinearVel->Data();
        it->second->SetVelocity(velocity);'''
    else:
        old = 'math::Vector3d magnetic_field_I(X, Y, Z);'
        new = '''math::Vector3d magnetic_field_I(X, Y, Z);
          const auto world = _ecm.EntityByComponents(components::World());
          const auto spherical =
              _ecm.Component<components::SphericalCoordinates>(world);
          if (spherical)
          {
            // The legacy NED option is applied as a world-frame vector by
            // this sensor stack; preserve its permutation / units at heading=0.
            // Rotate that world vector, not a second NED/ENU permutation.
            magnetic_field_I = spherical->Data().VelocityTransform(
                magnetic_field_I, math::SphericalCoordinates::GLOBAL,
                math::SphericalCoordinates::LOCAL2);
          }'''
    assert source.count(old) == 1, f"Unexpected upstream {name} source"
    path.write_text(source.replace(old, new))
