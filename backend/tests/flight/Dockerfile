FROM uav-gazebo-service:local
WORKDIR /opt/flight-tests
COPY backend/tests/flight/ /opt/flight-tests/
RUN g++ -O2 -std=c++17 /opt/flight-tests/native_geodetic.cpp -o /tmp/native-geodetic \
    $(pkg-config --cflags --libs gz-math7)
CMD ["python", "-m", "pytest", "-q", "test_flight.py"]
