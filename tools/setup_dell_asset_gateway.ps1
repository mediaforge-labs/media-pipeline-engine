param([string]$Repo='C:\media-pipeline-engine',[string]$Root='C:\LeonidanosVideoPipeline')
$ErrorActionPreference='Stop'
$cfg=Join-Path $env:LOCALAPPDATA 'MediaForge'; New-Item -ItemType Directory -Force -Path $cfg | Out-Null
$cred=Join-Path $cfg 'supabase-secret.dpapi'; $cloudflared=Join-Path $cfg 'cloudflared.exe'
if(!(Test-Path $cred)){ $s=Read-Host 'Cole a SUPABASE_SECRET_KEY (sera salva criptografada pelo Windows para este usuario)' -AsSecureString; $s | ConvertFrom-SecureString | Set-Content $cred }
if(!(Test-Path $cloudflared)){ Write-Host 'Baixando Cloudflare Tunnel...'; Invoke-WebRequest 'https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-windows-amd64.exe' -OutFile $cloudflared }
$runner=Join-Path $Repo 'tools\run_dell_asset_gateway.ps1'
if(!(Test-Path $runner)){throw "Arquivo nao encontrado: $runner. Rode git pull em $Repo."}
Write-Host 'Atualizando catalogo metadata-only...'; & powershell -ExecutionPolicy Bypass -File $runner -Repo $Repo -Root $Root -CatalogOnly
if($LASTEXITCODE -ne 0){throw 'Catalogacao falhou.'}
$cmd="powershell.exe -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$runner`" -Repo `"$Repo`" -Root `"$Root`""
New-ItemProperty -Path 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run' -Name 'MediaForgeDellAssetGateway' -Value $cmd -PropertyType String -Force | Out-Null
Write-Host 'Iniciando gateway...'; Start-Process powershell.exe -ArgumentList '-WindowStyle','Hidden','-ExecutionPolicy','Bypass','-File',$runner,'-Repo',$Repo,'-Root',$Root
Write-Host 'MediaForge Dell Asset Gateway instalado. Os originais no Dell nunca sao apagados; apenas copias temporarias do GitHub sao removidas apos o render.'
