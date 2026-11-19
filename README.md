# Game Runner: run .exe and .apk games in the browser

Two separate websites in one GitHub Codespace:

| Site | What it does | Website port | Screen port |
|---|---|---|---|
| **EXE Runner** | Runs Windows `.exe` games with **Wine** | `8080` | `6080` |
| **APK Runner** | Installs `.apk` games on an **Android emulator** | `8081` | `6081` |

## How it works

```
Browser ──upload──▶ Flask website ──launches──▶ Wine / Android emulator
                                                       │ draws onto
                                                       ▼
Browser ◀──noVNC (port 6080/6081)── x11vnc ◀── Xvfb (virtual monitor)
```

Nothing actually runs *in* your browser. The game runs on the Codespace's Linux machine,
draws onto a fake monitor (Xvfb), and that monitor is streamed to the page as video.
Your mouse and keyboard clicks go back the same way.

## Setup on GitHub Codespaces

1. Make a new GitHub repo and upload everything in this folder, **including the hidden `.devcontainer` folder**.
2. On the repo page: **Code → Codespaces → Create codespace on main**.
   The first build takes about 5 minutes because it installs Wine.
3. In the Codespace terminal:
   ```bash
   bash start-exe.sh      # EXE site
   # or, in a second terminal:
   bash start-apk.sh      # APK site
   ```
4. Open the **PORTS** tab and click the 🌐 globe next to **8080** (EXE) or **8081** (APK).

**If the game screen stays blank inside the page**, click **"Open screen in new tab ↗"**.
Codespaces gives each port its own private web address, and some browsers block one private
port from showing up inside another. The other fix is to right-click port 6080 → *Port Visibility → Public*.
Be careful with that one: anyone who has the link can then see and control the screen.

