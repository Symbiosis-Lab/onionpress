<div align="center">
  <img src="logo.png" alt="OnionPress logo" width="400">

  **Your Decentralized Social Blog Site**

  **[Website](https://onionpress.org)** · **[Download](https://github.com/brewsterkahle/onionpress/releases/latest)**
</div>

# OnionPress

**Run your own website from your computer. Just Works. Free, forever.**

OnionPress turns your Mac or Linux computer into a web server running WordPress, accessible via the Tor network. Your site gets its own permanent .onion address that no one can take away from you. It's backed up automatically by the Internet Archive's Wayback Machine.

**WordPress + Tor + Wayback Machine.** You write. Tor delivers. Wayback remembers.

## Features

- 📝 **Full WordPress**: Any theme, any plugin, a dashboard you already know
- 🧅 **Permanent .onion Address**: Your own address on the Tor network — no domain registrar, no DNS, uncensorable
- 🏛️ **Wayback Machine Integration**: Automatically archived and served when you're offline
- 🏠 **Works Behind Firewalls**: Home, school, work — Tor punches through NAT and firewalls
- 🔐 **Private by Default**: No analytics, no tracking, no ads. End-to-end encrypted without certificates
- 🐳 **Sandboxed**: Docker containers inside a VM for isolation and security
- 📱 **Mac Menu Bar App**: Purple onion icon with one-click controls
- 🐧 **Linux Support**: CLI + systemd for servers and Raspberry Pi

## Requirements

- **Mac**: macOS 13.0 (Ventura) or later
- **Linux**: Docker and Docker Compose
- Internet connection

## Installation

### Mac

1. Download [`onionpress.dmg`](https://github.com/brewsterkahle/onionpress/releases/latest/download/onionpress.dmg)
2. Drag `OnionPress.app` to Applications and launch
3. First launch takes 3-5 minutes (one-time setup)

### Linux

One command installs everything:

```bash
curl -sSL https://raw.githubusercontent.com/brewsterkahle/onionpress/main/linux/install.sh | bash
```

Or download the [`.deb` package](https://github.com/brewsterkahle/onionpress/releases/latest):

```bash
sudo apt-get install ./onionpress_*.deb
sudo systemctl start onionpress
```

After install: `onionpress status`, `onionpress address`, `onionpress logs`.

### macOS Security Warning

On first launch, macOS will show a security warning (normal for open-source software). Go to **System Settings → Privacy & Security**, scroll down, and click **"Open Anyway"**.

## Usage

### Menu Bar Controls

On Mac, OnionPress appears in your menu bar:
- 🟣 **Purple** = running and available
- 🟡 **Yellow** = starting or reconnecting
- ⚪ **Gray** = stopped

## Viewing Your Site

Your site is viewable on any Tor-enabled browser:

- **[Tor Browser](https://www.torproject.org/download/)** — Windows, Mac, Linux (the gold standard)
- **[Brave Browser](https://brave.com/)** — Windows, Mac, Linux, Android (built-in Tor window)
- **[Onion Browser](https://apps.apple.com/app/onion-browser/id519296448)** — iPhone & iPad
- **[Tor Browser for Android](https://play.google.com/store/apps/details?id=org.torproject.torbrowser)** — Android

## Architecture

OnionPress is built on [WordPress](https://wordpress.org/), [Tor](https://www.torproject.org/), and the Internet Archive's [Wayback Machine](https://web.archive.org/).

All data is stored in:
- `~/.onionpress/` — Application state, logs, config
- `~/Documents/onionpress/` — Backups and My Creations
- Docker volumes for WordPress content, database, and Tor keys

## Building from Source

```bash
# Mac DMG
bash build/build-dmg-simple.sh

# Linux .deb and AppImage
bash build/build-linux.sh
```

## Uninstalling

Click **Uninstall** from the menu bar app. It will remove all data and quit.

## License

AGPL 3 License - See LICENSE file for details

## Credits

A [Decentralized Web](http://brewster.kahle.org/2015/08/11/locking-the-web-open-a-call-for-a-distributed-web-2/) project.

Free and Open Source (AGPL 3). [Donate to the Tor Project](https://donate.torproject.org/) · [Donate to the Internet Archive](https://archive.org/donate/)

## Support

For issues, questions, or contributions, visit the [GitHub repository](https://github.com/brewsterkahle/onionpress) or the [product page](https://onionpress.org).
