param(
  [string]$Repo='C:\media-pipeline-engine',
  [string]$Root='C:\LeonidanosVideoPipeline'
)
$ErrorActionPreference='Stop'

$ExpectedVersion='3.2-active-gta-vi-rest'
$TaskName='MediaForgeDellAssetGateway'
$cfg=Join-Path $env:LOCALAPPDATA 'MediaForge'
New-Item -ItemType Directory -Force -Path $cfg | Out-Null

$a=Join-Path $cfg 's3-access.dpapi'
$s=Join-Path $cfg 's3-secret.dpapi'
$catalog=Join-Path $cfg 'catalog.json'
$urlfile=Join-Path $cfg 'gateway-url.txt'
$cloudflared=Join-Path $cfg 'cloudflared.exe'

function PlainSecure([Security.SecureString]$Secure){
  $p=[Runtime.InteropServices.Marshal]::SecureStringToBSTR($Secure)
  try { return [Runtime.InteropServices.Marshal]::PtrToStringBSTR($p) }
  finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($p) }
}

function Save-SecureFile([Security.SecureString]$Secure,[string]$Path){
  $enc=ConvertFrom-SecureString -SecureString $Secure
  [System.IO.File]::WriteAllText($Path,$enc,[System.Text.UTF8Encoding]::new($false))
}

function Load-PlainFile([string]$Path){
  $raw=[System.IO.File]::ReadAllText($Path).Trim()
  if([string]::IsNullOrWhiteSpace($raw)){ throw "Credencial vazia: $Path" }
  $sec=ConvertTo-SecureString -String $raw
  return PlainSecure $sec
}

function Set-S3Env([string]$Access,[string]$Secret){
  $env:SUPABASE_S3_ACCESS_KEY_ID=$Access
  $env:SUPABASE_S3_SECRET_ACCESS_KEY=$Secret
  $env:AWS_REQUEST_CHECKSUM_CALCULATION='when_required'
  $env:AWS_RESPONSE_CHECKSUM_VALIDATION='when_required'
  $env:AWS_EC2_METADATA_DISABLED='true'
}

function Clear-S3Env(){
  Remove-Item Env:SUPABASE_S3_ACCESS_KEY_ID -ErrorAction SilentlyContinue
  Remove-Item Env:SUPABASE_S3_SECRET_ACCESS_KEY -ErrorAction SilentlyContinue
  Remove-Item Env:AWS_REQUEST_CHECKSUM_CALCULATION -ErrorAction SilentlyContinue
  Remove-Item Env:AWS_RESPONSE_CHECKSUM_VALIDATION -ErrorAction SilentlyContinue
  Remove-Item Env:AWS_EC2_METADATA_DISABLED -ErrorAction SilentlyContinue
}

if(!(Test-Path $Repo -PathType Container)){ throw "Repositorio nao encontrado: $Repo" }
if(!(Test-Path $Root -PathType Container)){ throw "Biblioteca nao encontrada: $Root" }
$activeRoot=Join-Path $Root 'media\gta_vi'
if(!(Test-Path $activeRoot -PathType Container)){ throw "Colecao ativa GTA VI nao encontrada: $activeRoot" }

$py=Get-Command python -ErrorAction SilentlyContinue
if(!$py){$py=Get-Command py -ErrorAction SilentlyContinue}
if(!$py){throw 'Python nao encontrado.'}

& $py.Source -m pip install --disable-pip-version-check 'boto3==1.40.45' 'botocore==1.40.45' 's3transfer==0.14.0' 'pypdf==6.1.1' | Out-Null
if($LASTEXITCODE -ne 0){ throw 'Falha ao instalar dependencias do gateway.' }

$valid=$false
if((Test-Path $a) -and (Test-Path $s)){
  try {
    $access=Load-PlainFile $a
    $secret=Load-PlainFile $s
    Set-S3Env $access $secret
    & $py.Source (Join-Path $Repo 'tools\s3_probe.py')
    if($LASTEXITCODE -eq 0){
      $valid=$true
      Write-Host 'Credenciais S3 locais existentes validadas.'
    }
  } catch {
    Write-Warning 'Credenciais S3 locais antigas nao puderam ser usadas; elas serao recriadas.'
  }
  if(-not $valid){ Remove-Item $a,$s -Force -ErrorAction SilentlyContinue }
}

if(-not $valid){
  for($i=1;$i -le 3 -and -not $valid;$i++){
    Write-Host ''
    Write-Host 'Use Supabase > Storage > S3 > Access Keys.'
    $sa=Read-Host 'S3 Access Key ID (nao sera exibido)' -AsSecureString
    $ss=Read-Host 'S3 Secret Access Key (nao sera exibido)' -AsSecureString
    $access=PlainSecure $sa
    $secret=PlainSecure $ss
    Set-S3Env $access $secret
    & $py.Source (Join-Path $Repo 'tools\s3_probe.py')
    if($LASTEXITCODE -eq 0){
      Save-SecureFile $sa $a
      Save-SecureFile $ss $s
      $valid=$true
      Write-Host 'Credenciais S3 validadas e salvas criptografadas pelo Windows.'
    } else {
      Write-Warning 'Credenciais S3 recusadas.'
    }
  }
}
if(-not $valid){ Clear-S3Env; throw 'Nao foi possivel validar as credenciais S3.' }

if(!(Test-Path $cloudflared)){
  Write-Host 'Baixando Cloudflare Tunnel...'
  Invoke-WebRequest 'https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-windows-amd64.exe' -OutFile $cloudflared
}