`bash stop-all.sh` kills everything. (Using `bash` means it still works if GitHub drops the scripts' run permission when you upload them.)

---

## ⚡ Reducing lag

- **📶 ping meter** (top of the screen): the round trip to your Codespace. Under 60 ms means distance
  isn't the problem; the lag is graphics or streaming. GitHub already picks the nearest region
  automatically (GitHub → Settings → Codespaces → Region).
- **Quality menu:** ⚡ Fast sends blurrier frames that arrive sooner. Use it on slow Wi-Fi.
- **Smaller screen** = less to draw and stream: `SCREEN=960x540 bash start-exe.sh` (default 1280x720).
- **4-core machine:** github.com/codespaces → ⋯ → Change machine type. The CPU draws all the graphics.
- **Lower in-game settings** (render distance, shadows) help more than anything else for 3D games.

## 🧩 Apps: Chrome, GeForce NOW, Minecraft, Steam

The **Apps** panel on the site installs and opens these on the game screen with one click:
press **Install** (1–5 minutes, runs in the background), then **▶**.

| App | What to expect |
|---|---|
| **Minecraft** | Works. Use 🎮 Game mode to look around, and older versions run faster. |
| **Chrome** | A normal browser on the game screen. |
| **GeForce NOW** | No Linux app exists, so it opens play.geforcenow.com in Chrome. The video is decoded on the CPU and then streamed to you again, so it's laggy. Opening play.geforcenow.com directly on your own computer is much smoother. |
| **Steam** | Installs fine, but may not start or run games: Steam uses a sandbox Codespaces may block, and Windows games through Proton are slow without a graphics card. |

You can also install them from the terminal: `bash scripts/install-apps.sh chrome|minecraft|steam|all`.

## ☁ Shared game library

Upload a game once and it's available in every Codespace made from this repo, so nobody has to
upload it again.

- Leave **☁ Share to library** ticked when uploading. The site uploads a copy to this repo's
  **GitHub Releases** (a release tagged `game-library`) in the background.
- In any other Codespace, the **☁ Library** panel lists every shared game. Press **⬇** to download
  one, then ▶ to play.
- **✕** removes a game from the library for everyone, but keeps the copy on your Codespace.

**Why Releases and not normal commits:** GitHub blocks files over 100 MB in git, and every game
committed would stay in the repo history forever and slow down every Codespace. Release files can
be up to 2 GB each, don't count toward repo size, and are only downloaded when someone presses ⬇.

**⚠ This repo is public, so anyone on the internet can download what's in the library.** Only share
free games or games you made. Sharing paid games is redistribution, and GitHub takes repos (and
accounts) down for that.

It works with no setup inside Codespaces, which provide `GITHUB_TOKEN` automatically. Other people's
uploads only reach this library if they use a Codespace of **this** repo (add them as
collaborators). A fork has its own separate library.

## 🎮 Game mode (for Minecraft and other 3D games)

Normally the screen sends your mouse's *position*, so in a 3D game you can only turn until your
cursor hits the edge of the browser. **Game mode** locks and hides the mouse (like browser games do),
goes fullscreen, and sends *movement* instead, so you can turn forever.

- Click **🎮 Game mode** above the screen.
- **Hold Esc** to leave. A quick tap of Esc still goes to the game (pause menu).
- If the mouse gets released (for example after switching tabs), click the screen to grab it again.
- Use the 🖱 slider to change mouse sensitivity.
- **In Minecraft:** Options → Controls → Mouse Settings → **Raw Input: OFF** if turning feels stuck.

Game mode needs two extra Python libraries, which the devcontainer installs. On an older Codespace, run
`pip install flask-sock python-xlib` and restart the site.

## EXE Runner: what will and won't work

**Tested:** I compiled a small Windows game, uploaded it as a `.zip`, launched it, and watched
it run in a Chrome tab through noVNC. Upload, launch, stop, delete, and the zip-folder handling all work.

- **Upload the whole game folder as a `.zip`.** Most games need DLLs and data files that sit next
  to the `.exe`. A lone `.exe` only works for single-file games.
- **Codespaces has no graphics card.** Wine renders 3D on the CPU, so:
  - ✅ 2D games, pixel games, older games (roughly before 2010), GameMaker games, many indie games
  - ⚠️ Simple 3D or Unity games: they might run, slowly
  - ❌ Modern DirectX 11/12 games: unplayable or won't start
- ❌ **Games with anti-cheat** (Valorant, Fortnite and similar) won't run. Those block Wine on purpose.
- ❌ **No sound.** Audio isn't streamed.
- ⚠️ **First-person games** that lock the mouse behave badly over VNC.
- 🔍 Look up your game on the [WineHQ AppDB](https://appdb.winehq.org) to see if it works with Wine.
- Games made with .NET or XNA may ask to install *Wine Mono* the first time. Click Install in the game screen.

## Why APKs might not work on Codespaces

The Android emulator needs **hardware virtualization** (`/dev/kvm`) to run. It's basically
a computer inside a computer, and without KVM the current emulator won't even start for x86 phone images.
GitHub Codespaces machines usually **don't** expose `/dev/kvm`.

Check in your Codespace terminal:
```bash
ls /dev/kvm
```
- **"No such file or directory"** → the APK site will open and explain why it can't boot a phone.
  That's the expected result on Codespaces.
- **It exists** → great, run `bash scripts/setup-android.sh` (downloads about 3 GB), then `bash start-apk.sh` and press **Boot phone**.

Things that *don't* get around it:
- **Waydroid / Redroid** (Android in a container) need special Linux kernel modules (`binder`) that you can't load inside a Codespace.
- **Software emulation (no KVM)**: Google removed this for x86 images, and ARM images are too slow to play anything.

**Where the APK Runner *will* work:** the exact same project on
- your own Linux PC,
- Windows 11 with WSL2 (open the folder in VS Code → *Reopen in Container*), as long as virtualization is turned on in your BIOS, or
- a cloud VM that supports nested virtualization.

Even with KVM, keep in mind:
- The phone image is Android 11 x86_64 with Google's ARM translation, so most ARM-only games still install. Some don't (`NO_MATCHING_ABIS`).
- No GPU means 3D phone games will be slow.
- Games that need Google Play sign-in or Play Integrity checks will refuse to run.
- Only plain `.apk` files are supported. `.xapk` / `.apks` bundles are split APKs, and you'd need
  `adb install-multiple` for those.

## Codespaces cost

The devcontainer asks for a 4-core machine because games need the CPU. Free GitHub accounts get a
limited number of core-hours each month, and a 4-core machine uses them twice as fast as a 2-core one.
**Stop your Codespace when you're done** (it also auto-stops after 30 minutes idle). Check your
remaining hours under GitHub → Settings → Billing.

## Project layout

```
.devcontainer/      Codespaces machine setup (Wine, Xvfb, noVNC, Java)
exe-runner/         EXE website (Flask app + page)
apk-runner/         APK website (Flask app + page)
scripts/
  start-display.sh  virtual monitor + browser streaming
  setup-android.sh  downloads Android SDK + creates the "gamephone" emulator
  start-emulator.sh boots the emulator
start-exe.sh  start-apk.sh  stop-all.sh
```

Only run games you own or that are free to download. Running random `.exe` files from the
internet is a malware risk, though a Codespace is a decent sandbox for it.

## Quick test

`samples/BounceGame.zip` is a tiny Windows test program (source in `samples/bounce.c`).
Upload it to the EXE Runner and press ▶. You should see an orange ball bouncing around.
