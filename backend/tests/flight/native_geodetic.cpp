#include <gz/math/Angle.hh>
#include <gz/math/SphericalCoordinates.hh>
#include <gz/math/Vector3.hh>
#include <cstdlib>
#include <iomanip>
#include <iostream>

int main(int argc, char **argv)
{
  if (argc != 8) return 2;
  using C = gz::math::SphericalCoordinates;
  C context(C::EARTH_WGS84, gz::math::Angle(GZ_DTOR(std::atof(argv[1]))),
            gz::math::Angle(GZ_DTOR(std::atof(argv[2]))), std::atof(argv[3]),
            gz::math::Angle(GZ_DTOR(std::atof(argv[4]))));
  auto point = context.PositionTransform(
      gz::math::Vector3d(std::atof(argv[5]), std::atof(argv[6]), std::atof(argv[7])),
      C::LOCAL2, C::SPHERICAL);
  std::cout << std::setprecision(14) << GZ_RTOD(point.X()) << ' '
            << GZ_RTOD(point.Y()) << ' ' << point.Z() << '\n';
  return 0;
}
