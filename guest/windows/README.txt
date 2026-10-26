Palette Windows Guest Tools / Windows x64

Inside the Windows guest, open this optical disc and run Install.cmd.
Accept Windows administrator approval. Restart Windows after installation.
The installer does not restart the guest automatically. Re-run to retry failed
installation, or use each upstream component's Windows Apps repair/uninstaller.

Includes VirtIO drivers, QEMU Guest Agent and SPICE Agent from the official
virtio-win 0.1.285 bundle, and SPICE WebDAV daemon 2.4. Payloads retain upstream
installers, signatures, notices and licenses. See payloads.json for URLs/hashes.

Sources, licenses and packaging:
https://github.com/virtio-win/kvm-guest-drivers-windows
https://github.com/virtio-win/virtio-win-guest-tools-installer
https://www.spice-space.org/download.html
https://gitlab.gnome.org/GNOME/phodav

Windows 10/11 and Server 2016 or newer, x64 only. Does not support ARM64/x86.
Logs: %ProgramData%\Palette\GuestTools\Logs
Installer failures stop the sequence and report a log location. Installed
components are retained for repair. No rollback or automatic reboot is attempted.

Palette must expose the QGA and SPICE virtio channels in the VM's host Profile.
The signed VirtIO serial driver lets the services connect through those channels.
After boot/login, Palette checks responses rather than assuming installation.
Clipboard/Unicode paste and QGA file transfer are available when services respond.
The disc also compiles Palette.Agent.cs, Palette.Native.cs and Palette.Operations.cs
with the Windows inbox .NET Framework compiler. PaletteGuestTools runs a transport-only
SYSTEM service; file/system/App/UIA/clipboard operations run in the active console
user's ordinary process. No automatic UAC elevation is performed for operations.
Native Unicode input and coordinate/label batch tools use org.palette.tools.0.
Run Install.cmd -Uninstall to remove only this Palette service and helper files.
Windows automatic resolution, TPM changes and setup bypasses are not provided.
WebDAV shares contain only folders explicitly exported by the host.
