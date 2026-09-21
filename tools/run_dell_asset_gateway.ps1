param([string]$Repo='C:\media-pipeline-engine',[string]$Root='C:\LeonidanosVideoPipeline',[switch]$CatalogOnly)
$ErrorActionPreference='Stop'

$cfg=Join-Path $env:LOCALAPPDATA 'MediaForge'
$a=Join-Path $cfg 's3-access.dpapi'
$s=Join-Path $cfg 's3-secret.dpapi'
$urlfile=Join-Path $cfg 'gateway-url.txt'
$catalog=Join-Path $cfg 'catalog.json'
$cloudflared=Join-Path $cfg 'cloudflared.exe'
$errlog=Join-Path $cfg 'cloudflared.err.log'
$outlog=Join-Path $cfg 'cloudflared.out.log'
$gatewayPid=Join-Path $cfg 'gateway.pid'
$tunnelPid=Join-Path $cfg 'tunnel.pid'

function PlainSecure([Security.SecureString]$Secure){
  $p=[Runtime.InteropServices.Marshal]::SecureStringToBSTR($Secure)
  try { return [Runtime.InteropServices.Marshal]::PtrToStringBSTR($p) }
  finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($p) }
}

function Load-PlainFile([string]$Path){
  $raw=[System.IO.File]::ReadAllText($Path).Trim()
  if([string]::IsNullOrWhiteSpace($raw)){ throw "Credencial vazia: $Path" }
  $sec=ConvertTo-SecureString -String $raw
  return PlainSecure $sec
}

function Stop-SavedProcess([string]$PidFile,[string[]]$AllowedNames){
  if(!(Test-Path $PidFile)){ return }
  try {
    $pidValue=[int]([System.IO.File]::ReadAllText($PidFile).Trim())
    $proc=Get-Process -Id $pidValue -ErrorAction SilentlyContinue
    if($proc -and ($AllowedNames -contains $proc.ProcessName.ToLower())){
      Stop-Process -Id $pidValue -Force -ErrorAction SilentlyContinue
      Start-Sleep -Milliseconds 300
    }
  } catch { }
  Remove-Item $PidFile -Force -ErrorAction SilentlyContinue
}

if(!(Test-Path $a) -or !(Test-Path $s)){ throw 'Credenciais S3 locais ausentes. Rode setup_dell_asset_gateway.ps1.' }
if(!(Test-Path $Root -PathType Container)){ throw "Biblioteca nao encontrada: $Root" }
if(!(Test-Path $cloudflared -PathType Leaf)){ throw 'cloudflared ausente. Rode setup_dell_asset_gateway.ps1.' }

$env:SUPABASE_S3_ACCESS_KEY_ID=Load-PlainFile $a
$env:SUPABASE_S3_SECRET_ACCESS_KEY=Load-PlainFile $s
$env:AWS_REQUEST_CHECKSUM_CALCULATION='when_required'
$env:AWS_RESPONSE_CHECKSUM_VALIDATION='when_required'
$env:AWS_EC2_METADATA_DISABLED='true'

$py=Get-Command python -ErrorAction SilentlyContinue
if(!$py){$py=Get-Command py -ErrorAction SilentlyContinue}
if(!$py){throw 'Python nao encontrado.'}

& $py.Source -m pip install --disable-pip-version-check 'boto3==1.40.45' 'botocore==1.40.45' 's3transfer==0.14.0' 'pypdf==6.1.1' | Out-Null
if($LASTEXITCODE -ne 0){ throw 'Falha ao instalar dependencias do gateway.' }

$gatewayPy=Join-Path $Repo 'tools\dell_asset_gateway.py'
if(!(Test-Path $gatewayPy)){ throw "Arquivo nao encontrado: $gatewayPy" }

# Always rebuild the metadata-only catalog from the actual Dell repository before
# serving requests. This prevents a healthy gateway from advertising stale files after
# media/gta_vi changes. No MP4 is uploaded; only catalog metadata is synchronized.
Write-Host 'Atualizando catalogo metadata-only a partir do repositorio local...'
& $py.Source $gatewayPy --root $Root --catalog-file $catalog --sync-catalog
if($LASTEXITCODE -ne 0){ throw 'Falha ao atualizar o catalogo Dell.' }
if(!(Test-Path $catalog -PathType Leaf)){ throw 'Catalogo local nao foi criado.' }

