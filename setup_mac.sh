#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
#  setup_mac.sh — Home AI Monitor one-command setup for macOS
#  Tested on macOS Ventura / Sonoma, Intel MacBook Pro
#  Usage: bash setup_mac.sh
# ─────────────────────────────────────────────────────────────────────────────
set -e

YELLOW='\033[1;33m'
GREEN='\033[0;32m'
CYAN='\033[0;36m'
RED='\033[0;31m'
NC='\033[0m'

step() { echo -e "\n${CYAN}▶ $1${NC}"; }
ok()   { echo -e "${GREEN}✓ $1${NC}"; }
warn() { echo -e "${YELLOW}⚠ $1${NC}"; }
err()  { echo -e "${RED}✗ $1${NC}"; exit 1; }

echo -e "${GREEN}"
echo "  ╔══════════════════════════════════════════╗"
echo "  ║     Home AI Monitor — macOS Setup        ║"
echo "  ║     MacBook Pro · macOS Ventura/Sonoma   ║"
echo "  ╚══════════════════════════════════════════╝"
echo -e "${NC}"

# ── Pre-flight checks ─────────────────────────────────────────────────────────
step "Pre-flight checks"

# macOS version
SW_VERS=$(sw_vers -productVersion)
ok "macOS $SW_VERS"

# Disk space — warn if under 20GB free
FREE_GB=$(df -g / | awk 'NR==2 {print $4}')
if [ "$FREE_GB" -lt 20 ]; then
  warn "Only ${FREE_GB}GB free on /. Recommend at least 20GB for recordings."
else
  ok "${FREE_GB}GB free disk space"
fi

# ── Xcode Command Line Tools ──────────────────────────────────────────────────
step "Checking Xcode Command Line Tools"
if ! xcode-select -p &>/dev/null; then
  warn "Installing Xcode Command Line Tools — a dialog will appear, click Install"
  xcode-select --install
  echo "  Re-run this script after the install completes."
  exit 0
fi
ok "Xcode CLT: $(xcode-select -p)"

# ── Homebrew ──────────────────────────────────────────────────────────────────
step "Checking Homebrew"
if ! command -v brew &>/dev/null; then
  warn "Installing Homebrew..."
  /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
  # Add brew to PATH for Apple Silicon if needed
  if [ -f "/opt/homebrew/bin/brew" ]; then
    eval "$(/opt/homebrew/bin/brew shellenv)"
  fi
fi
ok "Homebrew $(brew --version | head -1)"

# ── System packages ───────────────────────────────────────────────────────────
step "Installing system packages via Homebrew"
brew update -q
brew install \
  python@3.11 \
  cmake \
  openblas \
  lapack \
  ffmpeg \
  mosquitto \
  node \
  git \
  2>/dev/null || true
ok "System packages installed"

# ── Tailscale (remote access) ─────────────────────────────────────────────────
step "Checking Tailscale (remote access)"
if ! command -v tailscale &>/dev/null; then
  warn "Installing Tailscale for remote dashboard access..."
  brew install --cask tailscale
  open -a Tailscale
  echo ""
  warn "Tailscale installed. Sign in via the menu bar icon, then run:"
  warn "  tailscale ip -4   (to get your Tailscale IP for remote access)"
else
  TAILSCALE_IP=$(tailscale ip -4 2>/dev/null || echo "not connected")
  ok "Tailscale installed — IP: $TAILSCALE_IP"
fi

# ── Mosquitto (local MQTT broker) ─────────────────────────────────────────────
step "Configuring Mosquitto MQTT broker"
MOSQ_CONF="$(brew --prefix)/etc/mosquitto/mosquitto.conf"

if ! grep -q "listener 1883 127.0.0.1" "$MOSQ_CONF" 2>/dev/null; then
  cat >> "$MOSQ_CONF" <<'EOF'

# Home AI Monitor — local listener (loopback only)
listener 1883 127.0.0.1
allow_anonymous true
EOF
fi

brew services restart mosquitto
ok "Mosquitto running on localhost:1883"

# ── Python virtual environment ────────────────────────────────────────────────
step "Setting up Python virtual environment"
PYTHON="$(brew --prefix)/bin/python3.11"
[ ! -f "$PYTHON" ] && PYTHON="$(brew --prefix)/bin/python3"

"$PYTHON" -m venv venv
source venv/bin/activate
pip install --upgrade pip wheel setuptools -q
ok "Virtual environment created at ./venv"

# ── Python dependencies ───────────────────────────────────────────────────────
step "Installing Python dependencies"
pip install -r requirements.txt -q
ok "Core dependencies installed"

# ── dlib + face_recognition ───────────────────────────────────────────────────
step "Building dlib + face_recognition (uses all CPU cores — takes 3-5 min)"
export CMAKE_BUILD_PARALLEL_LEVEL=$(sysctl -n hw.logicalcpu)
export OPENBLAS="$(brew --prefix openblas)"
pip install dlib face_recognition -q
ok "Face recognition installed"

# ── Create runtime directories ────────────────────────────────────────────────
step "Creating runtime directories"
mkdir -p recordings/snapshots
mkdir -p known_faces
ok "recordings/snapshots/ and known_faces/ created"

# ── Zigbee2MQTT ───────────────────────────────────────────────────────────────
step "Installing Zigbee2MQTT"
if [ ! -d "$HOME/zigbee2mqtt" ]; then
  git clone --depth 1 https://github.com/Koenkk/zigbee2mqtt.git "$HOME/zigbee2mqtt"
  cd "$HOME/zigbee2mqtt"
  npm ci --legacy-peer-deps -q
  cd - > /dev/null

  mkdir -p "$HOME/zigbee2mqtt/data"
  cat > "$HOME/zigbee2mqtt/data/configuration.yaml" <<'EOF'
