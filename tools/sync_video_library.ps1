param(
  [string]$Root = 'C:\LeonidanosVideoPipeline'
)

$ErrorActionPreference = 'Stop'

function Read-SecretText([string]$Prompt) {
  $secure = Read-Host $Prompt -AsSecureString
  $ptr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
  try {
    return [Runtime.InteropServices.Marshal]::PtrToStringBSTR($ptr)
  }
  finally {
    [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($ptr)
  }
}

if (-not (Test-Path -LiteralPath $Root -PathType Container)) {
  throw "Biblioteca nao encontrada: $Root"
}

$python = Get-Command python -ErrorAction SilentlyContinue
if (-not $python) { $python = Get-Command py -ErrorAction SilentlyContinue }
if (-not $python) { throw 'Python nao encontrado no PATH.' }

if ([string]::IsNullOrWhiteSpace($env:SUPABASE_S3_ACCESS_KEY_ID)) {
  $env:SUPABASE_S3_ACCESS_KEY_ID = Read-SecretText 'Cole o S3 Access Key ID do Supabase (nao sera exibido)'
}
if ([string]::IsNullOrWhiteSpace($env:SUPABASE_S3_SECRET_ACCESS_KEY)) {
  $env:SUPABASE_S3_SECRET_ACCESS_KEY = Read-SecretText 'Cole o S3 Secret Access Key do Supabase (nao sera exibido)'
}

$env:AWS_REQUEST_CHECKSUM_CALCULATION = 'when_required'
$env:AWS_RESPONSE_CHECKSUM_VALIDATION = 'when_required'
$env:AWS_EC2_METADATA_DISABLED = 'true'

try {
  Write-Host 'Instalando cliente S3 fixado e leitor de PDF...'
  & $python.Source -m pip install --disable-pip-version-check --upgrade `
    'boto3==1.40.45' 'botocore==1.40.45' 's3transfer==0.14.0' 'pypdf==6.1.1'
  if ($LASTEXITCODE -ne 0) { throw 'Falha ao instalar dependencias do sincronizador.' }

  Write-Host 'Validando credenciais, endpoint, bucket e escrita real no S3...'
  & $python.Source tools\s3_probe.py
  if ($LASTEXITCODE -ne 0) {
    throw 'Preflight S3 falhou. Nenhum video foi enviado. Veja a mensagem S3 PREFLIGHT ERROR acima.'
  }

  $ffprobe = Get-Command ffprobe -ErrorAction SilentlyContinue
  if (-not $ffprobe) {
    Write-Warning 'ffprobe nao foi encontrado. O upload funciona, mas algumas duracoes ficarao vazias no catalogo.'
  }

  Write-Host "Sincronizando biblioteca aprovada em $Root via Supabase S3 direto..."
  & $python.Source tools\sync_video_library_v2.py --root $Root
  if ($LASTEXITCODE -ne 0) { throw "Sincronizacao falhou com codigo $LASTEXITCODE" }

  Write-Host 'Sincronizacao concluida. O Dell nao e necessario para os renders.'
}
finally {
  Remove-Item Env:SUPABASE_S3_ACCESS_KEY_ID -ErrorAction SilentlyContinue
  Remove-Item Env:SUPABASE_S3_SECRET_ACCESS_KEY -ErrorAction SilentlyContinue
  Remove-Item Env:AWS_REQUEST_CHECKSUM_CALCULATION -ErrorAction SilentlyContinue
  Remove-Item Env:AWS_RESPONSE_CHECKSUM_VALIDATION -ErrorAction SilentlyContinue
  Remove-Item Env:AWS_EC2_METADATA_DISABLED -ErrorAction SilentlyContinue
}
