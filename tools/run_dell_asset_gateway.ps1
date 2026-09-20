param([string]$Repo='C:\media-pipeline-engine',[string]$Root='C:\LeonidanosVideoPipeline',[switch]$CatalogOnly)
$ErrorActionPreference='Stop'
$cfg=Join-Path $env:LOCALAPPDATA 'MediaForge'; $a=Join-Path $cfg 's3-access.dpapi'; $s=Join-Path $cfg 's3-secret.dpapi'; $urlfile=Join-Path $cfg 'gateway-url.txt'; $catalog=Join-Path $cfg 'catalog.json'; $cloudflared=Join-Path $cfg 'cloudflared.exe'; $log=Join-Path $cfg 'cloudflared.log'
function Plain($path){$sec=Get-Content $path -Raw|ConvertTo-SecureString;$p=[Runtime.InteropServices.Marshal]::SecureStringToBSTR($sec);try{return [Runtime.InteropServices.Marshal]::PtrToStringBSTR($p)}finally{[Runtime.InteropServices.Marshal]::ZeroFreeBSTR($p)}}
if(!(Test-Path $a) -or !(Test-Path $s)){throw 'Credenciais S3 locais ausentes. Rode setup_dell_asset_gateway.ps1.'}
$env:SUPABASE_S3_ACCESS_KEY_ID=Plain $a; $env:SUPABASE_S3_SECRET_ACCESS_KEY=Plain $s; $env:AWS_REQUEST_CHECKSUM_CALCULATION='when_required'; $env:AWS_RESPONSE_CHECKSUM_VALIDATION='when_required'; $env:AWS_EC2_METADATA_DISABLED='true'
$py=Get-Command python -ErrorAction SilentlyContinue;if(!$py){$py=Get-Command py -ErrorAction SilentlyContinue};if(!$py){throw 'Python nao encontrado.'}
& $py.Source -m pip install --disable-pip-version-check 'boto3==1.40.45' 'botocore==1.40.45' 's3transfer==0.14.0' 'pypdf==6.1.1' | Out-Null
if($CatalogOnly){& $py.Source (Join-Path $Repo 'tools\dell_asset_gateway.py') --root $Root --catalog-file $catalog --sync-catalog; exit $LASTEXITCODE}
Remove-Item $urlfile,$log -ErrorAction SilentlyContinue
$gateway=Start-Process $py.Source -ArgumentList (Join-Path $Repo 'tools\dell_asset_gateway.py'),'--root',$Root,'--catalog-file',$catalog,'--endpoint-file',$urlfile -PassThru -WindowStyle Hidden
Start-Sleep -Seconds 2
$tunnel=Start-Process $cloudflared -ArgumentList 'tunnel','--url','http://127.0.0.1:8765','--no-autoupdate' -RedirectStandardError $log -PassThru -WindowStyle Hidden
$url=$null
for($i=0;$i -lt 60;$i++){Start-Sleep -Seconds 1;if(Test-Path $log){$m=Select-String -Path $log -Pattern 'https://[a-z0-9-]+\.trycloudflare\.com' -AllMatches | Select-Object -Last 1;if($m){$url=$m.Matches[0].Value;break}}}
if(!$url){Stop-Process -Id $gateway.Id -Force -ErrorAction SilentlyContinue;Stop-Process -Id $tunnel.Id -Force -ErrorAction SilentlyContinue;throw 'Nao foi possivel obter URL do Cloudflare Tunnel.'}
Set-Content -Path $urlfile -Value $url -Encoding UTF8
Write-Host "Gateway online: $url"
Wait-Process -Id $gateway.Id
