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

if ([string]::IsNullOrWhiteSpace($env:SUPABASE_S3_ACCESS_KEY_ID)) {
  $env:SUPABASE_S3_ACCESS_KEY_ID = Read-SecretText 'Cole o S3 Access Key ID do Supabase (nao sera exibido)'
}
if ([string]::IsNullOrWhiteSpace($env:SUPABASE_S3_SECRET_ACCESS_KEY)) {
  $env:SUPABASE_S3_SECRET_ACCESS_KEY = Read-SecretText 'Cole o S3 Secret Access Key do Supabase (nao sera exibido)'
}

if (-not (Test-Path -LiteralPath $Root -PathType Container)) {
  throw "Biblioteca nao encontrada: $Root"
}

$python = Get-Command python -ErrorAction SilentlyContinue
if (-not $python) { $python = Get-Command py -ErrorAction SilentlyContinue }
if (-not $python) { throw 'Python nao encontrado no PATH.' }

$ffprobe = Get-Command ffprobe -ErrorAction SilentlyContinue
if (-not $ffprobe) {
  Write-Warning 'ffprobe nao foi encontrado. O upload funciona, mas algumas duracoes ficarao vazias no catalogo.'
}

Write-Host 'Instalando dependencias do sincronizador S3...'
& $python.Source -m pip install --disable-pip-version-check boto3==1.40.45
if ($LASTEXITCODE -ne 0) { throw 'Falha ao instalar boto3.' }

Write-Host "Sincronizando biblioteca aprovada em $Root via Supabase S3..."
& $python.Source tools\sync_video_library.py --root $Root
if ($LASTEXITCODE -ne 0) { throw "Sincronizacao falhou com codigo $LASTEXITCODE" }

Write-Host 'Sincronizacao concluida. O Dell nao e necessario para os renders.'
Remove-Item Env:SUPABASE_S3_ACCESS_KEY_ID -ErrorAction SilentlyContinue
Remove-Item Env:SUPABASE_S3_SECRET_ACCESS_KEY -ErrorAction SilentlyContinue
