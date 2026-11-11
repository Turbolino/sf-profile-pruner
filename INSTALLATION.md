# Installing Profile Pruner (Windows)

This guide walks through a clean install of **Profile Pruner** on Windows and
how to make the `sfpp` command available from a terminal.

The package is named **`sf-profile-pruner`**; `sfpp` is the *command* the
package installs.

> **Most common symptom**: after `pip install sf-profile-pruner`, `sfpp` is
> "not recognized" (`could not find the file`). This is not a package bug: the
> `sfpp.exe` executable is installed, but the Python `Scripts` folder is not on
> your `PATH`. STEP 4 below fixes it.

---

## STEP 1 — Install Python correctly

1. Download the latest **Python 3.x** from <https://www.python.org/downloads/>
   (Windows 64-bit installer).
2. Run the installer and — **the key step** — check:
   - ☑ **Add python.exe to PATH**
   - ☑ **Install launcher for all users** (recommended)
3. Click **Install Now**.
4. **Close and reopen the terminal**, then verify:

   ```powershell
   python --version
   ```

   It should print something like `Python 3.13.x`.

> If it opens the **Microsoft Store**, or the path contains `WindowsApps`, you
> are using the Store Python. Uninstall it and redo STEP 1 with the python.org
> installer (the one with the PATH checkbox).

---

## STEP 2 — Install Profile Pruner

Always use `python -m pip` (so you use *exactly* the Python you just installed):

```powershell
python -m pip install --upgrade sf-profile-pruner
```

Wait for the line `Successfully installed sf-profile-pruner-...`.

> `Requirement already satisfied` (on a second attempt) is normal: it means the
> package is already installed. It has nothing to do with the `PATH`.

---

## STEP 3 — Find the `Scripts` folder

```powershell
python -c "import sysconfig; print(sysconfig.get_path('scripts'))"
```

**Copy** the printed path (e.g.
`C:\Users\you\AppData\Local\Programs\Python\Python313\Scripts`). Confirm that
`sfpp.exe` is there:

```powershell
Get-ChildItem (python -c "import sysconfig; print(sysconfig.get_path('scripts'))") -Filter "sfpp*"
```

If you see `sfpp.exe`, the package is installed: only the `PATH` is missing.

---

## STEP 4 — Add the `Scripts` folder to the user `PATH`

### Method A — via the GUI (always works)

This method also works when PowerShell is in **Constrained Language Mode**
(which blocks .NET methods such as `[Environment]::SetEnvironmentVariable`).

1. Press **Win + R**.
2. Type **`sysdm.cpl`** and press **Enter**.
3. Go to the **Advanced** tab.
4. Click **Environment Variables...**.
5. In the upper section (*User variables for `<your name>`*), select **`Path`**.
6. Click **Edit...**.
7. Click **New**.
8. **Paste** the `Scripts` folder you copied in STEP 3.
9. Click **OK** on every open dialog.

### Method B — from PowerShell (only if not in constrained mode)

Replace `<FOLDER>` with the path from STEP 3:

```powershell
[Environment]::SetEnvironmentVariable(
  'Path',
  [Environment]::GetEnvironmentVariable('Path','User') + ';<FOLDER>',
  'User'
)
```

Example:

```powershell
[Environment]::SetEnvironmentVariable('Path', [Environment]::GetEnvironmentVariable('Path','User') + ';C:\Users\you\AppData\Local\Programs\Python\Python313\Scripts', 'User')
```

> If you get *"Unable to call the method... it is supported only for base
> types"*, you are in Constrained Language Mode: use **Method A**.

---

## STEP 5 — Reopen the terminal and verify

The `PATH` only updates in a **new window**. Close the terminal, reopen it, and:

```powershell
sfpp --version
```

It should print:

```
Profile Pruner v1.10.9 - Made with <3 by Lorenzo D'Aleo
(turbolino.dev@proton.me)
```

---

## Fallback without touching the `PATH`

The tool also runs as a Python module, so it always works even when `sfpp` is
not on the `PATH`:

```powershell
python -m prune_profile --version
python -m prune_profile --help
```

## Quick test (current session only)

To try it right now without changing anything (it disappears when the terminal
closes), replace `<FOLDER>` and run:

```powershell
$env:Path += ";<FOLDER>"
sfpp --version
```

---

## Troubleshooting

If `sfpp` still does not work, run and report the output of:

```powershell
python -c "import sysconfig; print(sysconfig.get_path('scripts'))"
where.exe sfpp
Get-ChildItem (python -c "import sysconfig; print(sysconfig.get_path('scripts'))") -Filter "sfpp*"
```

To check whether PowerShell is in constrained mode:

```powershell
$ExecutionContext.SessionState.LanguageMode
```

If it prints `ConstrainedLanguage`, use **Method A** in STEP 4.
