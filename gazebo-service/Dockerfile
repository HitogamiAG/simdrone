FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 \
    GZ_VERSION=harmonic GZ_PARTITION=uav-sim GZ_SIM_RESOURCE_PATH=/opt/uav/models:/opt/uav/third-party/models:/opt/uav/app/models \
    LIBGL_ALWAYS_SOFTWARE=1 MESA_GL_VERSION_OVERRIDE=4.5

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates curl gnupg lsb-release git python3 python3-venv python3-pip \
    g++ pkg-config python3-dev pybind11-dev \
    ffmpeg libgl1 libegl1 libgles2 libglvnd0 libx11-6 libxext6 libxrender1 \
    libxi6 libxrandr2 libxinerama1 libxcursor1 libxkbcommon0 libsm6 libice6 \
    mesa-utils libosmesa6 && \
    curl -fsSL https://packages.osrfoundation.org/gazebo.gpg -o /usr/share/keyrings/pkgs-osrf-archive-keyring.gpg && \
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/pkgs-osrf-archive-keyring.gpg] https://packages.osrfoundation.org/gazebo/ubuntu-stable noble main" > /etc/apt/sources.list.d/gazebo-stable.list && \
    apt-get update && apt-get install -y --no-install-recommends \
    gz-harmonic=1.0.0-1~noble gz-sim8-cli=8.15.0-1~noble \
    python3-gz-transport13=13.6.0-1~noble python3-gz-msgs10=10.4.0-1~noble && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /opt/uav
ARG PX4_MODELS_COMMIT=a15af9628536914ff7201c992fce5e3cb5d70db9
RUN git init /tmp/PX4-gazebo-models && cd /tmp/PX4-gazebo-models && \
    git remote add origin https://github.com/PX4/PX4-gazebo-models.git && \
    git fetch --depth 1 origin ${PX4_MODELS_COMMIT} && git checkout FETCH_HEAD && \
    mkdir -p /opt/uav/models && cp -a models/. /opt/uav/models/ && \
    rm -rf /tmp/PX4-gazebo-models

COPY third-party/models /opt/uav/third-party/models
COPY third-party/src /opt/uav/third-party/src
COPY third-party/worlds /opt/uav/third-party/worlds
COPY gazebo-service/requirements.txt /opt/uav/requirements.txt
RUN python3 -m venv --system-site-packages /opt/venv && \
    /opt/venv/bin/pip install --no-cache-dir -r requirements.txt

COPY gazebo-service/app /opt/uav/app
COPY gazebo-service/tests /opt/uav/tests
RUN g++ -O2 -shared -std=c++17 -fPIC $(python3-config --includes) \
      $(pkg-config --cflags gz-transport13) app/native/transport_request.cc \
      -o app/_transport_request$(python3-config --extension-suffix) \
      $(pkg-config --libs gz-transport13) && \
    groupadd --system gazebo && useradd --system --gid gazebo --home-dir /opt/uav gazebo && \
    chown -R gazebo:gazebo /opt/uav

ENV PATH=/opt/venv/bin:$PATH PYTHONPATH=/opt/uav
USER gazebo
EXPOSE 8000
HEALTHCHECK --interval=10s --timeout=3s --start-period=90s --retries=12 \
  CMD curl -fsS http://127.0.0.1:8000/healthz || exit 1
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
