#!/usr/bin/env bash
# Installs extra apps into the Codespace.
#   usage: bash scripts/install-apps.sh chrome|minecraft|steam|all
# GeForce NOW has no Linux app: it runs in Chrome, so installing "chrome" covers it.
set -e
APP=${1:-all}
export DEBIAN_FRONTEND=noninteractive
cd /tmp

base_graphics() {
  # Software 3D (CPU) so apps can draw without a graphics card
  sudo apt-get install -y libgl1-mesa-dri libglx-mesa0 libopengl0 libgl1 libegl1 libgles2 \
    mesa-utils mesa-vulkan-drivers libvulkan1 fonts-liberation xdg-utils
}

install_chrome() {
  echo ">>> Installing Google Chrome"
  curl -fL -o chrome.deb https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb
  sudo apt-get install -y ./chrome.deb
  rm -f chrome.deb
}

install_minecraft() {
  echo ">>> Installing Minecraft Launcher"
  curl -fL -o Minecraft.deb https://launcher.mojang.com/download/Minecraft.deb
  sudo apt-get install -y ./Minecraft.deb
  rm -f Minecraft.deb
}

install_steam() {
  echo ">>> Installing Steam"
  sudo dpkg --add-architecture i386
  sudo apt-get update
  # Accept Steam's license non-interactively (otherwise apt waits forever for a keypress)
  for owner in steam steam-installer; do
    echo "$owner steam/question select I AGREE" | sudo debconf-set-selections
    echo "$owner steam/license note ''" | sudo debconf-set-selections
  done
  # Steam is 32-bit, so it needs the 32-bit graphics libraries too
  sudo apt-get install -y libgl1-mesa-dri:i386 libgl1:i386 libglx-mesa0:i386 mesa-vulkan-drivers:i386 || true
  if apt-cache show steam-installer >/dev/null 2>&1; then
    sudo apt-get install -y steam-installer
  else
    curl -fL -o steam.deb https://repo.steampowered.com/steam/archive/stable/steam_latest.deb
    sudo apt-get install -y ./steam.deb
    rm -f steam.deb
  fi
}

sudo apt-get update
base_graphics
case "$APP" in
  chrome|geforcenow) install_chrome ;;
  minecraft) install_minecraft ;;
  steam) install_steam ;;
  all) install_chrome; install_minecraft; install_steam ;;
  *) echo "unknown app: $APP"; exit 1 ;;
esac
echo ">>> DONE: $APP installed"
