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


def test_main_install_can_skip_the_unused_playwright_browser_for_extension_nodes():
    path = Path(__file__).parents[1] / "scripts" / "install.ps1"
    script = path.read_text(encoding="utf-8-sig")

    assert "[switch]$SkipPlaywright" in script
    assert "[switch]$SkipRemoteControlRefresh" in script
    assert "if (-not $SkipPlaywright)" in script
    assert "-m playwright install chromium" in script
    assert "$RemoteTask -and -not $SkipRemoteControlRefresh" in script


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
    assert '"AVITO_EXTENSION_INCOGNITO"' in script
    assert '(Get-EnvValue $WrittenText "AVITO_EXTENSION_INCOGNITO") -ne "true"' in script
    assert '"LOCAL_TIME_GUARD_ENABLED" "true"' in script
    assert ".env.before-vds-import-" in script
    assert "ConvertFrom-Json" in script
    assert "GOOGLE_CREDENTIALS_FILE" in script
    assert '"GOOGLE_CREDENTIALS_FILE" $TargetGoogle' in script
    assert "$TargetGoogle.FullName" not in script
    assert "WrittenGooglePath" in script
    assert "Write-Host $LocalToken" not in script
    assert "config.local.js" in script
    assert "TokenMatch" in script
    assert '"AVITO_EXTENSION_TOKEN" $LocalToken' in script
    assert "System.StringComparison]::Ordinal" in script
    assert '"=([^`r`n]*)"' in script
    assert '"=[^`r`n]*"' in script
    assert '"NOTIFICATION_COMPUTER_NAME" $NotificationComputerName.Trim()' in script


def test_extension_installer_enables_incognito_and_requires_user_permission():
    path = Path(__file__).parents[1] / "scripts" / "install-chrome-extension.ps1"
    script = path.read_text(encoding="utf-8-sig")

    assert '"AVITO_EXTENSION_INCOGNITO" = "true"' in script
    assert "Разрешить использование в режиме инкогнито" in script


def test_remote_control_can_be_registered_fail_closed_without_google_writes():
    path = Path(__file__).parents[1] / "scripts" / "install-remote-control.ps1"
    script = path.read_text(encoding="utf-8-sig")

    assert "[switch]$SkipGoogleSetup" in script
    assert "[switch]$Disabled" in script
    assert "if ($SkipGoogleSetup -and -not $Disabled)" in script
    assert "if (-not $SkipGoogleSetup)" in script
    assert "Disable-ScheduledTask -TaskName $TaskName" in script
    assert script.index("Disable-ScheduledTask -TaskName $TaskName") < script.index(
        "Пульт зарегистрирован, но отключён."
    )


def test_worker_vm_host_preflight_is_read_only_and_fail_closed():
    path = Path(__file__).parents[1] / "scripts" / "worker-vm-host-preflight.ps1"
    script = path.read_text(encoding="utf-8-sig")

    assert '"BLOCKED"' in script
    assert "$TotalMemoryGB -lt 12" in script
    assert "$LogicalCpuCount -lt 4" in script
    assert "$GuestDiskGB + 20" in script
    assert "VirtualizationFirmwareEnabled" in script
    for mutator in (
        "Remove-Item",
        "Set-Content",
        "New-Item",
        "Register-ScheduledTask",
        "winget.exe install",
    ):
        assert mutator not in script


def test_worker_vm_creator_uses_isolated_nat_and_never_deletes_an_existing_vm():
    path = Path(__file__).parents[1] / "scripts" / "new-worker-vm-virtualbox.ps1"
    script = path.read_text(encoding="utf-8-sig")

    assert '$VirtualBoxVersion = "7.2.8"' in script
    assert '"--tpm-type", "2.0"' in script
    assert '"--user-password-file", $PasswordFile' in script
    assert "$PlainPassword.Length -lt 12" in script
    assert '"--nic1", "nat"' in script
    assert '"--clipboard-mode", "disabled"' in script
    assert '"--drag-and-drop", "disabled"' in script
    assert '"--readonly"' in script
    assert '"AvitoCrmTransfer"' in script
    assert "$InteractiveIdentity" in script
    assert "-LogonType Interactive -RunLevel Limited" in script
    assert "уже существует. Скрипт не изменяет и не удаляет" in script
    assert 'unregistervm", $VmName, "--delete"' not in script


