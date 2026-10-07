FROM ubuntu:24.04
ARG DEBIAN_FRONTEND=noninteractive
ARG HAPP_VERSION=4.3.0
ARG HAPP_SHA256=41c86c0caf18190bce58a7f603fd466c9e3fbfef2b85f837635a0453401affba
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates curl python3 tini xvfb xauth xdotool xclip \
    dbus-x11 at-spi2-core python3-pyatspi \
    libopengl0 libegl1 libgl1 libxkbcommon-x11-0 libxcb-cursor0 \
    libxcb-icccm4 libxcb-keysyms1 libxcb-shape0 libxcb-xinerama0 \
    libxcb-randr0 libxcb-render-util0 libxcb-image0 libglib2.0-0 \
    libfontconfig1 fonts-dejavu-core libdbus-1-3 libssl3t64 \
    && rm -rf /var/lib/apt/lists/*
RUN curl -fL --retry 3 -o /tmp/happ.deb \
    "https://github.com/Happ-proxy/happ-desktop/releases/download/${HAPP_VERSION}/Happ.linux.x64.deb" \
    && echo "${HAPP_SHA256}  /tmp/happ.deb" | sha256sum -c - \
    && dpkg-deb -x /tmp/happ.deb /tmp/happ-package \
    && mv /tmp/happ-package/opt/happ /opt/happ \
    && rm -rf /tmp/happ-package /tmp/happ.deb
ENV LANG=C.UTF-8 DISPLAY=:97 XAUTHORITY=/run/happ/Xauthority \
    XDG_CONFIG_HOME=/state/config XDG_DATA_HOME=/state/data \
    XDG_CACHE_HOME=/state/cache XDG_RUNTIME_DIR=/run/happ \
    QT_QUICK_BACKEND=software QSG_RHI_BACKEND=software HAPP_GRAPHICS_BACKEND=software \
    QT_LINUX_ACCESSIBILITY_ALWAYS_ON=1
COPY runtime/ /app/
HEALTHCHECK --interval=30s --timeout=5s --start-period=150s --retries=3 CMD python3 /app/healthcheck.py
ENTRYPOINT ["/usr/bin/tini","--"]
CMD ["bash","/app/entrypoint.sh"]
