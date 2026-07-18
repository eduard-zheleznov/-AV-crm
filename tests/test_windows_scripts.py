from pathlib import Path


def test_windows_powershell_scripts_have_utf8_bom():
    """Windows PowerShell 5.1 needs a BOM to decode Russian UTF-8 safely."""
    scripts = Path(__file__).parents[1] / "scripts"

    for path in scripts.glob("*.ps1"):
        assert path.read_bytes().startswith(b"\xef\xbb\xbf"), path.name
