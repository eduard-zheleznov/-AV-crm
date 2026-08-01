from pathlib import Path


def test_windows_powershell_scripts_have_utf8_bom():
    """Windows PowerShell 5.1 needs a BOM to decode Russian UTF-8 safely."""
    scripts = Path(__file__).parents[1] / "scripts"

    for path in scripts.glob("*.ps1"):
        assert path.read_bytes().startswith(b"\xef\xbb\xbf"), path.name


def test_remote_control_task_is_interactive_and_explicitly_live():
    path = Path(__file__).parents[1] / "scripts" / "install-remote-control.ps1"
    script = path.read_text(encoding="utf-8-sig")

    assert "--allow-live-crm" in script
    assert "-LogonType Interactive" in script
    assert "-AtLogOn" in script
    assert "pythonw.exe" in script
    assert "Test-ActiveWorkerLock" in script
    assert "GetProcessById" in script
    assert "SafeStopTimeoutSeconds" in script
    assert script.index("while ((Test-ActiveWorkerLock)") < script.index("Stop-ScheduledTask")


def test_main_install_refreshes_an_existing_remote_controller():
    path = Path(__file__).parents[1] / "scripts" / "install.ps1"
    script = path.read_text(encoding="utf-8-sig")

    assert "Get-ScheduledTask -TaskName $RemoteTaskName" in script
    assert 'Join-Path $PSScriptRoot "install-remote-control.ps1"' in script


def test_remote_control_uninstall_waits_for_a_safe_worker_stop():
    path = Path(__file__).parents[1] / "scripts" / "uninstall-remote-control.ps1"
    script = path.read_text(encoding="utf-8-sig")

    assert "SafeStopTimeoutSeconds" in script
    assert "Test-ActiveWorkerLock" in script
    assert "GetProcessById" in script
    assert "while ((Test-ActiveWorkerLock)" in script
    assert script.index("while ((Test-ActiveWorkerLock)") < script.index("Stop-ScheduledTask")


def test_remote_status_reports_lock_owners_and_phase_without_mutating_runtime():
    path = Path(__file__).parents[1] / "scripts" / "remote-control-status.ps1"
    script = path.read_text(encoding="utf-8-sig")

    assert "ShowRecentLog" in script
    assert "Get-Process -Id $OwnerPid" in script
    assert "remote-control.json" in script
    assert "$State.phase" in script
    assert "worker.lock" in script
    assert "Remove-Item" not in script


def test_vds_settings_import_preserves_the_local_chrome_bridge_and_backs_up_env():
    path = Path(__file__).parents[1] / "scripts" / "import-vds-settings.ps1"
    script = path.read_text(encoding="utf-8-sig")

    assert '"AVITO_EXTENSION_TOKEN"' in script
    assert '"AVITO_BROWSER_DRIVER" "chrome_extension"' in script
    assert '"LOCAL_TIME_GUARD_ENABLED" "true"' in script
    assert ".env.before-vds-import-" in script
    assert "ConvertFrom-Json" in script
    assert "GOOGLE_CREDENTIALS_FILE" in script
    assert "Write-Host $LocalToken" not in script
    assert '"=([^`r`n]*)$"' in script
    assert '"=[^`r`n]*$"' in script
