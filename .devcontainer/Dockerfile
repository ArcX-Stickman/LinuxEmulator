FROM mcr.microsoft.com/devcontainers/base:ubuntu-24.04
ENV DEBIAN_FRONTEND=noninteractive
# Wine (32+64 bit) for .exe, a virtual screen + browser streaming, Java for the Android SDK tools
RUN dpkg --add-architecture i386 && apt-get update && apt-get install -y --no-install-recommends \
      wine wine64 wine32:i386 \
      xvfb x11vnc novnc websockify fluxbox xdotool \
      fonts-dejavu fonts-liberation \
      python3 python3-flask python3-xlib python3-pip \
      libgl1-mesa-dri libglx-mesa0 libopengl0 libegl1 mesa-utils mesa-vulkan-drivers libvulkan1 \
      openjdk-17-jre-headless unzip curl ca-certificates \
      libpulse0 libgl1 libnss3 libxcomposite1 libxcursor1 libxi6 libxtst6 libxdamage1 libxkbfile1 \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --break-system-packages flask-sock
ENV ANDROID_HOME=/home/vscode/android-sdk