homeassistant: false
permit_join: true
mqtt:
  base_topic: zigbee2mqtt
  server: mqtt://localhost
serial:
  port: /dev/cu.usbserial-0001   # ← update after plugging in ZBDongle-E
frontend:
  port: 8080
EOF
  ok "Zigbee2MQTT installed at ~/zigbee2mqtt"
else
  ok "Zigbee2MQTT already installed — skipping"
fi

# ── macOS Launch Agent (auto-start on login) ──────────────────────────────────
step "Creating macOS Launch Agent"
WORKDIR="$(pwd)"
PYTHON_BIN="$(pwd)/venv/bin/python"
PLIST="$HOME/Library/LaunchAgents/com.homeaimonitor.plist"

cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>com.homeaimonitor</string>
  <key>ProgramArguments</key>
  <array>
    <string>${PYTHON_BIN}</string>
    <string>${WORKDIR}/main.py</string>
  </array>
  <key>WorkingDirectory</key>
  <string>${WORKDIR}</string>
  <key>RunAtLoad</key>
  <false/>
  <key>KeepAlive</key>
  <true/>
  <key>StandardOutPath</key>
  <string>${WORKDIR}/monitor.log</string>
  <key>StandardErrorPath</key>
  <string>${WORKDIR}/monitor.log</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key>
    <string>/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin</string>
  </dict>
</dict>
</plist>
EOF
ok "Launch Agent created at $PLIST"

# ── Silicon Labs CP2102 driver reminder ───────────────────────────────────────
step "Checking for Zigbee dongle driver"
if ls /dev/cu.usbserial* &>/dev/null || ls /dev/cu.SLAB_USBtoUART* &>/dev/null; then
  DONGLE=$(ls /dev/cu.usbserial* /dev/cu.SLAB_USBtoUART* 2>/dev/null | head -1)
  ok "ZBDongle-E detected: $DONGLE"
  warn "Update serial.port in ~/zigbee2mqtt/data/configuration.yaml to: $DONGLE"
else
  warn "ZBDongle-E not detected yet."
  warn "If it doesn't appear after plugging in, install the driver:"
  warn "https://www.silabs.com/developers/usb-to-uart-bridge-vcp-drivers"
fi

# ── Config check ──────────────────────────────────────────────────────────────
step "Checking config.yaml"
if [ -f "config.yaml" ]; then
  if grep -q "YOUR_ANTHROPIC_API_KEY\|YOUR_NVR_PASSWORD\|CHANGEME" config.yaml 2>/dev/null; then
    warn "config.yaml has unfilled placeholders — see Next Steps below"
  else
    ok "config.yaml looks filled in"
  fi
else
  warn "config.yaml not found — copying from config.example.yaml"
  cp config.example.yaml config.yaml
  warn "Edit config.yaml before running the monitor"
fi

# ── macOS firewall note ───────────────────────────────────────────────────────
step "macOS firewall"
warn "If macOS prompts 'Allow incoming connections' for Python — click Allow."
warn "This is required for the dashboard to be reachable on your network."

# ── Done ──────────────────────────────────────────────────────────────────────
echo ""
echo -e "${GREEN}══════════════════════════════════════════════════════${NC}"
echo -e "${GREEN}  Setup complete! Here's what to do next:             ${NC}"
echo -e "${GREEN}══════════════════════════════════════════════════════${NC}"
echo ""
echo "  ── Today (sensors, no cameras yet) ───────────────────"
echo ""
echo "  1. Edit config.yaml:"
echo "     nano config.yaml"
echo "     Set: ai.api_key, alarm.pin, notifications.ntfy.topic"
echo "     Leave nvr.ip/password for tomorrow when cameras are installed"
echo "     Update zigbee_sensors with your sensor IDs after pairing"
echo ""
echo "  2. Connect ZBDongle-E via USB-A → USB-C adapter"
echo "     ls /dev/cu.*   (find the dongle path)"
echo "     Update ~/zigbee2mqtt/data/configuration.yaml with that path"
echo ""
echo "  3. Start Zigbee2MQTT and pair sensors:"
echo "     cd ~/zigbee2mqtt && npm start"
echo "     Open http://localhost:8080 → Permit join → pair each sensor"
echo "     Copy friendly names back into config.yaml"
echo ""
echo "  4. Add known face photos (optional):"
echo "     cp yourphoto.jpg known_faces/YourName.jpg"
echo ""
echo "  5. Disable cameras in config.yaml until tomorrow:"
echo "     Set enabled: false for all cameras, or set nvr.ip to a test value"
echo ""
echo "  6. Start the monitor (sensors + alarm only, no cameras):"
echo "     source venv/bin/activate && python main.py"
echo "     Dashboard → http://localhost:5000"
echo ""
echo "  ── Tomorrow (cameras) ─────────────────────────────────"
echo ""
echo "  7. Find NVR IP in your router, then test RTSP:"
echo "     ffplay 'rtsp://admin:PASSWORD@NVR_IP:554/ch01/main/av_stream'"
echo "     Update nvr.ip, nvr.password, nvr.rtsp_path in config.yaml"
echo "     Set cameras back to enabled: true"
echo ""
echo "  ── Auto-start on login ─────────────────────────────────"
echo ""
echo "  Enable:  launchctl load ~/Library/LaunchAgents/com.homeaimonitor.plist"
echo "  Disable: launchctl unload ~/Library/LaunchAgents/com.homeaimonitor.plist"
echo "  Logs:    tail -f monitor.log"
echo ""
echo "  ── Tailscale (remote access from phone) ────────────────"
echo ""
echo "  tailscale ip -4    → get your Tailscale IP"
echo "  Open http://TAILSCALE_IP:5000 in Safari on your iPhone"
echo ""
