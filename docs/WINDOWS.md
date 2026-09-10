# Modelfiche for Windows

Modelfiche keeps image datasets, captions, training runs, checkpoints, and comparisons together on your computer.

## Open the app

1. Download `Modelfiche-0.1.0-windows-x64.zip` from [GitHub Releases](https://github.com/latentwill/modelfiche/releases/latest).
2. Right-click the ZIP and choose **Extract All**. Keep the extracted files together in a folder you own.
3. Open **Modelfiche.exe**. The launcher opens your default browser when the app is ready.
4. Keep the launcher window open while working. **Stop and close**, or the window's close button, stops the services. Closing just the browser leaves the services running.

The package includes Python, the web interface, API, worker, W&B-compatible ingress, migrations, and the `mfiche.exe` CLI. No Python, Node.js, terminal setup, administrator rights, or trainer installation is needed to run Modelfiche.

Requires **Windows 10 or Windows 11, 64-bit Intel/AMD**, with the built-in .NET Framework 4.8. ARM64 has no native build in this release. The app is unsigned, so Windows may display an unknown-publisher or SmartScreen notice. The SHA-256 file on the release page lets you verify the download; it is not a code signature.

## Your data

The app saves work independently of the extracted application folder:

- Data, database, and settings: `%LOCALAPPDATA%\Modelfiche`
- Logs: `%LOCALAPPDATA%\Modelfiche\logs`
- Default backup folder: `%LOCALAPPDATA%\Modelfiche-backups`

To update, stop Modelfiche, extract the new version into a new folder, and open its launcher. Existing data is reused. A database upgrade creates a backup before migration. Removing the application folder does not delete your workspace.

Use a local disk and your own Windows account. Windows directory permissions protect the per-user data folder; Unix permission bits and directory `fsync` guarantees do not apply. The internal descriptor-relative `LocalRoot` publication primitive is POSIX-only; the Windows browser import, asset cache, and backup paths use the portable storage implementations.

## Command line

Open PowerShell in the extracted Modelfiche folder:

```powershell
.\mfiche.exe --json doctor
.\mfiche.exe --json capabilities
.\mfiche.exe --json workspace list
.\mfiche.exe --json --workspace <workspace-id-or-slug> project list
.\mfiche.exe status
.\mfiche.exe stop
.\mfiche.exe start
```

Settings can install a forwarding command in `%USERPROFILE%\.local\bin`; add that folder to your user `PATH` to use `mfiche` from any directory. The forwarding command points to this extracted app, so reinstall it after moving or upgrading the application folder.

Create a backup while the app is running:

```powershell
.\mfiche.exe --json backup create
```

See [Installation and operations](https://github.com/latentwill/modelfiche/blob/main/docs/OPERATIONS.md) for backup/restore and remote access. Training packets target the trainer's environment, usually Linux; the Windows application does not install or run the GPU trainer.

## Troubleshooting

Open **View logs** in the launcher if startup fails. The normal address is `http://127.0.0.1:8400/`; the W&B-compatible listener uses port 8401. Both listen on this computer only. The launcher refuses to use ports already occupied by another application.

To choose other ports, set both before opening the app:

```powershell
$env:TITLES_API_PORT = "8500"
$env:TITLES_WANDB_INGRESS_PORT = "8501"
.\Modelfiche.exe
```

Use the same environment for CLI commands. For isolated testing, `TITLES_APP_SUPPORT` can select another data directory. FAL, LLM, and storage credentials remain optional and are configured in Settings. No paid provider calls are made during the build or smoke tests.

## Build and verify

On a Windows development machine, install Python, uv, Node.js, and pnpm, then:

```powershell
uv sync --frozen --extra dev
pnpm --dir apps/web install --frozen-lockfile
.\scripts\build_windows_app.ps1
uv run python scripts/windows/smoke_test.py
```

The build pins and verifies the official Python embeddable ZIP, installs locked Python dependencies, builds the locked frontend, and compiles the native launchers. Release files are in `dist/windows/release`. The smoke test extracts the actual release archive to a path with spaces and non-ASCII characters and tests the native executable against disposable data.
