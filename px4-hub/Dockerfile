# Shared OSRF repository; build tools never enter the runtime image.
FROM ubuntu:24.04@sha256:a853f94d226358a79c740cfc7bce0c289748f3fe3488d921d038ccd752c61b60 AS base
SHELL ["/bin/bash", "-o", "pipefail", "-c"]
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates curl && \
    curl -fsSL https://packages.osrfoundation.org/gazebo.gpg -o /usr/share/keyrings/pkgs-osrf-archive-keyring.gpg && \
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/pkgs-osrf-archive-keyring.gpg] https://packages.osrfoundation.org/gazebo/ubuntu-stable noble main" > /etc/apt/sources.list.d/gazebo-stable.list && \
    rm -rf /var/lib/apt/lists/*

FROM base AS px4-build
RUN apt-get update && apt-get install -y --no-install-recommends \
    git build-essential cmake ninja-build ccache pkg-config genromfs \
    python3 python3-dev python3-pip python3-setuptools python3-wheel \
    python3-jinja2 python3-numpy python3-yaml python3-jsonschema \
    python3-toml python3-packaging libxml2-utils zip unzip \
    libgz-sim8-dev=8.15.0-1~noble libgz-transport13-dev=13.6.0-1~noble \
    libopencv-dev cppzmq-dev libgstreamer-plugins-base1.0-dev && \
    rm -rf /var/lib/apt/lists/*
RUN pip3 install --break-system-packages --no-cache-dir \
    empy==3.3.4 kconfiglib==14.1.0 pyros-genmsg==0.5.8 \
    future==1.0.0 lxml==5.4.0 six==1.17.0

# v1.16.0: submodule revisions come from this real Git commit.
# Fetch only dependencies needed by SITL, excluding NuttX and other simulators.
ARG PX4_COMMIT=6ea3539157ca358c70a515878b77077af7d4611d
WORKDIR /src/PX4-Autopilot
RUN git init -q . && git remote add origin https://github.com/PX4/PX4-Autopilot.git && \
    git fetch --progress --depth 1 origin tag v1.16.0 && git checkout --detach "${PX4_COMMIT}" && \
    test "$(git rev-parse HEAD)" = "${PX4_COMMIT}" && \
    git submodule update --init --recursive --depth 1 \
      src/modules/mavlink/mavlink \
      src/modules/uxrce_dds_client/Micro-XRCE-DDS-Client \
      src/drivers/gps/devices src/lib/heatshrink/heatshrink \
      src/lib/events/libevents Tools/simulation/gz
ARG BUILD_JOBS=2
RUN make PX4_MAKE_ARGS="-j${BUILD_JOBS}" px4_sitl_default && \
    mkdir -p /opt/PX4-Autopilot/build/px4_sitl_default && \
    cp -a build/px4_sitl_default/bin build/px4_sitl_default/etc \
      /opt/PX4-Autopilot/build/px4_sitl_default/ && \
    mkdir -p /opt/PX4-Autopilot/build/px4_sitl_default/rootfs && \
    cp -a build/px4_sitl_default/etc \
      /opt/PX4-Autopilot/build/px4_sitl_default/rootfs/

FROM base AS runtime
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 \
    GZ_VERSION=harmonic PX4_HOME_LAT=47.397742 PX4_HOME_LON=8.545594 PX4_HOME_ALT=488
RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 python3-venv gz-sim8-cli=8.15.0-1~noble \
    libgz-transport13=13.6.0-1~noble libgz-sim8=8.15.0-1~noble && \
    rm -rf /var/lib/apt/lists/*
WORKDIR /opt/uav
COPY px4-hub/requirements.txt ./requirements.txt
RUN python3 -m venv /opt/venv && /opt/venv/bin/pip install --no-cache-dir -r requirements.txt && \
    groupadd --system px4 && useradd --system --gid px4 --home-dir /opt/uav px4 && \
    mkdir -p /opt/uav/instances && chown px4:px4 /opt/uav /opt/uav/instances
COPY --from=px4-build /opt/PX4-Autopilot /opt/PX4-Autopilot
COPY --chown=px4:px4 px4-hub/app ./app
ENV PATH=/opt/venv/bin:$PATH PYTHONPATH=/opt/uav
USER px4
EXPOSE 8002
HEALTHCHECK --interval=10s --timeout=3s --start-period=10s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8002/healthz || exit 1
CMD ["uvicorn", "app.api:app", "--host", "0.0.0.0", "--port", "8002", "--workers", "1"]
