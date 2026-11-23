$ErrorActionPreference = "Stop"
$dir = $PSScriptRoot
$desktop = [Environment]::GetFolderPath("Desktop")
Set-Location -LiteralPath $dir

Get-Process | Where-Object { $_.ProcessName -match "^python|LEHA Vortex" } | Stop-Process -Force -ErrorAction SilentlyContinue
Start-Sleep -Milliseconds 300

# Đảm bảo PyInstaller sẵn sàng
python -m PyInstaller --version 2>$null | Out-Null
if ($LASTEXITCODE -ne 0) {
    pip install --quiet pyinstaller
}

# Đóng gói One-File Windows Executable kèm studio_ui.html & app_icon.ico
python -m PyInstaller --noconfirm --clean --onefile --windowed `
    --name "LEHA Vortex Workspace Suite" `
    --icon "app_icon.ico" `
    --version-file "version_info.txt" `
    --add-data "studio_ui.html;." `
    --add-data "app_icon.ico;." `
    --hidden-import "pyvda" `
    --hidden-import "win32com.client" `
    --hidden-import "PIL" `
    "vortex_suite.py"

$exePath = Join-Path $dir "dist\LEHA Vortex Workspace Suite.exe"
if (-not (Test-Path -LiteralPath $exePath)) {
    throw "Không tìm thấy tệp thực thi sau khi biên dịch!"
}

# Ký chữ ký số Authenticode (CN=Le Ha)
$certSubject = "CN=Le Ha, O=LEHA Studio, C=VN"
$cert = Get-ChildItem Cert:\CurrentUser\My -CodeSigningCert | Where-Object { $_.Subject -eq $certSubject } | Select-Object -First 1
if (-not $cert) {
    $cert = New-SelfSignedCertificate -Type CodeSigningCert -Subject $certSubject -KeyAlgorithm RSA -KeyLength 2048 -HashAlgorithm SHA256 -CertStoreLocation "Cert:\CurrentUser\My" -NotAfter (Get-Date).AddYears(5)
}

# Cài chứng chỉ vào Trusted Root & Trusted Publishers của CurrentUser để Windows xác thực Valid
foreach ($storeName in @("Root", "TrustedPublisher")) {
    $store = New-Object System.Security.Cryptography.X509Certificates.X509Store($storeName, "CurrentUser")
    $store.Open("ReadWrite")
    if (-not ($store.Certificates | Where-Object { $_.Thumbprint -eq $cert.Thumbprint })) {
        $store.Add($cert)
    }
    $store.Close()
}

$sig = Set-AuthenticodeSignature -FilePath $exePath -Certificate $cert -HashAlgorithm SHA256
if ($sig.Status -ne "Valid") {
    throw "Ký chữ ký số thất bại: $($sig.StatusMessage)"
}

# Xuất bản phát hành sạch ra Desktop (Portable Release ZIP & Source Backup ZIP)
$releaseDir = Join-Path $dir "dist\LEHA_Vortex_Workspace_Suite_v1.2.6_Signed"
New-Item -ItemType Directory -Force -Path $releaseDir | Out-Null
Copy-Item -LiteralPath $exePath -Destination $releaseDir -Force
Copy-Item -LiteralPath (Join-Path $dir "LICENSE") -Destination $releaseDir -Force -ErrorAction SilentlyContinue
Copy-Item -LiteralPath (Join-Path $dir "README.md") -Destination $releaseDir -Force -ErrorAction SilentlyContinue

$relZip = Join-Path $desktop "LEHA_Vortex_Workspace_Suite_v1.2.6_Signed.zip"
$srcZip = Join-Path $desktop "LEHA_Vortex_Workspace_Suite_v1.2.6_Source.zip"
Remove-Item -LiteralPath $relZip, $srcZip -Force -ErrorAction SilentlyContinue

Compress-Archive -Path "$releaseDir\*" -DestinationPath $relZip -CompressionLevel Optimal
Compress-Archive -Path @(
    (Join-Path $dir "vortex_suite.py"),
    (Join-Path $dir "studio_ui.html"),
    (Join-Path $dir "app_icon.ico"),
    (Join-Path $dir "version_info.txt"),
    (Join-Path $dir "BUILD_AND_SIGN.ps1"),
    (Join-Path $dir "LICENSE"),
    (Join-Path $dir "README.md")
) -DestinationPath $srcZip -CompressionLevel Optimal

# Dọn dẹp thư mục build tạm của PyInstaller giữ thư mục gốc sạch sẽ
Remove-Item -LiteralPath (Join-Path $dir "build"), (Join-Path $dir "dist"), (Join-Path $dir "LEHA Vortex Workspace Suite.spec") -Recurse -Force -ErrorAction SilentlyContinue

# Khởi chạy bản mới nhất
# Handled by installed executable
Write-Host "==========================================================" -ForegroundColor Green
Write-Host "BUILD & CODE SIGNING COMPLETED SUCCESSFULLY (CN=Le Ha)" -ForegroundColor Green
Write-Host "1. Portable Signed ZIP : $relZip" -ForegroundColor Cyan
Write-Host "2. Clean Source ZIP    : $srcZip" -ForegroundColor Cyan
Write-Host "==========================================================" -ForegroundColor Green