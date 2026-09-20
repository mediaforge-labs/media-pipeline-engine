param([string]$Repo='C:\media-pipeline-engine',[string]$Root='C:\LeonidanosVideoPipeline',[switch]$CatalogOnly)
$ErrorActionPreference='Stop'; $cfg=Join-Path $env:LOCALAPPDATA 'MediaForge'; $cred=Join-Path $cfg 'supabase-secret.dpapi'; $urlfile=Join-Path $cfg 'gateway-url.txt'; $cloudflared=Join-Path $cfg 'cloudflared.exe'; $log=Join-Path $cfg 'cloudflared.log'
if(!(Test-Path $cred)){throw 'Credencial local ausente. Rode setup_dell_asset_gateway.ps1 primeiro.'}
$s=Get-Content $cred | ConvertTo-SecureString; $p=[Runtime.InteropServices.Marshal]::SecureStringToBSTR($s); try{$env:SUPABASE_SECRET_KEY=[Runtime.InteropServices.Marshal]::PtrToStringBSTR($p)}finally{[Runtime.InteropServices.Marshal]::ZeroFreeBSTR($p)}
$env:SUPABASE_URL='https://rhddgfvtrkmusbvphnlg.supabase.co'
$py=(Get-Command python -ErrorAction SilentlyContinue); if(!$py){$py=(Get-Command py -ErrorAction SilentlyContinue)}; if(!$py){throw 'Python nao encontrado.'}
& $py.Source -m pip install --disable-pip-version-check requests==2.32.5 pypdf==6.1.1 | Out-Null
if($CatalogOnly){& $py.Source (Join-Path $Repo 'tools\dell_asset_gateway.py') --root $Root --sync-catalog; exit $LASTEXITCODE}
Remove-Item $urlfile,$log -ErrorAction SilentlyContinue
$gateway=Start-Process $py.Source -ArgumentList (Join-Path $Repo 'tools\dell_asset_gateway.py'),'--root',$Root,'--endpoint-file',$urlfile -PassThru -WindowStyle Hidden
Start-Sleep -Seconds 2
$tunnel=Start-Process $cloudflared -ArgumentList 'tunnel','--url','http://127.0.0.1:8765','--no-autoupdate' -RedirectStandardError $log -PassThru -WindowStyle Hidden
$url=$null
for($i=0;$i -lt 60;$i++){Start-Sleep -Seconds 1;if(Test-Path $log){$m=Select-String -Path $log -Pattern 'https://[a-z0-9-]+\.trycloudflare\.com' -AllMatches | Select-Object -Last 1;if($m){$url=$m.Matches[0].Value;break}}}
if(!$url){Stop-Process -Id $gateway.Id -Force -ErrorAction SilentlyContinue;Stop-Process -Id $tunnel.Id -Force -ErrorAction SilentlyContinue;throw 'Nao foi possivel obter URL do Cloudflare Tunnel.'}
Set-Content -Path $urlfile -Value $url -Encoding UTF8
Write-Host "Gateway online: $url"
Wait-Process -Id $gateway.Id