$gatewayPy=Join-Path $Repo 'tools\dell_asset_gateway_v32.py'
$runner=Join-Path $Repo 'tools\run_dell_asset_gateway_v32.ps1'
if(!(Test-Path $gatewayPy)){ Clear-S3Env; throw "Arquivo nao encontrado: $gatewayPy. Rode git pull." }
if(!(Test-Path $runner)){ Clear-S3Env; throw "Arquivo nao encontrado: $runner. Rode git pull." }

Write-Host 'Atualizando catalogo da colecao ativa media\gta_vi para o contrato v3.2...'
& $py.Source $gatewayPy --root $Root --catalog-file $catalog --sync-catalog
if($LASTEXITCODE -ne 0){ Clear-S3Env; throw 'Catalogacao v3.2 falhou.' }

try {
  $cat=Get-Content $catalog -Raw | ConvertFrom-Json
  $count=@($cat.assets).Count
  if($cat.scope -ne 'media/gta_vi-active-edit-only'){ throw "Escopo inesperado: $($cat.scope)" }
  if($cat.gateway_version -ne $ExpectedVersion){ throw "Gateway/catalogo desatualizado: $($cat.gateway_version)" }
  if($count -lt 126){ throw "Catalogo ativo tem somente $count visuais unicos; minimo esperado: 126." }
  $bad=@($cat.assets | Where-Object {
    $_.collection -ne 'media/gta_vi' -or
    $_.file_name -notlike '*-mudo.mp4' -or
    $_.relative_path -notmatch '^media/gta_vi/'
  })
  if($bad.Count -gt 0){ throw "Catalogo contem $($bad.Count) assets fora do contrato." }
  Write-Host "Catalogo v3.2 pronto: $count visuais unicos elegiveis."
} catch {
  Clear-S3Env
  throw "Catalogo invalido: $($_.Exception.Message)"
}

# Replace the fragile HKCU-only startup entry with a self-restarting Scheduled Task.
$legacyRun='HKCU:\Software\Microsoft\Windows\CurrentVersion\Run'
Remove-ItemProperty -Path $legacyRun -Name 'MediaForgeDellAssetGateway' -ErrorAction SilentlyContinue

$quotedRunner='"{0}"' -f $runner
$quotedRepo='"{0}"' -f $Repo
$quotedRoot='"{0}"' -f $Root
$runnerArgs="-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File $quotedRunner -Repo $quotedRepo -Root $quotedRoot"

$taskRegistered=$false
try {
  $identity=[System.Security.Principal.WindowsIdentity]::GetCurrent().Name
  $action=New-ScheduledTaskAction -Execute 'powershell.exe' -Argument $runnerArgs
  $trigger=New-ScheduledTaskTrigger -AtLogOn -User $identity
  $principal=New-ScheduledTaskPrincipal -UserId $identity -LogonType Interactive -RunLevel Limited
  $settings=New-ScheduledTaskSettingsSet `
    -RestartCount 999 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -MultipleInstances IgnoreNew `
    -StartWhenAvailable
  Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $action `
    -Trigger $trigger `
    -Principal $principal `
    -Settings $settings `
    -Description 'MediaForge Dell Asset Gateway v3.2 - supplier only, GitHub renders' `
    -Force | Out-Null
  $taskRegistered=$true
  Write-Host 'Agendador configurado com reinicio automatico.'
} catch {
  Write-Warning "Nao foi possivel registrar Scheduled Task: $($_.Exception.Message)"
  $cmd="powershell.exe $runnerArgs"
  New-ItemProperty -Path $legacyRun -Name 'MediaForgeDellAssetGateway' -Value $cmd -PropertyType String -Force | Out-Null
  Write-Warning 'Fallback HKCU Run configurado.'
}

Remove-Item $urlfile -Force -ErrorAction SilentlyContinue
try { Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue } catch { }
if($taskRegistered){
  Start-ScheduledTask -TaskName $TaskName
} else {
  Start-Process powershell.exe -ArgumentList $runnerArgs -WindowStyle Hidden | Out-Null
}

$ready=$false
$lastUrl=''
for($i=0;$i -lt 120;$i++){
  Start-Sleep -Seconds 1
  if(Test-Path $urlfile){
    $u=[System.IO.File]::ReadAllText($urlfile).Trim()
    if($u){
      $lastUrl=$u
      try {
        $h=Invoke-RestMethod -Method Get -Uri ($u.TrimEnd('/') + '/health') -TimeoutSec 15
        if(
          $h.ok -eq $true -and
          $h.version -eq $ExpectedVersion -and
          [int]$h.catalog_assets -ge 126 -and
          $h.range_requests -eq $true
        ){
          $ready=$true
          Write-Host "Gateway v3.2 online e validado: $u"
          Write-Host "Assets ativos: $($h.catalog_assets); Range HTTP: $($h.range_requests)"
          break
        }
      } catch { }
    }
  }
}

Clear-S3Env
if(-not $ready){
  throw "Gateway v3.2 nao ficou publico. Ultima URL: $lastUrl. Consulte %LOCALAPPDATA%\MediaForge\cloudflared.err.log."
}

Write-Host ''
Write-Host 'SETUP V3.2 CONCLUIDO.'
Write-Host 'Dell = fornecedor de midia; GitHub = TTS/render/validacao/upload.'
Write-Host 'O gateway agora e reiniciado automaticamente no login e o tunnel se auto-recupera.'