def test_worker_node_installer_is_exact_versioned_and_starts_with_stop():
    path = Path(__file__).parents[1] / "scripts" / "install-worker-node.ps1"
    script = path.read_text(encoding="utf-8-sig")

    assert "[Parameter(Mandatory = $true)]" in script
    assert "[string]$ReleaseCommit" in script
    assert "checkout --detach $ReleaseCommit" in script
    assert '$ExpectedAppVersion = "1.12.43.1"' in script
    assert '$ExpectedExtensionVersion = "1.0.19"' in script
    assert "New-Item -ItemType File -Path $StopPath -Force" in script
    assert "& $InstallScript -SkipPlaywright -SkipRemoteControlRefresh" in script
    assert '"Google.Chrome"' in script
    assert '"UB-Mannheim.TesseractOCR"' in script
    assert "install-remote-control.ps1" not in script
    assert "worker-node-install.json" in script


def test_worker_node_completion_is_hash_checked_and_remains_passive():
    path = Path(__file__).parents[1] / "scripts" / "complete-worker-node.ps1"
    script = path.read_text(encoding="utf-8-sig")

    assert "Get-FileHash -LiteralPath $TransferEnv -Algorithm SHA256" in script
    assert "Get-FileHash -LiteralPath $TransferGoogle -Algorithm SHA256" in script
    assert "worker-node-install.json" in script
    assert "-Source google -OnlineCrm" in script
    assert "-NoStart -SkipGoogleSetup -Disabled" in script
    assert "New-Item -ItemType File -Path $StopPath -Force" in script
    assert "Disable-ScheduledTask" in script.rsplit("catch {", 1)[1]
    assert "WORKER NODE READY (PASSIVE)" in script


def test_worker_node_handoff_requires_one_active_controller_and_false_panel_flags():
    disable = (Path(__file__).parents[1] / "scripts" / "disable-worker-node.ps1").read_text(
        encoding="utf-8-sig"
    )
    enable = (Path(__file__).parents[1] / "scripts" / "enable-worker-node.ps1").read_text(
        encoding="utf-8-sig"
    )

    assert "OLD NODE DISABLED" in disable
    assert "Disable-ScheduledTask" in disable
    assert "Test-ActiveLock -Path $WorkerLock" in disable
    assert "Test-ActiveLock -Path $ControllerLock" in disable
    assert "[switch]$ConfirmPreviousNodeStopped" in enable
    assert 'json.dumps({"start": c.start, "stop": c.stop})' in enable
    assert "if ([bool]$Panel.start -or [bool]$Panel.stop)" in enable
    assert "NEW NODE ACTIVE" in enable
    assert "Test-ActiveLock -Path $ControllerLock" in enable
    assert enable.index("Remove-Item -LiteralPath $StopPath") < enable.index("Start-ScheduledTask")
    assert "Disable-ScheduledTask" in enable.rsplit("catch {", 1)[1]


def test_worker_node_offline_smoke_always_restores_stop_and_never_uses_live_crm():
    path = Path(__file__).parents[1] / "scripts" / "test-worker-node.ps1"
    script = path.read_text(encoding="utf-8-sig")

    assert '$Task.State -ne "Disabled"' in script
    assert "avito-extension-test $Url --max-clicks 1" in script
    assert "finally {" in script
    assert "New-Item -ItemType File -Path $StopPath -Force" in script
    assert "--live" not in script
    assert "--allow-live-crm" not in script


def test_worker_settings_export_never_prints_secret_values_and_uses_restricted_acl():
    path = Path(__file__).parents[1] / "scripts" / "export-worker-node-settings.ps1"
    script = path.read_text(encoding="utf-8-sig")

    assert "Copy-Item -LiteralPath $EnvPath" in script
    assert "Copy-Item -LiteralPath $GooglePath" in script
    assert "/inheritance:r" in script
    assert "env_sha256" in script
    assert "google_sha256" in script
    assert "Write-Host $EnvText" not in script
    assert "Write-Host $GooglePayload" not in script


def test_host_bootstrap_defaults_to_preflight_and_needs_an_explicit_create_switch():
    path = Path(__file__).parents[1] / "scripts" / "bootstrap-worker-vm-host.ps1"
    script = path.read_text(encoding="utf-8-sig")

    assert "[string]$ReleaseCommit" in script
    assert "if (-not $Create)" in script
    assert "worker-vm-host-preflight.ps1" in script
    assert "prepare-worker-vm-host.ps1" in script
    assert "-InstallVirtualBox:$InstallVirtualBox" in script


def test_autologon_helper_accepts_only_a_valid_microsoft_signed_binary_in_a_vm():
    path = Path(__file__).parents[1] / "scripts" / "prepare-worker-vm-autologon.ps1"
    script = path.read_text(encoding="utf-8-sig")

    assert "https://download.sysinternals.com/files/AutoLogon.zip" in script
    assert "Get-AuthenticodeSignature" in script
    assert '$Signature.Status -ne "Valid"' in script
    assert "SignerCertificate.Subject" in script
    assert 'notmatch "VirtualBox|VMware|Virtual Machine|KVM|QEMU|Parallels|Xen"' in script
    assert "Get-Credential" not in script
