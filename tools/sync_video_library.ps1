param(
  [string]$Root = 'C:\LeonidanosVideoPipeline'
)

$ErrorActionPreference = 'Stop'
$env:SUPABASE_URL = 'https://rhddgfvtrkmusbvphnlg.supabase.co'

if ([string]::IsNullOrWhiteSpace($env:SUPABASE_SECRET_KEY)) {
  $secure = Read-Host 'Cole a SUPABASE_SECRET_KEY do MediaForge (ela nao sera exibida)' -AsSecureString
  $ptr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
  try {
    $env:SUPABASE_SECRET_KEY = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($ptr)
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

$ffprobe = Get-Command ffprobe -ErrorAction SilentlyContinue
if (-not $ffprobe) {
  Write-Warning 'ffprobe nao foi encontrado. O upload funciona, mas algumas duracoes ficarao vazias no catalogo.'
}

Write-Host 'Instalando dependencia leve para o upload...'
& $python.Source -m pip install --disable-pip-version-check requests==2.32.5

Write-Host "Sincronizando biblioteca aprovada em $Root ..."
& $python.Source tools\sync_video_library.py --root $Root
if ($LASTEXITCODE -ne 0) { throw "Sincronizacao falhou com codigo $LASTEXITCODE" }

Write-Host 'Sincronizacao concluida. O Dell nao e necessario para os renders.'
Remove-Item Env:SUPABASE_SECRET_KEY -ErrorAction SilentlyContinue
