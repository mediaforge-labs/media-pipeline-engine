param([string]$Repo='C:\media-pipeline-engine',[string]$Root='C:\LeonidanosVideoPipeline')
$ErrorActionPreference='Stop'

$SupabaseUrl='https://rhddgfvtrkmusbvphnlg.supabase.co'
$cfg=Join-Path $env:LOCALAPPDATA 'MediaForge'
New-Item -ItemType Directory -Force -Path $cfg | Out-Null
$cred=Join-Path $cfg 'supabase-secret.dpapi'
$cloudflared=Join-Path $cfg 'cloudflared.exe'

function Convert-SecureToPlain([Security.SecureString]$Secure) {
  $ptr=[Runtime.InteropServices.Marshal]::SecureStringToBSTR($Secure)
  try { return [Runtime.InteropServices.Marshal]::PtrToStringBSTR($ptr) }
  finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($ptr) }
}

function Test-SupabaseApiKey([string]$Key) {
  if([string]::IsNullOrWhiteSpace($Key)){ return $false }
  if(-not ($Key.StartsWith('sb_secret_') -or $Key.StartsWith('eyJ'))){
    Write-Warning 'Essa chave nao parece uma Supabase Secret/Service Role API Key. Nao use S3 Access Key nem S3 Secret Access Key.'
    return $false
  }
  try {
    $headers=@{ apikey=$Key; Authorization="Bearer $Key" }
    $uri="$SupabaseUrl/rest/v1/mediaforge_assets?select=asset_id&limit=1"
    Invoke-RestMethod -Method Get -Uri $uri -Headers $headers -TimeoutSec 30 | Out-Null
    return $true
  }
  catch {
    Write-Warning 'A chave foi recusada pelo Supabase REST API. Use Settings > API Keys > Secret key (sb_secret_...) ou a legacy service_role key. Nao use as credenciais S3.'
    return $false
  }
}

$valid=$false
if(Test-Path $cred){
  try {
    $stored=Get-Content $cred -Raw | ConvertTo-SecureString
    $plain=Convert-SecureToPlain $stored
    if(Test-SupabaseApiKey $plain){
      Write-Host 'Credencial Supabase local validada.'
      $valid=$true
    } else {
      Write-Warning 'A credencial salva anteriormente e invalida e sera substituida.'
      Remove-Item $cred -Force -ErrorAction SilentlyContinue
    }
  } catch {
    Remove-Item $cred -Force -ErrorAction SilentlyContinue
  }
}

if(-not $valid){
  for($attempt=1;$attempt -le 3 -and -not $valid;$attempt++){
    Write-Host ''
    Write-Host 'Cole a SUPABASE SECRET API KEY do projeto Portal Leonidanos.'
    Write-Host 'Aceito: sb_secret_... ou legacy service_role (eyJ...).'
    Write-Host 'NAO cole S3 Access Key ID nem S3 Secret Access Key.'
    $s=Read-Host 'Supabase Secret API Key (nao sera exibida)' -AsSecureString
    $plain=Convert-SecureToPlain $s
    if(Test-SupabaseApiKey $plain){
      $s | ConvertFrom-SecureString | Set-Content $cred
      $valid=$true
      Write-Host 'Chave validada e salva criptografada pelo Windows.'
    }
  }
}
if(-not $valid){ throw 'Nao foi possivel validar uma Supabase Secret API Key apos 3 tentativas.' }

if(!(Test-Path $cloudflared)){
  Write-Host 'Baixando Cloudflare Tunnel...'
  Invoke-WebRequest 'https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-windows-amd64.exe' -OutFile $cloudflared
}

$runner=Join-Path $Repo 'tools\run_dell_asset_gateway.ps1'
if(!(Test-Path $runner)){ throw "Arquivo nao encontrado: $runner. Rode git pull em $Repo." }
if(!(Test-Path $Root -PathType Container)){ throw "Biblioteca nao encontrada: $Root" }

Write-Host 'Atualizando catalogo metadata-only...'
& powershell -ExecutionPolicy Bypass -File $runner -Repo $Repo -Root $Root -CatalogOnly
if($LASTEXITCODE -ne 0){ throw 'Catalogacao falhou.' }

$cmd="powershell.exe -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$runner`" -Repo `"$Repo`" -Root `"$Root`""
New-ItemProperty -Path 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run' -Name 'MediaForgeDellAssetGateway' -Value $cmd -PropertyType String -Force | Out-Null
Write-Host 'Iniciando gateway...'
Start-Process powershell.exe -ArgumentList '-WindowStyle','Hidden','-ExecutionPolicy','Bypass','-File',$runner,'-Repo',$Repo,'-Root',$Root
Write-Host 'MediaForge Dell Asset Gateway instalado. Originais no Dell permanecem intactos; copias temporarias do GitHub sao removidas apos o render.'
