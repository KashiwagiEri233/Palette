Palette Windows Guest Tools / Windows x64

During Windows installation (Windows Setup / WinPE):
Keep the Windows installation ISO mounted and mount this tools disc as well.
At the disk selection page, choose Load driver / Browse, then select:
  drivers\viostor\w11\amd64   VirtIO Block disk
  drivers\vioscsi\w11\amd64   VirtIO SCSI disk
For Windows 10 use w10. For Server 2016/2019/2022/2025 use
2k16/2k19/2k22/2k25 respectively. SATA/IDE disks use Windows inbox drivers.
Load the driver matching the configured disk controller; keep incompatible
drivers hidden. INF, SYS, CAT and companion files retain their upstream bytes.
For network setup, browse to drivers\NetKVM\<OS>\amd64\netkvm.inf.
If no driver button is offered during OOBE, press Shift+F10 and run (replace E:):
  pnputil /add-driver E:\drivers\NetKVM\w11\amd64\netkvm.inf /install
vioserial drivers for the guest-service channel are also under drivers.
The original VirtIO license is in drivers\virtio-win_license.txt.
Install.cmd is for the installed Windows desktop, not Windows Setup / WinPE.

Inside the Windows guest, open this optical disc and run Install.cmd.
Accept Windows administrator approval. Restart Windows after installation.
The installer first stages the matching serial driver and installs the Palette helper.
If an upstream installer fails, it reports failure and keeps the helper installed;
QGA, SPICE or WebDAV may still be unavailable. WebDAV is configured to restart
after a service failure. The installer does not restart Windows automatically.
Re-run to retry, or use each upstream component's Windows Apps repair/uninstaller.
If QGA VSS registration fails, inspect the MSI and Windows COM+ event logs. On one
customized Windows 11 guest, COM+ event 4691 identified a disabled MS-DTC service;
an administrator set it to Manual, started it, and then retried successfully.

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
