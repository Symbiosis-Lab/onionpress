import Cocoa

/// Thin wrapper that launches the bash launcher script and handles macOS
/// Apple Events (reopen on double-click, quit via osascript).
/// All real logic stays in launcher.sh.

class AppDelegate: NSObject, NSApplicationDelegate {
    var launcherProcess: Process?
    var dataDir: String {
        NSHomeDirectory() + "/.onionpress"
    }

    func applicationDidFinishLaunching(_ notification: Notification) {
        let scriptDir = Bundle.main.bundlePath + "/Contents/MacOS"
        let script = scriptDir + "/launcher.sh"

        let process = Process()
        process.executableURL = URL(fileURLWithPath: "/bin/bash")
        process.arguments = [script]
        process.terminationHandler = { _ in
            // MenubarApp exited — quit the wrapper too
            DispatchQueue.main.async {
                NSApplication.shared.terminate(nil)
            }
        }
        do {
            try process.run()
            launcherProcess = process
        } catch {
            NSLog("Failed to launch launcher.sh: \(error)")
            NSApplication.shared.terminate(nil)
        }
    }

    func applicationShouldHandleReopen(_ sender: NSApplication,
                                        hasVisibleWindows flag: Bool) -> Bool {
        // User double-clicked the app while running — signal the MenubarApp
        let reopenFile = dataDir + "/.reopen"
        FileManager.default.createFile(atPath: reopenFile, contents: nil)
        // Post a distributed notification so the MenubarApp can respond immediately
        // instead of waiting for its next 30-second poll cycle
        DistributedNotificationCenter.default().postNotificationName(
            NSNotification.Name("press.onion.app.reopen"),
            object: nil)
        return false
    }

    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        // Forward quit to the launcher process (which forwards to MenubarApp)
        if let process = launcherProcess, process.isRunning {
            process.interrupt()  // SIGINT
            process.waitUntilExit()
        }
        return .terminateNow
    }
}

let app = NSApplication.shared
let delegate = AppDelegate()
app.delegate = delegate
app.run()
