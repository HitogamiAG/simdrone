# syntax=docker/dockerfile:1.7
FROM uav-gazebo-service:local
WORKDIR /opt/flight-tests
RUN --mount=type=cache,target=/root/.cache/pip \
    /opt/venv/bin/pip install --target /opt/flight-tests/vendor pyulog==1.2.3
ENV PYTHONPATH=/opt/flight-tests/vendor:/opt/uav
COPY backend/tests/flight/ /opt/flight-tests/
RUN g++ -O2 -std=c++17 /opt/flight-tests/native_geodetic.cpp -o /tmp/native-geodetic \
    $(pkg-config --cflags --libs gz-math7)
CMD ["python", "-m", "pytest", "-q", "test_flight.py"]
