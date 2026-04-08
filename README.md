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

1. Download [`onionpress.dmg`](https://github.com/brewsterkahle/onionpress/releases/latest/download/onionpress.dmg) from the [releases page](https://github.com/brewsterkahle/onionpress/releases)
2. Open the DMG and drag `OnionPress.app` to your Applications folder
3. Launch OnionPress from Applications
4. On first launch:
   - The app will generate your onion address (starting with "op2") — takes < 1 second
   - The app will initialize its bundled container runtime (Colima) — takes ~2-3 minutes
   - It will download WordPress, MariaDB, and Tor container images (~1GB)
   - Total one-time setup: 3-5 minutes depending on your internet connection

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

Since this app is not code-signed with an Apple Developer certificate, macOS on first launch. This is normal for open-source software.

**Method 1 - System Settings (Recommended):**
1. Open the app when in your Applications folder - you'll see a security warning.  Hit Done.
2. Open **System Settings** → **Privacy & Security**
3. Scroll down and click **"Open Anyway"** next to the OnionPress warning
4. Click **"Open Anyway"** in the confirmation dialog, and enter your computer's password

**Method 2 - Right-Click:**
1. Right-click (or Control-click) on the OnionPress app in you Application folder
2. Select **"Open"**
3. Click **"Open"** in the dialog

**Method 3 - Terminal (Advanced):**

If you're comfortable with the terminal, you can remove the quarantine flag:
```bash
# After moving to Applications folder
xattr -cr /Applications/OnionPress.app
```
This removes macOS's quarantine attribute and allows the app to launch without warnings.

## Usage

### Menu Bar Controls

Once installed, OnionPress appears in your menu bar with an onion icon:
- 🟣 **Purple** = running and available
- 🟡 **Yellow** = starting or reconnecting
- 🔴 **Red** = stopped or offline

Menu items:

- **Copy Onion Address**: Copy your .onion URL to clipboard
- **Open in Browser**: Open your site in Tor Browser, Brave, or your default browser with the OnionPress extension
- **Start / Stop / Restart**: Control the WordPress service
- **View Logs**: Open the OnionPress log in the built-in log viewer
- **View Web Usage Log**: See WordPress access logs (who's visiting your site)
- **Settings...**: Open configuration file for customization
- **Backup...**: Create a full backup (Tor keys, database, wp-content) as a zip file
- **Restore...**: Restore from a backup zip file
- **Check for Updates...**: Check for new app versions and update WordPress, MariaDB, and Tor container images
- **About OnionPress**: Version info and credits
- **Uninstall...**: Remove OnionPress and all data (prompts for backup first)

### Keeping Your Site Updated

**Manual Updates** (Recommended):
Click "Check for Updates..." in the menu to:
1. Check for new OnionPress app versions
2. Download updated WordPress, MariaDB, and Tor container images
3. Apply security patches and new features

**Automatic Updates** (Optional):
Enable automatic Docker image updates on launch by editing `~/.onionpress/config`:
```bash
UPDATE_ON_LAUNCH=yes
```

When enabled, onionpress will check for and download updated container images each time you launch the app. This ensures you have the latest security patches without manual intervention.

**Note**: Updated container images take effect the next time the service is started.

### Launch on Login

Have your WordPress site start automatically when you log in to macOS by editing `~/.onionpress/config`:
```bash
LAUNCH_ON_LOGIN=yes
```

When enabled:
- OnionPress automatically launches when you log in
- Your WordPress site starts automatically in the background
- The menu bar app appears and shows your status

The app automatically syncs this setting with macOS login items. You can also manage this in **System Settings → General → Login Items**.

**Default**: Disabled (manual launch required)

### Accessing Your Site

1. Your onion address is displayed in the menu bar dropdown (starts with "op2" for easy identification)
2. Install [Tor Browser](https://www.torproject.org/download/) to access .onion sites
3. Copy your onion address and paste it into Tor Browser
4. Complete the WordPress setup wizard

**Address Prefix Customization**: You can customize the prefix in `~/.onionpress/config` before first launch. See the config file for details on generation times for different prefix lengths.

### Backup & Restore

OnionPress can create a full backup of your site including Tor keys (your .onion address), the WordPress database, and all wp-content (themes, plugins, uploads).

**To backup:**
1. Click "Backup..." in the menu bar
2. Enter your WordPress admin credentials (the password encrypts the backup)
3. Choose a save location
4. A zip file is created containing everything needed to restore

**To restore:**
1. Click "Restore..." in the menu bar
2. Select a backup zip file
3. Enter the password used when the backup was created
4. Your site, onion address, and all content will be restored

⚠️ **Security Note**: Backup files contain your Tor private key. Anyone with this file and the password can restore your exact onion address. Store backups securely.

### Internet Archive Wayback Machine Link Fixer

OnionPress automatically installs and activates the [Internet Archive Wayback Machine Link Fixer plugin](https://wordpress.org/plugins/internet-archive-wayback-machine-link-fixer/), which helps combat link rot by:

- Automatically scanning your posts for outbound links
- Creating archived versions in the Wayback Machine
- Redirecting to archived versions when links break
- Archiving your own posts on every update

**The plugin is enabled by default.** To disable automatic installation, edit `~/.onionpress/config` before first launch:
```bash
INSTALL_IA_PLUGIN=no
```

For increased daily link processing, you can add your free Archive.org API credentials in the plugin settings after setup.

### Recommended WordPress Plugins for Tor Onion Services

These plugins are optimized for the Tor network's slower speeds and privacy-focused audience:

#### Performance & Optimization (Essential for Tor)

- **[WP Super Cache](https://wordpress.org/plugins/wp-super-cache/)** or **[W3 Total Cache](https://wordpress.org/plugins/w3-total-cache/)** - Critical for caching to improve response times over Tor's slower connections
- **[Autoptimize](https://wordpress.org/plugins/autoptimize/)** - Minifies and concatenates CSS/JavaScript to reduce HTTP requests and data transfer
- **[EWWW Image Optimizer](https://wordpress.org/plugins/ewww-image-optimizer/)** - Compresses images locally without cloud dependencies
- **[Lazy Load](https://wordpress.org/plugins/rocket-lazy-load/)** - Only loads images when scrolling, reducing initial page load time

#### Privacy & Self-Hosted Alternatives

- **[Simple Local Avatars](https://wordpress.org/plugins/simple-local-avatars/)** - Replaces Gravatar with local avatars (no external service calls)
- **[Koko Analytics](https://wordpress.org/plugins/koko-analytics/)** - Privacy-friendly, cookieless analytics (self-hosted, GDPR-compliant)
- **[Simple Location](https://wordpress.org/plugins/simple-location/)** - Uses OpenStreetMap instead of Google Maps
- **[ActivityPub](https://wordpress.org/plugins/activitypub/)** - Connect your WordPress site to the Fediverse for decentralized social networking

#### Security & Anti-Spam

- **[WP Cerber Security](https://wordpress.org/plugins/wp-cerber/)** or **[Wordfence Security](https://wordpress.org/plugins/wordfence/)** - Rate limiting and login protection
- **[CleanTalk](https://wordpress.org/plugins/cleantalk-spam-protect/)** - Effective spam protection that works well with Tor users
- **[Math Captcha](https://wordpress.org/plugins/wp-math-captcha/)** - Self-hosted CAPTCHA alternative (avoid Google reCAPTCHA which blocks many Tor users)
- **[Disable Comments](https://wordpress.org/plugins/disable-comments/)** - Reduces spam attack surface if comments aren't needed

#### Content Security

- **[HTTP Headers](https://wordpress.org/plugins/http-headers/)** - Add security headers and control referrer policies
- **[Content Security Policy Manager](https://wordpress.org/plugins/content-security-policy-manager/)** - Prevents loading of external resources for better security

**Installation tip**: Install these plugins through the WordPress admin interface after completing initial setup. Focus on performance plugins first to optimize for Tor's network characteristics.

### Local Testing

For testing purposes, your WordPress site is also available at:
- http://localhost:8080 (only accessible from your Mac)

## Viewing Your Site

Your site is viewable on any Tor-enabled browser:

- **[Tor Browser](https://www.torproject.org/download/)** — Windows, Mac, Linux (the gold standard)
- **[Brave Browser](https://brave.com/)** — Windows, Mac, Linux, Android (built-in Tor window)
- **[Onion Browser](https://apps.apple.com/app/onion-browser/id519296448)** — iPhone & iPad
- **[Tor Browser for Android](https://play.google.com/store/apps/details?id=org.torproject.torbrowser)** — Android

## Architecture

OnionPress uses:
- **[WordPress](https://wordpress.org/)** — Content management system
- **[Tor](https://www.torproject.org/)** — Onion service for permanent .onion addresses
- **[MariaDB](https://mariadb.org/)** — Database
- **[Wayback Machine](https://web.archive.org/)** — Automatic archiving and offline replay
- **[Docker](https://www.docker.com/)** — Container isolation
- **[Colima](https://github.com/abiosoft/colima)** — Container runtime for macOS (bundled)

All data is stored in:
- `~/.onionpress/` — Application data, logs, config
- Docker volumes for WordPress content, database, and Tor keys

## Building from Source

```bash
# Mac DMG
bash build/build-dmg-simple.sh

# Linux .deb and AppImage
bash build/build-linux.sh
```

## Troubleshooting

### "macOS version too old"
OnionPress requires macOS 13 (Ventura) or later for Apple's native virtualization framework.

### Containers won't start
Check the logs via the menu bar app or run:
```bash
tail -f ~/.onionpress/onionpress.log
tail -f ~/.onionpress/colima/colima.log
```

### Onion address not generating
Wait 30-60 seconds for Tor to generate your onion address. Check logs if it takes longer.

## Security Notes

- Change the default WordPress admin password immediately after installation
- Your site is only accessible via Tor by default (port 8080 is localhost-only for testing)
- Keep WordPress and plugins updated regularly

## Uninstalling

1. Click Uninstall from the menu bar app 
2. Quit OnionPress
3. Move `OnionPress.app` to Trash
   or
1. Quit OnionPress
2. Move `OnionPress.app` to Trash
3. Remove data directory: `rm -rf ~/.onionpress`
4. Remove Docker volumes:
   ```bash
   docker volume rm onionpress-tor-keys onionpress-wordpress-data onionpress-db-data
   ```
5. Reboot

## License

AGPL 3 License - See LICENSE file for details

## Credits

A [Decentralized Web](http://brewster.kahle.org/2015/08/11/locking-the-web-open-a-call-for-a-distributed-web-2/) project.

Free and Open Source (AGPL 3). [Donate to the Tor Project](https://donate.torproject.org/) · [Donate to the Internet Archive](https://archive.org/donate/)

## Support

For issues, questions, or contributions, visit the [GitHub repository](https://github.com/brewsterkahle/onionpress) or the [product page](https://onionpress.org).