try {
  $cat=Get-Content $catalog -Raw | ConvertFrom-Json
  $count=@($cat.assets).Count
  if($count -lt 1){ throw 'Catalogo foi criado sem assets elegiveis.' }
  Write-Host "Catalogo atualizado: $count assets unicos elegiveis no gateway."
} catch {
  throw "Catalogo invalido: $($_.Exception.Message)"
}

if($CatalogOnly){ exit 0 }

Stop-SavedProcess $gatewayPid @('python','python3','py')
Stop-SavedProcess $tunnelPid @('cloudflared')
Remove-Item $urlfile,$errlog,$outlog -Force -ErrorAction SilentlyContinue

$gateway=Start-Process $py.Source -ArgumentList $gatewayPy,'--root',$Root,'--catalog-file',$catalog,'--endpoint-file',$urlfile -PassThru -WindowStyle Hidden
[System.IO.File]::WriteAllText($gatewayPid,[string]$gateway.Id,[System.Text.UTF8Encoding]::new($false))

$localReady=$false
for($i=0;$i -lt 20;$i++){
  Start-Sleep -Milliseconds 500
  if($gateway.HasExited){ break }
  try {
    $health=Invoke-RestMethod -Method Get -Uri 'http://127.0.0.1:8765/health' -TimeoutSec 3
    if($health.ok -eq $true){ $localReady=$true; break }
  } catch { }
}
if(-not $localReady){
  Stop-Process -Id $gateway.Id -Force -ErrorAction SilentlyContinue
  Remove-Item $gatewayPid -Force -ErrorAction SilentlyContinue
  throw 'Gateway local nao iniciou em 127.0.0.1:8765.'
}

$tunnel=Start-Process $cloudflared -ArgumentList 'tunnel','--url','http://127.0.0.1:8765','--no-autoupdate' -RedirectStandardError $errlog -RedirectStandardOutput $outlog -PassThru -WindowStyle Hidden
[System.IO.File]::WriteAllText($tunnelPid,[string]$tunnel.Id,[System.Text.UTF8Encoding]::new($false))

$url=$null
for($i=0;$i -lt 75;$i++){
  Start-Sleep -Seconds 1
  if($tunnel.HasExited){ break }
  foreach($lf in @($errlog,$outlog)){
    if(Test-Path $lf){
      $m=Select-String -Path $lf -Pattern 'https://[a-z0-9-]+\.trycloudflare\.com' -AllMatches | Select-Object -Last 1
      if($m){ $url=$m.Matches[0].Value; break }
    }
  }
  if($url){ break }
}

if(-not $url){
  Stop-Process -Id $gateway.Id -Force -ErrorAction SilentlyContinue
  Stop-Process -Id $tunnel.Id -Force -ErrorAction SilentlyContinue
  Remove-Item $gatewayPid,$tunnelPid -Force -ErrorAction SilentlyContinue
  throw "Nao foi possivel obter URL do Cloudflare Tunnel. Veja $errlog"
}

[System.IO.File]::WriteAllText($urlfile,$url,[System.Text.UTF8Encoding]::new($false))

$publicReady=$false
for($i=0;$i -lt 30;$i++){
  Start-Sleep -Seconds 1
  try {
    $health=Invoke-RestMethod -Method Get -Uri ($url.TrimEnd('/') + '/health') -TimeoutSec 10
    if($health.ok -eq $true){ $publicReady=$true; break }
  } catch { }
}
if(-not $publicReady){
  Stop-Process -Id $gateway.Id -Force -ErrorAction SilentlyContinue
  Stop-Process -Id $tunnel.Id -Force -ErrorAction SilentlyContinue
  Remove-Item $gatewayPid,$tunnelPid -Force -ErrorAction SilentlyContinue
  throw "Tunnel recebeu URL, mas /health publico nao respondeu. Veja $errlog"
}

Write-Host "Gateway online: $url"

try {
  Wait-Process -Id $gateway.Id
}
finally {
  Stop-Process -Id $tunnel.Id -Force -ErrorAction SilentlyContinue
  Remove-Item $gatewayPid,$tunnelPid -Force -ErrorAction SilentlyContinue
}
