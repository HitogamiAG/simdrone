// Reference conversion from the native Gazebo library; no simulated flight.
#include <gz/math/SphericalCoordinates.hh>
#include <gz/math/Angle.hh>
#include <gz/math/Vector3.hh>
#include <iostream>
#include <iomanip>

int main()
{
    using C = gz::math::SphericalCoordinates;
    C context(C::EARTH_WGS84, gz::math::Angle(0), gz::math::Angle(0), 0,
              gz::math::Angle(GZ_DTOR(90)));
    auto point = context.PositionTransform(gz::math::Vector3d(10, 0, 0), C::LOCAL2, C::SPHERICAL);
    std::cout << std::setprecision(14) << "Gazebo heading=90 x=10: latitude="
              << GZ_RTOD(point.X()) << " longitude=" << GZ_RTOD(point.Y()) << std::endl;
    return point.X() > 0 ? 0 : 1;
}
