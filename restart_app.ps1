$ErrorActionPreference = "Stop"

$pythonExe = "C:\Users\ASUS\AppData\Local\Programs\Python\Python312\python.exe"
$workdir = "C:\schedulechatbot"
$stdoutLog = Join-Path $workdir "streamlit.out.log"
$stderrLog = Join-Path $workdir "streamlit.err.log"

if (!(Test-Path $pythonExe)) {
    Write-Output "Python not found: $pythonExe"
    exit 1
}

# Stop old Streamlit app process first.
$existing = Get-CimInstance Win32_Process |
    Where-Object { $_.Name -eq "python.exe" -and $_.CommandLine -like "*streamlit run app.py*" }
foreach ($proc in $existing) {
    Stop-Process -Id $proc.ProcessId -Force
}

Start-Sleep -Milliseconds 600

# Force UTF-8 stream encoding for verbose debug output on Windows.
$env:PYTHONIOENCODING = "utf-8"
$env:PYTHONUTF8 = "1"

# Start detached.
Start-Process `
    -FilePath $pythonExe `
    -ArgumentList "-m", "streamlit", "run", "app.py", "--server.headless", "true" `
    -WorkingDirectory $workdir `
    -RedirectStandardOutput $stdoutLog `
    -RedirectStandardError $stderrLog

# Wait until app is live.
$maxAttempts = 30
for ($i = 1; $i -le $maxAttempts; $i++) {
    try {
        $resp = Invoke-WebRequest -Uri "http://localhost:8501" -UseBasicParsing -TimeoutSec 3
        if ($resp.StatusCode -eq 200) {
            $running = Get-CimInstance Win32_Process |
                Where-Object { $_.Name -eq "python.exe" -and $_.CommandLine -like "*streamlit run app.py*" } |
                Select-Object -First 1
            if ($running) {
                Write-Output "Restart OK. PID: $($running.ProcessId), HTTP: 200"
                exit 0
            }
        }
    } catch {
        # continue polling
    }
    Start-Sleep -Seconds 1
}

Write-Output "Restart failed. Last stderr tail:"
if (Test-Path $stderrLog) {
    Get-Content $stderrLog -Tail 40
}
exit 2
