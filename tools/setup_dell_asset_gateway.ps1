param([string]$Repo='C:\media-pipeline-engine',[string]$Root='C:\LeonidanosVideoPipeline')
$ErrorActionPreference='Stop'
$cfg=Join-Path $env:LOCALAPPDATA 'MediaForge'; New-Item -ItemType Directory -Force -Path $cfg|Out-Null
$a=Join-Path $cfg 's3-access.dpapi'; $s=Join-Path $cfg 's3-secret.dpapi'; $old=Join-Path $cfg 'supabase-secret.dpapi'; $cloudflared=Join-Path $cfg 'cloudflared.exe'
Remove-Item $old -Force -ErrorAction SilentlyContinue
function PlainSecure($sec){$p=[Runtime.InteropServices.Marshal]::SecureStringToBSTR($sec);try{return [Runtime.InteropServices.Marshal]::PtrToStringBSTR($p)}finally{[Runtime.InteropServices.Marshal]::ZeroFreeBSTR($p)}}
function LoadPlain($path){$sec=Get-Content $path -Raw|ConvertTo-SecureString;return PlainSecure $sec}
$py=Get-Command python -ErrorAction SilentlyContinue;if(!$py){$py=Get-Command py -ErrorAction SilentlyContinue};if(!$py){throw 'Python nao encontrado.'}
& $py.Source -m pip install --disable-pip-version-check 'boto3==1.40.45' 'botocore==1.40.45' 's3transfer==0.14.0' 'pypdf==6.1.1' | Out-Null
$valid=$false
if((Test-Path $a) -and (Test-Path $s)){
  $env:SUPABASE_S3_ACCESS_KEY_ID=LoadPlain $a; $env:SUPABASE_S3_SECRET_ACCESS_KEY=LoadPlain $s; $env:AWS_REQUEST_CHECKSUM_CALCULATION='when_required'; $env:AWS_RESPONSE_CHECKSUM_VALIDATION='when_required'; $env:AWS_EC2_METADATA_DISABLED='true'
  & $py.Source (Join-Path $Repo 'tools\s3_probe.py')
  if($LASTEXITCODE -eq 0){$valid=$true;Write-Host 'Credenciais S3 locais validadas.'}else{Remove-Item $a,$s -Force -ErrorAction SilentlyContinue}
}
if(-not $valid){
  for($i=1;$i -le 3 -and -not $valid;$i++){
    Write-Host ''; Write-Host 'Use as credenciais em Storage > S3 Access Keys.'
    $sa=Read-Host 'S3 Access Key ID (nao sera exibido)' -AsSecureString; $ss=Read-Host 'S3 Secret Access Key (nao sera exibido)' -AsSecureString
    $env:SUPABASE_S3_ACCESS_KEY_ID=PlainSecure $sa; $env:SUPABASE_S3_SECRET_ACCESS_KEY=PlainSecure $ss; $env:AWS_REQUEST_CHECKSUM_CALCULATION='when_required'; $env:AWS_RESPONSE_CHECKSUM_VALIDATION='when_required'; $env:AWS_EC2_METADATA_DISABLED='true'
    & $py.Source (Join-Path $Repo 'tools\s3_probe.py')
    if($LASTEXITCODE -eq 0){$sa|ConvertFrom-SecureString|Set-Content $a;$ss|ConvertFrom-SecureString|Set-Content $s;$valid=$true;Write-Host 'Credenciais S3 validadas e salvas criptografadas pelo Windows.'}else{Write-Warning 'Credenciais S3 recusadas. Confira Access Key ID e Secret Access Key.'}
  }
}
if(-not $valid){throw 'Nao foi possivel validar as credenciais S3 apos 3 tentativas.'}
if(!(Test-Path $cloudflared)){Write-Host 'Baixando Cloudflare Tunnel...';Invoke-WebRequest 'https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-windows-amd64.exe' -OutFile $cloudflared}
$runner=Join-Path $Repo 'tools\run_dell_asset_gateway.ps1'; if(!(Test-Path $runner)){throw "Arquivo nao encontrado: $runner"}; if(!(Test-Path $Root -PathType Container)){throw "Biblioteca nao encontrada: $Root"}
Write-Host 'Atualizando catalogo metadata-only via S3...'; & powershell -ExecutionPolicy Bypass -File $runner -Repo $Repo -Root $Root -CatalogOnly; if($LASTEXITCODE -ne 0){throw 'Catalogacao falhou.'}
$cmd="powershell.exe -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$runner`" -Repo `"$Repo`" -Root `"$Root`""
New-ItemProperty -Path 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run' -Name 'MediaForgeDellAssetGateway' -Value $cmd -PropertyType String -Force|Out-Null
Write-Host 'Iniciando gateway...';Start-Process powershell.exe -ArgumentList '-WindowStyle','Hidden','-ExecutionPolicy','Bypass','-File',$runner,'-Repo',$Repo,'-Root',$Root
Write-Host 'MediaForge Dell Asset Gateway instalado. Nenhum MP4 foi enviado ao Supabase.'
